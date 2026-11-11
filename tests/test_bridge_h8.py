"""T-P2-H8: gateway.current.selected_node_id (决定_第七波裁定 T-P2-H5 #2, #4).

The saved Gateway selection names a node (T-GW-ID): its identity with the
proxy name it had at pick time as a label, or an older name-only selection
read under the stricter name rule. A Gateway's proxy name moves (latency
order swaps same-named twins, a namesake in an earlier tier pushes it to
``-2``); the node it names does not. The runtime getter
``bridge_gateway_selected_node_id`` maps the selection to the P2-04 Node.id
through the render receipt of the latest strict reload (the proxies the real
renderer emitted and the member it recorded for the selection), only while
that receipt is the one the published route was built from; anything
uncertain omits the field.

Isolated: the P2-04 synthetic runtime, the real clash_formatter renderer and
the real receipt/contract helpers of DesktopRuntime; no live profile, no
Core, no Mihomo, no network, no HKCU or firewall writes.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import inspect
import json
import logging
from pathlib import Path

import pytest
import yaml

import bridge_protocol as protocol
from bridge_publish import Publisher  # noqa: F401  (import side effects match H6)
from bridge_runtime_access import RuntimeAccess, gateway_selected_node_id, loaded_export_revision
from bridge_state import PublicationState
from bridge_tiers import SampledTopics
from desktop_runtime import DesktopRuntime
from gateway_selection import gateway_identity, read_selection, write_bound_selection
from runtime_egress_recovery import CoreLoadedGroupState, CoreLoadedRouteSnapshot, RuntimeNodeRef
from subscription import render_subscription_route
from tests.test_bridge_h5 import exportable, gw, node_id, other, real_order
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)
from tests.test_bridge_p2_08 import dto, observe

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "bridge_schema" / "fixtures" / "v1"


# --------------------------------------------------------------- harness

def prepared(rt, *, tier3_limit=20):
    rt = exportable(rt, tier3_limit=tier3_limit)
    manager = rt.manager
    manager.testing_config = {}
    manager.client_config = rt.config["client"]
    manager.logger = logging.getLogger("tests.bridge_h8")
    rt._wake_parked_tier3_pool_sync_if_causal_key_changed = lambda: None
    rt._mihomo_tier3_export_limit = lambda: tier3_limit
    rt.write_mihomo_runtime_config = lambda *a, **k: pytest.fail("config write")
    rt.select_mihomo_proxy = lambda *a, **k: pytest.fail("Mihomo write")
    return rt


def choose(rt, node, label):
    """Save a pick the way this build saves one: ``node``'s identity with
    ``label`` (its proxy name at pick time), never ``selected``. The manager
    reads the selection back from the config, as
    ProxyTierManager.active_exit_selection does."""
    write_bound_selection(rt.config["tiers"]["exit"], label, gateway_identity(node))
    rt.manager.active_exit_selection = lambda: read_selection(rt.config["tiers"]["exit"])
    rt.manager.active_exit_proxy_name = lambda: read_selection(rt.config["tiers"]["exit"]).label
    return read_selection(rt.config["tiers"]["exit"])


def strict_reload(rt, selected, *, publish=True):
    """One strict reload of the name-only selection ``selected``, written as
    an older build (or a hand edit) writes it: ``tiers.exit.selected``, which
    wins over any saved record. See strict_reload_saved."""
    rt.config["tiers"]["exit"]["selected"] = selected
    rt.manager.active_exit_proxy_name = lambda: selected
    return strict_reload_saved(rt, publish=publish)


def strict_reload_saved(rt, *, publish=True):
    """One strict reload of the selection already saved (e.g. by choose()),
    writing nothing: the real renderer with its receipt, the real
    receipt -> proxy map -> export contract -> loaded-contract commit, then
    (``publish``) the route publication _refresh_runtime_route_snapshot_serialized
    makes from that loaded contract (synthetic only in its Core readback)."""
    view = rt._manager_for_mihomo_runtime_config()
    receipt = {}
    _, payload = render_subscription_route(
        view, "/clash/config", {"tier3_limit": rt._mihomo_tier3_export_limit()},
        render_receipt=receipt)
    proxy_map = rt._runtime_proxy_map_from_render_receipt(receipt)
    contract = rt._runtime_export_contract_for_payload(payload, rendered_proxy_map=proxy_map)
    rt._record_pending_runtime_export_contract(payload, Path("isolated-runtime.yaml"),
                                               contract=contract)
    rt._mark_loaded_runtime_export_contract()
    document = yaml.safe_load(payload)
    if publish:
        publish_route(rt, document)
    return document


def publish_route(rt, document):
    loaded = rt._runtime_loaded_export_contract
    proxy_map = loaded["proxy_map"]
    groups = {group["name"]: tuple(group["proxies"]) for group in document["proxy-groups"]}
    states = (CoreLoadedGroupState("FINAL-EXIT", "Selector", ("EXIT-SELECT",), "EXIT-SELECT"),
              CoreLoadedGroupState("EXIT-SELECT", "Selector", groups["EXIT-SELECT"],
                                   groups["EXIT-SELECT"][0]))
    refs = tuple(RuntimeNodeRef(info["tier"], info["pool_kind"], info["identity"], name)
                 for name, info in proxy_map.items() if info["identity"])
    with rt._runtime_route_snapshot_lock:
        rt._runtime_route_epoch += 1
        rt._runtime_route_snapshot = CoreLoadedRouteSnapshot(
            process_pid=42, process_generation=3, core_generation=8,
            semantic_digest=str(loaded.get("route_authority_semantic_digest")
                                or loaded["semantic_digest"]),
            route_epoch=rt._runtime_route_epoch, network_context_epoch=4,
            group_states=states, proxy_nodes=refs, loaded_at_monotonic=1.0)
        rt._runtime_route_snapshot_payload = {"proxies": {}, "proxy_map": dict(proxy_map)}
    return groups["EXIT-SELECT"]


def ids(rt):
    return {(row["tier"], row["name"]): row["id"] for row in rt.bridge_node_records()["items"]}


def resolved(rt):
    raw = rt.bridge_gateway_selected_node_id()
    assert set(raw) == {"node_id", "reason", "generation"}
    assert raw["generation"] == rt._runtime_generation
    assert (raw["node_id"] is None) == (raw["reason"] is not None)
    return raw["node_id"], raw["reason"]


def exit_select(rt):
    states = {row.name: row for row in rt._runtime_route_snapshot.group_states}
    return states["EXIT-SELECT"].members


# ---------------------------------------------------------------- protocol

def test_p2h8_fixtures_are_new_files_and_validate():
    valid = sorted((V1 / "valid").glob("p2h8_*.json"))
    invalid = sorted((V1 / "invalid").glob("p2h8_*.json"))
    assert len(valid) == 2 and len(invalid) == 4
    for path in valid:
        assert protocol.validate_frame(json.loads(path.read_bytes())) == [], path.name
    for path in invalid:
        case = json.loads(path.read_bytes())
        assert case["expected"] in protocol.validate_frame(case["frame"]), path.name


def test_field_is_optional_and_old_payloads_stay_valid():
    spec = protocol.V1_TOPIC_SPECS["gateway.current"][2]
    assert spec["selected_node_id?"] == "str" and "selected_node_id" not in spec
    base = {"selected": "Tokyo-2", "effective": "Tokyo-2", "missing": False, "applied": True,
            "pending": None}
    assert protocol.validate_topic_payload("gateway.current", base) == []
    stopped = {"selected": "Tokyo-2", "effective": None, "missing": False, "applied": False,
               "pending": None}
    assert protocol.validate_topic_payload("gateway.current", stopped) == []
    assert protocol.validate_topic_payload(
        "gateway.current", {**base, "selected_node_id": "0123456789abcdef01234567"}) == []
    assert protocol.validate_frame(json.loads((V1 / "valid" / "topic_gateway.current.json")
                                              .read_bytes())) == []
    assert "selected_node_id" not in protocol.V1_TOPIC_SPECS["nodes.list"][2]


# ------------------------------------------------------- the runtime getter

def test_cross_tier_duplicate_reverses_the_suffixed_gateway(runtime):
    rt = prepared(runtime)
    target = gw("Tokyo", address="tokyo-gw.example.invalid")
    rt.manager.runtime_nodes["tier2"] = [other("Tokyo", "tier2")]
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "Tokyo-2")
    assert exit_select(rt) == ("Tokyo-2",)
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    assert resolved(rt)[0] == ids(rt)[("gateway", "Tokyo")]        # the nodes.list row
    # The plain name denotes the Gateway as well: a selection names a Gateway
    # (base name "Tokyo"), never the Sentinel node that only owns the proxy
    # name "Tokyo". The renderer serves the Gateway's "Tokyo-2" and records
    # that decision for the saved name on the member.
    strict_reload(rt, "Tokyo")
    assert exit_select(rt) == ("Tokyo-2",)
    loaded = rt._runtime_loaded_export_contract["proxy_map"]
    assert loaded["Tokyo-2"]["selected_for"] == "name:Tokyo"
    assert "selected_for" not in loaded["Tokyo"]                    # the Sentinel's entry
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    # A pick saved by identity names the same node.
    choose(rt, target, "Tokyo-2")
    strict_reload_saved(rt)
    assert exit_select(rt) == ("Tokyo-2",)
    loaded = rt._runtime_loaded_export_contract["proxy_map"]
    assert loaded["Tokyo-2"]["selected_for"] == "id:" + gateway_identity(target)
    assert resolved(rt) == (node_id(rt, "exit", target), None)


def test_a_name_of_another_tier_is_never_reversed_to_a_gateway(runtime):
    rt = prepared(runtime)
    target = gw("Tokyo", address="tokyo-gw.example.invalid")
    rt.manager.runtime_nodes["tier2"] = [other("Tokyo", "tier2")]
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "Tokyo-2")
    # Even a Core whose EXIT-SELECT held the Sentinel proxy's name is refused:
    # the saved name denotes the Gateway (its member "Tokyo-2" in this
    # receipt), which that EXIT-SELECT does not hold. Never the Sentinel.
    with rt._runtime_route_snapshot_lock:
        snapshot = rt._runtime_route_snapshot
        rt._runtime_route_snapshot = replace(snapshot, group_states=(
            snapshot.group_states[0],
            CoreLoadedGroupState("EXIT-SELECT", "Selector", ("Tokyo",), "Tokyo")))
    rt.config["tiers"]["exit"]["selected"] = "Tokyo"
    assert resolved(rt) == (None, "missing")
    status = rt.bridge_gateway_status()
    assert status["stale"] is False and status["effective"] is None   # a Sentinel leaf
    assert (status["selected"], status["missing"], status["applied"]) == ("Tokyo", True, False)
    # A pick saved by identity under the Sentinel's proxy name: refused alike.
    choose(rt, target, "Tokyo")
    assert resolved(rt) == (None, "missing")


def test_latency_reorder_moves_the_suffix_and_the_new_receipt_decides(runtime):
    """Same-named twins swap suffixes with latency order. The selection is
    saved by identity, so each receipt's member for that identity decides and
    the id stays the target's through every swap; it is omitted only while
    the receipt is stale. (Name-era pin replaced: the id used to follow the
    saved name "Paris-2" to the decoy.)"""
    rt = real_order(prepared(runtime))
    target = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["exit"] = [target, decoy]
    choose(rt, target, "Paris-2")
    strict_reload_saved(rt)
    assert exit_select(rt) == ("Paris-2",)
    loaded = rt._runtime_loaded_export_contract["proxy_map"]
    assert loaded["Paris-2"]["node"].address == target.address
    assert loaded["Paris-2"]["selected_for"] == "id:" + gateway_identity(target)
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    # Latency reorders the exit tier; Core still has the old render loaded,
    # where "Paris-2" still is the target.
    target.latency_ms = 5
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    # A strict reload renders the swap: the target is "Paris" now and the
    # decoy holds the saved label. Until this route is published the receipt
    # does not match the published route and nothing is claimed.
    document = strict_reload_saved(rt, publish=False)
    exit_group = next(g for g in document["proxy-groups"] if g["name"] == "EXIT-SELECT")
    assert exit_group["proxies"] == ["Paris"]
    loaded = rt._runtime_loaded_export_contract["proxy_map"]
    assert loaded["Paris"]["node"].address == target.address
    assert loaded["Paris"]["selected_for"] == "id:" + gateway_identity(target)
    assert loaded["Paris-2"]["node"].address == decoy.address
    assert "selected_for" not in loaded["Paris-2"]
    assert resolved(rt) == (None, "receipt_stale")
    # A route whose EXIT-SELECT holds the saved label, now the decoy's proxy
    # name, never reverses to the decoy.
    publish_route(rt, yaml.safe_load(
        "proxy-groups: [{name: EXIT-SELECT, proxies: [Paris-2]}]"))
    assert resolved(rt) == (None, "missing")
    # The reload's own route: the target under its new name.
    publish_route(rt, document)
    assert exit_select(rt) == ("Paris",)
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    # Swapped back: the target is "Paris-2" again, still the target's id.
    target.latency_ms = 50
    strict_reload_saved(rt)
    assert exit_select(rt) == ("Paris-2",)
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    assert node_id(rt, "exit", target) != node_id(rt, "exit", decoy)
    # A name-only selection cannot tell the twins apart: it fails closed
    # (it used to serve whichever twin held the name).
    strict_reload(rt, "Paris")
    assert exit_select(rt) == ("REJECT",)
    assert resolved(rt) == (None, "missing")


def test_redacted_name_reverses_and_only_the_display_name_is_redacted(runtime):
    rt = prepared(runtime)
    secret = "GatewaySecret-77"
    target = gw(f"Osaka {secret}", password=secret, address="osaka-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, target.name)
    shown = next(row for row in rt.bridge_node_records()["items"] if row["tier"] == "gateway")
    assert secret not in shown["name"]
    assert resolved(rt) == (shown["id"], None)
    status = rt.bridge_gateway_status()
    assert secret not in status["selected"] and status["missing"] is False


def test_empty_name_reverses_through_the_fallback_proxy_name(runtime):
    rt = prepared(runtime)
    target = gw("", address="anon-gw.example.invalid", port=8443)
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "trojan-anon-gw.example.invalid:8443")
    assert exit_select(rt) == ("trojan-anon-gw.example.invalid:8443",)
    assert resolved(rt) == (node_id(rt, "exit", target), None)


def test_stopped_core_omits(runtime):
    rt = prepared(runtime)
    strict_reload(rt, "Gateway A")
    assert resolved(rt)[0] == ids(rt)[("gateway", "Gateway A")]
    rt.core_fact = lambda: {"running": False, "pid": None, "process_generation": 0,
                            "core_generation": 8, "cleanup_pending": False}
    assert resolved(rt) == (None, "stopped")


def test_no_selection_or_no_route_omits(runtime):
    rt = prepared(runtime)
    strict_reload(rt, "Gateway A")
    rt.config["tiers"]["exit"]["selected"] = ""
    assert resolved(rt) == (None, "no_selection")
    rt.config["tiers"]["exit"]["selected"] = "Gateway A"
    rt.core_fact = lambda: {"running": True, "pid": 99, "process_generation": 3,
                            "core_generation": 8}        # another Core process
    assert resolved(rt) == (None, "no_route")


def test_ambiguous_identities_omit(runtime):
    rt = prepared(runtime)
    first = gw("Twin A", address="twin.example.invalid")
    second = gw("Twin B", address="twin.example.invalid")   # same 12-field identity
    rt.manager.runtime_nodes["exit"] = [first, second]
    strict_reload(rt, "Twin A")
    assert resolved(rt) == (None, "ambiguous")              # two receipt entries
    rt.manager.runtime_nodes["exit"] = [first]
    strict_reload(rt, "Twin A")
    assert resolved(rt)[0] == node_id(rt, "exit", first)
    rt.manager.runtime_nodes["exit"] = [first, first.clone()]   # loaded twice now
    assert resolved(rt) == (None, "ambiguous")


def test_superseded_receipt_or_changed_owners_omit(runtime):
    rt = prepared(runtime)
    target = gw("Rome", address="rome-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "Rome")
    assert resolved(rt)[0] == node_id(rt, "exit", target)
    # The loaded node changed (a refresh re-keyed it): no loaded node has
    # the receipt's identity any more.
    rt.manager.runtime_nodes["exit"] = [gw("Rome", address="rome-gw.example.invalid",
                                           password="Rotated-2")]
    assert resolved(rt) == (None, "unknown")
    rt.manager.runtime_nodes["exit"] = [target]
    # The saved selection was edited (not reloaded yet): not in EXIT-SELECT.
    rt.config["tiers"]["exit"]["selected"] = "Elsewhere"
    assert resolved(rt) == (None, "missing")
    rt.config["tiers"]["exit"]["selected"] = "Rome"
    # No loaded contract, or one without a digest: nothing to reverse with.
    held = rt._runtime_loaded_export_contract
    rt._runtime_loaded_export_contract = None
    assert resolved(rt) == (None, "receipt_stale")
    rt._runtime_loaded_export_contract = {**held, "semantic_digest": ""}
    assert resolved(rt) == (None, "receipt_stale")
    rt._runtime_loaded_export_contract = held
    # A new runtime generation (the manager already rebuilt): stale.
    rt.manager.runtime_generation = 8
    assert resolved(rt) == (None, "stale")
    rt.manager.runtime_generation = 7
    assert resolved(rt)[0] == node_id(rt, "exit", target)


def test_a_route_published_during_the_read_is_stale(runtime):
    rt = prepared(runtime)
    target = gw("Rome", address="rome-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "Rome")
    lock = rt.manager.lock

    class Swapping:
        def __enter__(self):
            snapshot = rt._runtime_route_snapshot
            rt._runtime_route_snapshot = replace(snapshot, loaded_at_monotonic=2.0)
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    rt.manager.lock = Swapping()
    assert resolved(rt) == (None, "stale")
    rt.manager.lock = lock
    assert resolved(rt)[0] == node_id(rt, "exit", target)
    # An in-place selection edit during the read is stale as well when it
    # changes what is saved (the selection's key): here the bind tick saving
    # the served name's node identity (name:Rome -> id:...).

    class Editing:
        def __init__(self, edit):
            self.edit = edit

        def __enter__(self):
            self.edit()
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    block = rt.config["tiers"]["exit"]
    # Whitespace around the saved name is no new selection (one parser for
    # every reader, one key): the answer stands.
    rt.manager.lock = Editing(lambda: block.update(selected="Rome "))
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    rt.manager.lock = Editing(
        lambda: write_bound_selection(block, "Rome", gateway_identity(target)))
    assert resolved(rt) == (None, "stale")
    rt.manager.lock = lock
    assert read_selection(block).key == "id:" + gateway_identity(target)
    assert resolved(rt) == (node_id(rt, "exit", target), None)   # the same node, by identity


def test_getter_is_read_only_and_never_nests_locks(runtime):
    rt = prepared(runtime)
    strict_reload(rt, "Gateway A")
    route_lock, intent_lock, manager_lock = (rt._runtime_route_snapshot_lock,
                                             rt._runtime_intent_lock, rt.manager.lock)

    class Checked:
        def __init__(self, inner, others):
            self.inner, self.others = inner, others

        def __enter__(self):
            assert not any(lock.depth for lock in self.others), "nested runtime/manager lock"
            return self.inner.__enter__()

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    rt.manager.lock = Checked(manager_lock, (route_lock, intent_lock))
    rt._runtime_route_snapshot_lock = Checked(route_lock, (manager_lock,))
    before = [vars(node.clone()) for node in rt.manager.runtime_nodes["exit"]]
    contract = deepcopy(rt._runtime_loaded_export_contract)
    snapshot = rt._runtime_route_snapshot
    config = deepcopy(rt.config)
    rt._manager_for_mihomo_runtime_config = lambda: pytest.fail("render/export view")
    assert resolved(rt)[0] == ids(rt)[("gateway", "Gateway A")]
    assert [vars(node) for node in rt.manager.runtime_nodes["exit"]] == before
    assert rt._runtime_route_snapshot is snapshot and rt.config == config
    assert rt._runtime_loaded_export_contract.keys() == contract.keys()
    assert rt.api_client.calls == []                       # ForbiddenAPI: no controller request


def test_getter_is_appended_in_the_bridge_region_only():
    source = (ROOT / "desktop_runtime.py").read_bytes()
    # One line-ending style throughout (.gitattributes checks out LF everywhere).
    assert source.count(b"\r\n") in (0, source.count(b"\n"))
    text = source.decode("utf-8")
    start = text.index("    def bridge_gateway_selected_node_id(self):")
    assert start > text.index("    def bridge_sampling_fact_versions(self):")
    assert start < text.index("class RefreshLockBusy(RuntimeError):")
    revision = text.index("    def bridge_loaded_export_revision(self):")
    assert start < revision < text.index("class RefreshLockBusy(RuntimeError):")
    # _loaded_gateway_member: the getter's member decision for the saved
    # selection (called with the receipt it already copied).
    for getter in (DesktopRuntime.bridge_gateway_selected_node_id,
                   DesktopRuntime.bridge_loaded_export_revision,
                   DesktopRuntime._loaded_gateway_member):
        body = inspect.getsource(getter)
        for forbidden in ("api_client", "read_sources", "_refresh_runtime_route_snapshot",
                          "render_subscription_route", "_manager_for_mihomo_runtime_config",
                          "write_", "select_mihomo_proxy", "_node_judgement",
                          "_runtime_post_dial"):
            assert forbidden not in body, forbidden


def test_route_publication_still_keys_the_snapshot_by_the_route_authority_digest():
    """The getter matches the loaded contract's route-authority digest against
    the published snapshot's semantic_digest; the test harness's publish_route
    copies that coupling, so pin it in the two real publication paths."""
    def flat(function):
        return " ".join(inspect.getsource(function).split())

    # 决定_第八波裁定: these two publication functions are Fallback-owned epoch
    # sites; assert only that the coupling is still there (the digest name is
    # used for the published snapshot), not their exact formatting.
    refresh = flat(DesktopRuntime._refresh_runtime_route_snapshot_serialized)
    assert "route_authority_semantic_digest" in refresh and "semantic_digest=" in refresh
    tier3 = flat(DesktopRuntime._commit_tier3_provider_contract_from_readback)
    assert "route_authority_semantic_digest" in tier3 and "semantic_digest=" in tier3
    getter = flat(DesktopRuntime.bridge_gateway_selected_node_id)
    assert ('contract.get("route_authority_semantic_digest") '
            'or contract.get("semantic_digest")') in getter


def test_the_id_hashes_the_loaded_nodes_tuple_not_the_receipt_clones(runtime):
    rt = prepared(runtime)
    target = gw("Lima", address="lima-gw.example.invalid", port=443)
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "Lima")
    # == equal but not repr-equal: the clone's port drifts to 443.0.
    rt._runtime_loaded_export_contract["proxy_map"]["Lima"]["node"].port = 443.0
    assert resolved(rt) == (ids(rt)[("gateway", "Lima")], None)


def test_loaded_export_revision_advances_with_each_loaded_contract(runtime):
    rt = prepared(runtime)
    first = rt.bridge_loaded_export_revision()
    strict_reload(rt, "Gateway A", publish=False)
    assert rt.bridge_loaded_export_revision() == first + 1
    strict_reload(rt, "Gateway A")
    assert rt.bridge_loaded_export_revision() == first + 2
    access = RuntimeAccess(rt)
    assert loaded_export_revision(access) == first + 2
    for bad in (None, -1, "3", 1.0, True):
        rt.bridge_loaded_export_revision = lambda bad=bad: bad
        assert loaded_export_revision(access) is None, bad
    rt.bridge_loaded_export_revision = None
    assert loaded_export_revision(access) is None


# ------------------------------------------------------ the access adapter

def test_access_adapter_accepts_only_an_opaque_id_of_the_same_generation(runtime):
    access = RuntimeAccess(runtime)
    good = "0123456789abcdef01234567"
    runtime.bridge_gateway_selected_node_id = lambda: {"node_id": good, "reason": None,
                                                        "generation": 7}
    assert gateway_selected_node_id(access, generation=7) == good
    assert gateway_selected_node_id(access, generation=8) is None
    assert gateway_selected_node_id(access, generation=None) is None
    for bad in ("Tokyo-2", good.upper(), good[:-1], good + "0", None, 7):
        runtime.bridge_gateway_selected_node_id = (
            lambda bad=bad: {"node_id": bad, "reason": None, "generation": 7})
        assert gateway_selected_node_id(access, generation=7) is None, bad
    runtime.bridge_gateway_selected_node_id = lambda: None
    assert gateway_selected_node_id(access, generation=7) is None
    runtime.bridge_gateway_selected_node_id = None                # no getter at all
    assert gateway_selected_node_id(access, generation=7) is None


# ------------------------------------- gateway.current through SampledTopics

def sampled(runtime, *, running=True):
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    if running:
        lifecycle["core"].update(pid=42, process_generation=3)
    else:
        lifecycle["core"].update(running=False, pid=None, process_generation=0,
                                 cleanup_pending=False)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    work = []
    state = PublicationState("h8")
    topics = SampledTopics(state, access, submit_normal=lambda fn: work.append(fn) or True,
                           config_revision=runtime.bridge_config_revision)
    state.subscribe(["gateway.current"])
    return state, topics, work


def tick(topics, work):
    topics.sample(observe)
    while work:
        work.pop()()


def published(state):
    snapshot = state.snapshot("gateway.current")
    return None if snapshot is None else snapshot["data"]


def test_running_gateway_carries_the_reversed_node_id(runtime):
    rt = prepared(runtime)
    target = gw("Tokyo", address="tokyo-gw.example.invalid")
    rt.manager.runtime_nodes["tier2"] = [other("Tokyo", "tier2")]
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, "Tokyo-2")
    state, topics, work = sampled(rt)
    tick(topics, work)
    data = published(state)
    assert data["selected"] == "Tokyo-2" and data["missing"] is False
    assert data["selected_node_id"] == ids(rt)[("gateway", "Tokyo")]
    assert protocol.validate_topic_payload("gateway.current", data) == []
    # The selection is edited in place to the plain name: the fact version
    # moves and the gateway is re-read. The plain name denotes the Gateway
    # (never the Sentinel node that owns the proxy name "Tokyo"), which the
    # loaded "Tokyo-2" carries: same member, same id.
    gateway_id = ids(rt)[("gateway", "Tokyo")]
    rt.config["tiers"]["exit"]["selected"] = "Tokyo"
    tick(topics, work)
    data = published(state)
    assert (data["selected"], data["missing"], data["applied"]) == ("Tokyo-2", False, True)
    assert data["selected_node_id"] == gateway_id
    # Edited in place to a name no loaded Gateway carries: re-read, missing,
    # shown under its saved name, and the field goes away.
    rt.config["tiers"]["exit"]["selected"] = "Osaka"
    tick(topics, work)
    data = published(state)
    assert (data["selected"], data["missing"], data["applied"]) == ("Osaka", True, False)
    assert "selected_node_id" not in data
    # A reload that loads the plain name: its receipt records the Gateway's
    # "Tokyo-2" for it.
    strict_reload(rt, "Tokyo")
    assert exit_select(rt) == ("Tokyo-2",)
    tick(topics, work)
    data = published(state)
    assert (data["selected"], data["missing"], data["applied"]) == ("Tokyo-2", False, True)
    assert data["selected_node_id"] == gateway_id
    assert protocol.validate_topic_payload("gateway.current", data) == []
    assert rt.api_client.calls == []


def test_a_reload_that_swaps_the_suffix_moves_the_published_id(runtime):
    """Same saved node, new receipt: the publication revision moves and the
    sidecar re-reads (no selection edit). The published name moves with the
    suffix to the target's new proxy name; the id stays the target's. (Name-era
    pin replaced: the id used to move to the decoy that took the saved name.)"""
    rt = real_order(prepared(runtime))
    target = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["exit"] = [target, decoy]
    choose(rt, target, "Paris-2")
    strict_reload_saved(rt)
    state, topics, work = sampled(rt)
    tick(topics, work)
    data = published(state)
    assert (data["selected"], data["missing"], data["applied"]) == ("Paris-2", False, True)
    assert data["selected_node_id"] == node_id(rt, "exit", target)
    target.latency_ms = 5
    strict_reload_saved(rt)
    assert exit_select(rt) == ("Paris",)
    tick(topics, work)
    data = published(state)
    assert data["selected"] == data["effective"] == "Paris"
    assert data["missing"] is False and data["applied"] is True
    assert data["selected_node_id"] == node_id(rt, "exit", target)
    assert data["selected_node_id"] != node_id(rt, "exit", decoy)
    assert protocol.validate_topic_payload("gateway.current", data) == []
    assert rt.api_client.calls == []


def test_a_receipt_installed_without_a_route_publication_withdraws_the_id(runtime):
    """No-capture standby reloads install a new loaded contract without
    republishing the route: the receipt revision in the gateway token makes
    the next sample re-read (no 30 s wait) and the stale id goes away."""
    rt = real_order(prepared(runtime))
    target = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["exit"] = [target, decoy]
    choose(rt, target, "Paris-2")
    strict_reload_saved(rt)
    state, topics, work = sampled(rt)
    tick(topics, work)
    assert published(state)["selected_node_id"] == node_id(rt, "exit", target)
    route = rt.published_route_status()["publication_revision"]
    revision = rt.bridge_loaded_export_revision()
    target.latency_ms = 5
    document = strict_reload_saved(rt, publish=False)
    assert rt.published_route_status()["publication_revision"] == route
    assert rt.bridge_loaded_export_revision() == revision + 1
    assert resolved(rt) == (None, "receipt_stale")
    tick(topics, work)
    data = published(state)
    assert data is not None and data["selected"] == "Paris-2"
    assert "selected_node_id" not in data                   # receipt_stale: omitted
    # Its route is published later: the id is back, still the target's, now
    # under the target's new proxy name (the decoy holds the old one).
    publish_route(rt, document)
    tick(topics, work)
    data = published(state)
    assert data["selected_node_id"] == node_id(rt, "exit", target)
    assert (data["selected"], data["missing"], data["applied"]) == ("Paris", False, True)


def test_gateway_status_in_the_receipt_window_reads_the_published_route(runtime):
    """Between a newer receipt and its route publication (the no-capture
    standby reload), gateway.current still describes the published route,
    which is valid and holds the picked node under its old name "Paris-2"
    (the proxy map published with that route says so). The member must be
    read through that published map, as the startup readers do: pairing the
    published EXIT-SELECT members with the newer receipt (where "Paris-2" is
    now the decoy and the target is "Paris") reports missing/not applied
    while effective is the picked node itself."""
    rt = real_order(prepared(runtime))
    target = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["exit"] = [target, decoy]
    choose(rt, target, "Paris-2")
    strict_reload_saved(rt)
    published_map = rt._runtime_route_snapshot_payload["proxy_map"]
    assert published_map["Paris-2"]["node"].address == target.address
    target.latency_ms = 5
    strict_reload_saved(rt, publish=False)
    receipt = rt._runtime_loaded_export_contract["proxy_map"]
    assert receipt["Paris-2"]["node"].address == decoy.address      # newer receipt
    assert receipt["Paris"]["node"].address == target.address
    assert rt._runtime_route_snapshot_payload["proxy_map"] is published_map
    status = rt.bridge_gateway_status()
    assert status["stale"] is False and status["effective"] == "Paris-2"
    assert (status["selected"], status["missing"], status["applied"]) == ("Paris-2", False, True)


def test_redacted_selection_is_published_redacted_with_its_node_id(runtime):
    rt = prepared(runtime)
    secret = "GatewaySecret-77"
    target = gw(f"Osaka {secret}", password=secret, address="osaka-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    strict_reload(rt, target.name)
    state, topics, work = sampled(rt)
    tick(topics, work)
    data = published(state)
    row = next(row for row in rt.bridge_node_records()["items"] if row["tier"] == "gateway")
    assert data["selected_node_id"] == row["id"] and data["selected"] == row["name"]
    assert secret not in json.dumps(data)


def test_stopped_gateway_publishes_without_the_field(runtime):
    rt = prepared(runtime)
    strict_reload(rt, "Gateway A")
    rt.core_fact = lambda: {"running": False, "pid": None, "process_generation": 0,
                            "core_generation": 8, "cleanup_pending": False}
    state, topics, work = sampled(rt, running=False)
    tick(topics, work)
    assert published(state) == {"selected": "Gateway A", "effective": None, "missing": False,
                                "applied": False, "pending": None}


def test_getter_failure_or_absence_only_omits_the_field(runtime):
    rt = prepared(runtime)
    strict_reload(rt, "Gateway A")

    def broken():
        raise RuntimeError("boom Gateway A")

    rt.bridge_gateway_selected_node_id = broken
    state, topics, work = sampled(rt)
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("tesseract.bridge.tiers")
    handler, level = Keep(level=logging.DEBUG), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        tick(topics, work)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    data = published(state)
    assert data["selected"] == "Gateway A" and data["applied"] is True
    assert "selected_node_id" not in data and topics.status["gateway.current"] == "current"
    # A data-free trace: the exception type only, never its message or a name.
    assert topics.selected_node_id_errors == 1
    assert records == ["gateway.current: selected_node_id omitted, getter raised RuntimeError"]
    rt2 = prepared(runtime)
    rt2.bridge_gateway_selected_node_id = None
    state, topics, work = sampled(rt2)
    tick(topics, work)
    assert "selected_node_id" not in published(state)


def test_a_selection_edited_between_the_two_reads_drops_the_whole_read(runtime):
    rt = prepared(runtime)
    strict_reload(rt, "Gateway A")
    real = DesktopRuntime.bridge_gateway_selected_node_id

    def edited_meanwhile():
        answer = real(rt)
        rt.config["tiers"]["exit"]["selected"] = "Gateway B"
        return answer

    rt.bridge_gateway_selected_node_id = edited_meanwhile
    state, topics, work = sampled(rt)
    topics.sample(observe)
    work.pop()()
    assert published(state) is None                         # token moved: nothing claimed
    assert topics.status["gateway.current"] in ("unknown", "stale")
