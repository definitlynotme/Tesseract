"""T-P2-H5: gateway.select by node_id (决定_第五波裁定 T-P4-C2 #1, K-10).

Selecting by name is ambiguous in three ways (T-P4-C2 report section 9): a
cross-tier duplicate is renamed ``-2`` by the renderer, a name may be
redacted for display, and an empty name falls back differently. node_id is
resolved by the runtime getter ``bridge_exit_proxy_name`` with the renderer's
own naming, which also gives the node's Gateway identity (T-GW-ID). The pick
is saved as that identity with the proxy name as its label and applied as
the member the reloaded EXIT-SELECT holds for the identity, so a name that
moves during the reload (latency order, namesakes) still selects the picked
node and never another one.

Isolated: the P2-04 synthetic runtime, recording fakes and the real
clash_formatter renderer only; no live profile, no Core, no Mihomo, no
network, no HKCU or firewall writes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import types

import pytest
import yaml

import bridge_main
import bridge_protocol as protocol
from bridge_jobs import JobExecutor
from bridge_node_commands import NodeCommands, RuntimeNodeCommandAdapter
from bridge_runtime_access import opaque_id
from gateway_selection import (GatewaySelection, clear_gateway_selection, gateway_identity,
                               read_selection, write_bound_selection, write_name_only_selection)
from models import Node, NodeStatus
from subscription import render_subscription_route
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)
from tests.test_bridge_p2_14 import (FakePort, PublicOnlyRuntime, executor, nid,  # noqa: F401
                                     run, wire_text)


ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "bridge_schema" / "fixtures" / "v1"
REASONS = ("unknown_node", "not_gateway_node", "node_unusable")


# ------------------------------------------------------------------ protocol

def req(args):
    return {"t": "req", "epoch": "p2h5", "produced_at_ms": 1, "sent_at_ms": 2, "id": 1,
            "req_id": "q-1", "owner": "page-1", "cmd": "gateway.select", "args": args,
            "from": "page"}


def test_p2h5_fixtures_are_new_files_and_validate():
    valid = sorted((V1 / "valid").glob("p2h5_*.json"))
    invalid = sorted((V1 / "invalid").glob("p2h5_*.json"))
    assert len(valid) == 5 and len(invalid) == 3
    for path in valid:
        assert protocol.validate_frame(json.loads(path.read_bytes())) == [], path.name
    for path in invalid:
        case = json.loads(path.read_bytes())
        assert case["expected"] in protocol.validate_frame(case["frame"]), path.name


def test_node_id_and_proxy_name_together_are_invalid_type():
    both = req({"node_id": "a" * 24, "proxy_name": "Tokyo"})
    assert bridge_main.request_error_code(protocol.validate_frame(both)) == "invalid_type"
    assert JobExecutor._args_error("gateway.select", both["args"]) == "invalid_type"
    # Neither is still the old answer for a request without proxy_name.
    assert bridge_main.request_error_code(protocol.validate_frame(req({}))) == "missing_field"
    assert JobExecutor._args_error("gateway.select", {}) == "missing_field"
    assert JobExecutor._args_error("gateway.select", {"node_id": 7}) == "invalid_type"


def test_old_requests_stay_valid():
    for args in ({"proxy_name": "Tokyo"}, {"proxy_name": ""}, {"node_id": "a" * 24}):
        assert protocol.validate_frame(req(args)) == [], args
        assert JobExecutor._args_error("gateway.select", args) is None
    for name in ("command_gateway.select.json", "result_gateway.select.json",
                 "p4c2_command_gateway.select_deactivate.json"):
        assert protocol.validate_frame(json.loads((V1 / "valid" / name).read_bytes())) == []


def test_new_reason_codes_have_zh_cn_text():
    catalog = json.loads((ROOT / "ui/src/i18n/locales/zh_CN/errors.json").read_text(encoding="utf-8"))
    for reason in REASONS:
        entry = catalog[f"errors.gateway.{reason}"]
        assert entry["m"] and entry["_desc"]


# ------------------------------------------------------------ runtime getter

def gw(name, **fields):
    defaults = dict(protocol="trojan", tier="exit", address="gw.example.invalid", port=443,
                    password="GatewayPw-1", status=NodeStatus.ALIVE, latency_ms=30)
    defaults.update(fields)
    return Node(name=name, **defaults)


def other(name, tier, **fields):
    defaults = dict(protocol="trojan", tier=tier, address=f"{tier}.example.invalid", port=443,
                    password=f"{tier}-pw", status=NodeStatus.ALIVE, latency_ms=20)
    defaults.update(fields)
    return Node(name=name, **defaults)


def exportable(rt, *, tier3_limit=20):
    """The runtime state the export view reads (no overlay, no holds)."""
    rt._runtime_audit_export_overlay_lock = threading.Lock()
    rt._runtime_audit_export_excluded_identities = {}
    rt._runtime_failure_monitor_lock = threading.Lock()
    rt._runtime_failback_export_holds = set()
    rt._last_synced_tier3_nodes = []
    rt._tier3_core_export_override_lock = threading.Lock()
    rt._tier3_core_export_override_nodes = []
    rt._mihomo_tier3_export_limit = lambda: tier3_limit
    return rt


def ids(rt):
    return {(row["tier"], row["name"]): row["id"] for row in rt.bridge_node_records()["items"]}


def node_id(rt, tier, node):
    identity = (node.protocol, node.address, node.port, node.uuid, node.password, node.method,
                node.security, node.network, node.host, node.path, node.sni, node.plugin)
    return opaque_id(rt._runtime_generation, tier, identity)


def bound(node, label):
    """What a node_id pick saves: the node's identity, its proxy name as label."""
    return GatewaySelection("bound", label, identity=gateway_identity(node))


def save_selection(rt, selection):
    """Save a Gateway selection in tiers.exit as config_store.set_exit_selection
    does and let the manager read it back as ProxyTierManager.active_exit_selection
    does (read_selection over the saved block). ``selection`` is a
    GatewaySelection or a name saved without identity (an older build's pick
    or a proxy_name pick; "" = no Gateway)."""
    if not isinstance(selection, GatewaySelection):
        name = str(selection or "").strip()
        selection = GatewaySelection("legacy", name) if name else GatewaySelection("none")
    block = rt.config["tiers"]["exit"]
    if selection.state == "bound":
        write_bound_selection(block, selection.label, selection.identity)
    elif selection.state == "legacy":
        write_name_only_selection(block, selection.label)
    else:
        clear_gateway_selection(block)
    rt.manager.active_exit_selection = lambda: read_selection(rt.config["tiers"]["exit"])
    rt.manager.active_exit_proxy_name = lambda: read_selection(rt.config["tiers"]["exit"]).label
    return read_selection(block)


def renderable(rt):
    """The manager inputs the renderer reads besides the nodes."""
    manager = rt.manager
    manager.testing_config = {}
    manager.client_config = rt.config["client"]
    manager.logger = logging.getLogger("tests.bridge_h5")
    return rt


def rendered_exit_select(rt, selection, tier3_limit):
    """EXIT-SELECT of the real renderer for this saved selection, plus its proxies."""
    renderable(rt)
    save_selection(rt, selection)
    view = rt._manager_for_mihomo_runtime_config()
    _, payload = render_subscription_route(view, "/clash/config", {"tier3_limit": tier3_limit})
    document = yaml.safe_load(payload)
    group = next(g for g in document["proxy-groups"] if g["name"] == "EXIT-SELECT")
    return group["proxies"], {proxy["name"]: proxy for proxy in document["proxies"]}


def assert_renderer_agrees(rt, name, node, tier3_limit=20):
    """The pick saved as ``node``'s identity (label ``name``, the getter's
    proxy name) renders EXIT-SELECT [name], and that proxy is ``node``."""
    members, proxies = rendered_exit_select(rt, bound(node, name), tier3_limit)
    assert members == [name]  # not ["REJECT"]: the name is in Mihomo's EXIT-SELECT
    proxy = proxies[name]
    assert (proxy["server"], proxy["port"], proxy["password"]) == (
        node.address, node.port, node.password)
    assert proxy["dialer-proxy"] == "PROXY-FALLBACK"


def test_cross_tier_duplicate_resolves_to_the_suffixed_proxy(runtime):
    rt = exportable(runtime)
    target = gw("Tokyo", address="tokyo-gw.example.invalid")
    rt.manager.runtime_nodes["tier2"] = [other("Tokyo", "tier2")]
    rt.manager.runtime_nodes["exit"] = [target]
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert dict(raw) == {"status": "ok", "proxy_name": "Tokyo-2", "display_name": "Tokyo-2",
                         "identity": gateway_identity(target)}
    assert_renderer_agrees(rt, "Tokyo-2", target)
    # The tier2 node owns the plain proxy name, but an older name-only
    # "Tokyo" is the Gateway's own name pushed to "-2" by that namesake: it
    # is served as this Gateway, never as the tier2 node.
    members, proxies = rendered_exit_select(rt, "Tokyo", 20)
    assert members == ["Tokyo-2"]
    assert proxies["Tokyo-2"]["server"] == target.address


def test_tier3_export_limit_is_the_renderers(runtime):
    """Only Tier3 nodes the renderer exports take a name before the Gateway."""
    rt = exportable(runtime, tier3_limit=2)
    # Positional Tier3 names (the pre-11-02 lane): a Tier3 "Free" takes the
    # bare name and pushes the Gateway to "Free-2".
    rt.config.setdefault("tiers", {}).setdefault("tier3", {})["verified_provider_writes"] = False
    target = gw("Free", address="free-gw.example.invalid")
    rt.manager.runtime_nodes["tier3"] = [other("A", "tier3", address="a.invalid"),
                                         other("B", "tier3", address="b.invalid"),
                                         other("Free", "tier3", address="c.invalid")]
    rt.manager.runtime_nodes["exit"] = [target]
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["status"] == "ok" and raw["proxy_name"] == "Free"
    assert raw["identity"] == gateway_identity(target)
    assert_renderer_agrees(rt, "Free", target, tier3_limit=2)
    rt._mihomo_tier3_export_limit = lambda: 3
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["proxy_name"] == "Free-2"
    assert raw["identity"] == gateway_identity(target)  # the name moved, the node did not
    assert_renderer_agrees(rt, "Free-2", target, tier3_limit=3)


def test_stable_tier3_names_never_push_the_gateway_name(runtime):
    """Verified provider writes name Tier3 members by definition (base·digest):
    the bridge replays the same naming, so it still agrees with the renderer,
    and the Gateway keeps its bare name whatever the Tier3 export holds."""
    rt = exportable(runtime, tier3_limit=3)
    rt.config.setdefault("tiers", {}).setdefault("tier3", {})["verified_provider_writes"] = True
    target = gw("Free", address="free-gw.example.invalid")
    rt.manager.runtime_nodes["tier3"] = [other("A", "tier3", address="a.invalid"),
                                         other("B", "tier3", address="b.invalid"),
                                         other("Free", "tier3", address="c.invalid")]
    rt.manager.runtime_nodes["exit"] = [target]
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["status"] == "ok" and raw["proxy_name"] == "Free"
    assert raw["identity"] == gateway_identity(target)
    assert_renderer_agrees(rt, "Free", target, tier3_limit=3)


def test_redacted_name_resolves_to_the_raw_proxy_and_displays_redacted(runtime):
    rt = exportable(runtime)
    secret = "GatewaySecret-77"
    target = gw(f"Osaka {secret}", password=secret, address="osaka-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    shown = next(row["name"] for row in rt.bridge_node_records()["items"] if row["tier"] == "gateway")
    assert secret not in shown and shown != target.name  # nodes.list shows a redacted name
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["status"] == "ok" and raw["proxy_name"] == target.name
    assert raw["display_name"] == shown
    assert raw["identity"] == gateway_identity(target) and secret not in raw["identity"]
    assert_renderer_agrees(rt, target.name, target)


def test_empty_name_resolves_to_the_protocol_address_port_proxy(runtime):
    rt = exportable(runtime)
    target = gw("", address="anon-gw.example.invalid", port=8443)
    rt.manager.runtime_nodes["exit"] = [target]
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["status"] == "ok" and raw["proxy_name"] == "trojan-anon-gw.example.invalid:8443"
    assert raw["identity"] == gateway_identity(target)
    assert_renderer_agrees(rt, raw["proxy_name"], target)


def test_getter_statuses(runtime):
    rt = exportable(runtime)
    dead = gw("Dead", address="dead.example.invalid", status=NodeStatus.DEAD)
    tier2 = other("Edge", "tier2")
    rt.manager.runtime_nodes["tier2"] = [tier2]
    rt.manager.runtime_nodes["exit"].append(dead)
    assert dict(rt.bridge_exit_proxy_name("0" * 24)) == {
        "status": "unknown", "proxy_name": None, "display_name": None, "identity": None}
    assert rt.bridge_exit_proxy_name("")["status"] == "unknown"
    assert rt.bridge_exit_proxy_name(None)["status"] == "unknown"
    assert rt.bridge_exit_proxy_name(node_id(rt, "tier2", tier2))["status"] == "not_gateway"
    # Not ALIVE/SLOW: the renderer does not export it, Mihomo has no proxy.
    unusable = rt.bridge_exit_proxy_name(node_id(rt, "exit", dead))
    assert dict(unusable) == {"status": "unusable", "proxy_name": None, "display_name": None,
                              "identity": None}
    live = rt.manager.runtime_nodes["exit"][0]
    rt.manager.runtime_generation = 99  # manager moved on: ids of generation 7 are stale
    assert rt.bridge_exit_proxy_name(node_id(rt, "exit", live))["status"] == "stale"
    rt.manager.runtime_generation = 7
    rt.manager = None
    assert rt.bridge_exit_proxy_name(node_id(rt, "exit", live))["status"] == "unknown"


@pytest.mark.parametrize("variant", ["username", "grpc_service"])
def test_one_node_id_carrying_several_identities_is_unusable(runtime, variant):
    """A Node.id hashes 12 node fields; the username of a multiplexed
    residential Gateway and the gRPC service name are not among them, yet
    they choose the exit. An id that stands for several Gateway identities
    cannot say which node was picked: unusable, never the first of them."""
    rt = exportable(runtime)
    if variant == "username":
        first, second = (gw(f"Resi {country}", protocol="http", address="resi.example.invalid",
                            port=8000, password="ResiPw-1", extra={"username": f"u-country-{country}"})
                         for country in ("us", "jp"))
    else:
        first, second = (gw(f"Grpc {service}", protocol="vless", uuid="GrpcUuid-1", password=None,
                            address="grpc.example.invalid", network="grpc",
                            extra={"serviceName": service})
                         for service in ("a", "b"))
    rt.manager.runtime_nodes["exit"] = [first, second]
    assert node_id(rt, "exit", first) == node_id(rt, "exit", second)
    assert gateway_identity(first) != gateway_identity(second)
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", first))
    assert dict(raw) == {"status": "unusable", "proxy_name": None, "display_name": None,
                         "identity": None}
    # Also when only one of them is exported: the id may stand for the other
    # (it is listed too), so the exported one is never assumed.
    first.status = NodeStatus.DEAD
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", second))
    assert dict(raw) == {"status": "unusable", "proxy_name": None, "display_name": None,
                         "identity": None}
    # Alone, the same node is ok with its own identity, and that pick renders it.
    rt.manager.runtime_nodes["exit"] = [second]
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", second))
    assert raw["status"] == "ok" and raw["identity"] == gateway_identity(second)
    members, proxies = rendered_exit_select(rt, bound(second, raw["proxy_name"]), 20)
    assert members == [raw["proxy_name"]]
    assert proxies[raw["proxy_name"]]["server"] == second.address


@pytest.mark.parametrize("name", [" Tokyo ", "   ", "Tokyo\t"])
def test_getter_refuses_a_proxy_name_with_outer_whitespace(runtime, name):
    """Fix round 1 MUST-1: parser/renderer keep outer whitespace, but the
    runtime strips the saved selection, so EXIT-SELECT would be [REJECT]."""
    rt = exportable(runtime)
    target = gw(name, address="ws-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert dict(raw) == {"status": "unusable", "proxy_name": None, "display_name": None,
                         "identity": None}
    # Why: the stripped name the runtime would save is not what Mihomo has
    # (an all-whitespace name strips to "": no Gateway at all).
    if name.strip():
        members, _ = rendered_exit_select(rt, name.strip(), 20)
        assert members == ["REJECT"]
    else:
        with pytest.raises(StopIteration):  # no EXIT-SELECT group rendered
            rendered_exit_select(rt, "", 20)


def test_getter_answers_stale_when_the_service_stops_mid_read(runtime):
    """Fix round 1 S4: the view reads self.manager, not the owner snapshot."""
    rt = exportable(runtime)
    manager = rt.manager
    live = manager.runtime_nodes["exit"][0]
    build = rt._manager_for_mihomo_runtime_config

    def stopped_meanwhile():
        rt.manager = None
        return build()  # RuntimeError: manager service is not running
    rt._manager_for_mihomo_runtime_config = stopped_meanwhile
    assert rt.bridge_exit_proxy_name(node_id(rt, "exit", live))["status"] == "stale"
    # Still the current owner: a real failure is not hidden.
    rt.manager = manager

    def broken():
        raise RuntimeError("view failed")
    rt._manager_for_mihomo_runtime_config = broken
    with pytest.raises(RuntimeError, match="view failed"):
        rt.bridge_exit_proxy_name(node_id(rt, "exit", live))


def real_order(rt):
    """The real ProxyTierManager.get_nodes_for_subscription (sort_nodes_for_use
    for exit, subscription_nodes_for_tier) on the synthetic manager."""
    from manager import ProxyTierManager
    from tier3_pool_order import sort_tier3_nodes
    manager = rt.manager
    manager._tier3_enabled = lambda: True
    manager._sort_tier3_nodes = sort_tier3_nodes
    manager.get_nodes_for_subscription = types.MethodType(
        ProxyTierManager.get_nodes_for_subscription, manager)
    return rt


def test_real_manager_order_decides_the_suffix(runtime):
    """Fix round 1 SHOULD-3: two same-named Gateway nodes, real latency order.
    The suffix follows the order, the identity does not: only the identity
    tells the twins apart (saved as a name alone, either could be meant)."""
    rt = real_order(exportable(runtime))
    target = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["exit"] = [target, decoy]  # list order would name target "Paris"
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["status"] == "ok" and raw["proxy_name"] == "Paris-2"
    assert raw["identity"] == gateway_identity(target) != gateway_identity(decoy)
    assert_renderer_agrees(rt, "Paris-2", target)
    # The name alone names a render position of two Gateways: fail closed.
    members, _ = rendered_exit_select(rt, "Paris-2", 20)
    assert members == ["REJECT"]
    # Latency changes reorder them: the same node now owns the plain name.
    target.latency_ms = 5
    raw = rt.bridge_exit_proxy_name(node_id(rt, "exit", target))
    assert raw["proxy_name"] == "Paris" and raw["identity"] == gateway_identity(target)
    assert_renderer_agrees(rt, "Paris", target)
    # The pick saved before the reorder (label "Paris-2") follows the node.
    members, proxies = rendered_exit_select(rt, bound(target, "Paris-2"), 20)
    assert members == ["Paris"] and proxies["Paris"]["server"] == target.address


def test_getter_is_read_only_and_holds_no_lock_over_io(runtime):
    rt = exportable(runtime)
    rt.select_mihomo_proxy = lambda *a, **k: pytest.fail("Mihomo write")
    rt.write_mihomo_runtime_config = lambda *a, **k: pytest.fail("config write")
    before = [node.clone() for node in rt.manager.runtime_nodes["exit"]]
    raw = rt.bridge_exit_proxy_name(ids(rt)[("gateway", "Gateway A")])
    assert raw["status"] == "ok" and raw["proxy_name"] == "Gateway A"
    assert raw["identity"] == gateway_identity(before[0])
    assert [vars(node) for node in rt.manager.runtime_nodes["exit"]] == [vars(n) for n in before]
    assert rt.api_client.calls == []  # ForbiddenAPI: no controller request


# ------------------------------------------------------------- the command

GW_ID = "c" * 24
GW_IDENTITY = "v1:" + "d" * 64  # the getter's identity of the GW_ID node (runtime only)


def picked(name, identity, *, member=None):
    """DesktopRuntime.set_exit_selection's answer after a confirmed strict
    reload: ``member`` (default: the label) is the loaded EXIT-SELECT member
    it chose for the saved identity and PUT (the echo in ``selection``)."""
    member = name if member is None else member
    return {"selected": name, "selection_key": "id:" + identity, "member": member,
            "core_running": True, "applied": True,
            "selection": {"group": "EXIT-SELECT", "selected": member}}


class H5Port(FakePort):
    def __init__(self):
        super().__init__()
        self.proxy_names = {}
        self.proxy_error = None
        self.saved_key = None  # persisted selection key; None = unreadable
        self.display_names = {}
        self.select_node = picked

    def exit_proxy_name(self, node_id):
        self.calls.append(("exit_proxy_name", node_id))
        if self.proxy_error is not None:
            raise self.proxy_error
        return self.proxy_names.get(node_id, {"status": "unknown", "proxy_name": None,
                                             "display_name": None, "identity": None})

    def set_exit_selection(self, name, identity):
        self.calls.append(("set_exit_selection", name, identity))
        return self.select_node(name, identity)

    def persisted_exit_selection_key(self):
        if isinstance(self.saved_key, Exception):
            raise self.saved_key
        return self.saved_key

    def display_proxy_name(self, raw):
        self.calls.append(("display_proxy_name", raw))
        return self.display_names.get(raw)


@pytest.fixture
def h5port():
    port = H5Port()
    port.proxy_names[GW_ID] = {"status": "ok", "proxy_name": "Osaka Pw-Secret",
                               "display_name": "Osaka [REDACTED]", "identity": GW_IDENTITY}
    return port


@pytest.fixture
def h5handler(h5port, executor):
    instance = NodeCommands(h5port)
    executor.register("gateway.select", instance.gateway_select)
    return instance


def test_node_id_selects_the_resolved_proxy_and_reports_the_display_name(executor, h5handler, h5port):
    """The pick is saved as the node's identity with the resolved proxy name
    as its label (never by the name alone), and the node is resolved once."""
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert h5port.called("set_exit_selection") == [
        ("set_exit_selection", "Osaka Pw-Secret", GW_IDENTITY)]
    assert not h5port.called("set_exit_selected_proxy")
    assert h5port.called("exit_proxy_name") == [("exit_proxy_name", GW_ID)]
    assert job["state"] == "done"
    assert job["result"] == {"selected": "Osaka [REDACTED]", "applied": True, "apply_error": None,
                             "selection": "Osaka [REDACTED]", "pending": None}
    assert "Pw-Secret" not in wire_text(job) and GW_IDENTITY not in wire_text(job)


@pytest.mark.parametrize("status, reason", [
    ("unknown", "unknown_node"), ("stale", "unknown_node"),
    ("not_gateway", "not_gateway_node"), ("unusable", "node_unusable"),
    ("surprise", "unknown_node")])
def test_unresolved_node_id_is_conflict_with_reason_and_writes_nothing(
        executor, h5handler, h5port, status, reason):
    h5port.proxy_names[GW_ID] = {"status": status, "proxy_name": None, "display_name": None,
                                 "identity": None}
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["result"] is None
    assert job["error"] == {"code": "conflict", "message_key": f"errors.gateway.{reason}",
                            "params": {}, "reason_code": reason}
    assert not h5port.called("set_exit_selected_proxy")
    assert not h5port.called("set_exit_selection")


def test_missing_runtime_getter_or_malformed_id_is_unknown_node(executor, h5handler, h5port):
    job = run(executor, "gateway.select", {"node_id": "not-an-opaque-id"})
    assert job["error"]["reason_code"] == "unknown_node"
    assert not h5port.called("exit_proxy_name")  # rejected before any runtime read
    adapter = RuntimeNodeCommandAdapter(PublicOnlyRuntime())
    assert adapter.exit_proxy_name(GW_ID) is None  # no such public getter: unknown
    h5port.proxy_names[GW_ID] = None
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["error"]["reason_code"] == "unknown_node"
    assert not h5port.called("set_exit_selected_proxy")
    assert not h5port.called("set_exit_selection")


def test_getter_failure_or_incomplete_answer_is_internal(executor, h5handler, h5port):
    h5port.proxy_error = RuntimeError("C:/private/runtime detail")
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["error"]["code"] == "internal"
    assert "private" not in wire_text(job)
    h5port.proxy_error = None
    h5port.proxy_names[GW_ID] = {"status": "ok", "proxy_name": "", "display_name": "",
                                 "identity": GW_IDENTITY}
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["error"]["code"] == "internal"
    # A name without a v1 identity cannot be saved as the picked node.
    for identity in (None, "", "v2:" + "d" * 64, "d" * 64, 7):
        h5port.proxy_names[GW_ID] = {"status": "ok", "proxy_name": "Osaka",
                                     "display_name": "Osaka", "identity": identity}
        job = run(executor, "gateway.select", {"node_id": GW_ID})
        assert job["state"] == "failed" and job["error"]["code"] == "internal", identity
    h5port.proxy_names[GW_ID] = {"status": "ok", "proxy_name": "Osaka", "display_name": "Osaka"}
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["error"]["code"] == "internal"
    assert not h5port.called("set_exit_selected_proxy")
    assert not h5port.called("set_exit_selection")


def test_both_arguments_reaching_the_handler_are_validation(h5handler, h5port):
    from bridge_jobs import JobFailure
    with pytest.raises(JobFailure) as caught:
        h5handler.gateway_select({"node_id": GW_ID, "proxy_name": "Osaka"}, None)
    assert caught.value.code == "validation"
    assert h5port.calls == []


def test_failures_after_resolution_never_echo_the_raw_name(executor, h5handler, h5port):
    def controller_down(name, identity):
        raise ConnectionError("controller down")
    h5port.select_node = controller_down
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["selected"] == "Osaka [REDACTED]" and job["result"]["selection"] is None
    assert job["result"]["apply_error"]["reason_code"] == "apply_failed"
    # Mihomo reads back another member: never echoed raw, selection unknown.
    h5port.select_node = picked
    h5port.readback = "Other Raw Pw-Secret"
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["result"]["selection"] is None
    assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"
    assert "Pw-Secret" not in wire_text(job)


def test_node_id_with_stopped_core_is_done_with_the_saved_fact(executor, h5handler, h5port):
    """No Core: done with applied=true when the file holds this pick, i.e.
    the key "id:<identity>" the runtime just saved (effective at the next
    start; selection is the saved fact). The same label saved by name only,
    or another node under that label, is not this pick; an unreadable file
    falls back to the runtime's completed write."""
    h5port.saved_exit = "Osaka Pw-Secret"  # the label alone never confirms a node_id pick
    h5port.select_node = lambda name, identity: {
        "selected": name, "selection_key": "id:" + identity, "member": "",
        "applied": False, "core_running": False}
    h5port.saved_key = "id:" + GW_IDENTITY
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "done"
    assert job["result"]["selection"] == "Osaka [REDACTED]" and job["result"]["applied"] is True
    # No Core: nothing selected in Mihomo, nothing re-resolved (fix round 1).
    assert h5port.called("exit_proxy_name") == [("exit_proxy_name", GW_ID)]
    for saved in ("name:Osaka Pw-Secret", "id:v1:" + "0" * 64, ""):
        h5port.saved_key = saved
        job = run(executor, "gateway.select", {"node_id": GW_ID})
        assert job["state"] == "failed" and job["result"]["selection"] is None, saved
        assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"
        assert "Pw-Secret" not in wire_text(job)
    for unreadable in (None, OSError("C:/private/config.local.yaml")):
        h5port.saved_key = unreadable
        job = run(executor, "gateway.select", {"node_id": GW_ID})
        assert job["state"] == "done" and job["result"]["applied"] is True, unreadable
        assert "private" not in wire_text(job)


def test_recovery_gate_partial_carries_the_display_name(h5port):
    from bridge_jobs import JobFailure

    def closed():
        raise RuntimeError("recovery pending")
    handler = NodeCommands(h5port, recovery_gate=closed)
    with pytest.raises(JobFailure) as caught:
        handler.gateway_select({"node_id": GW_ID}, None)
    assert caught.value.code == "conflict"
    assert caught.value.partial_result["selected"] == "Osaka [REDACTED]"
    assert not h5port.called("set_exit_selected_proxy")
    assert not h5port.called("set_exit_selection")


def test_proxy_name_pick_takes_the_name_path_without_resolving_a_node(executor, h5handler, h5port):
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "done" and job["result"]["selected"] == "Gateway B"
    assert not h5port.called("exit_proxy_name")


def test_proxy_name_pick_with_core_stopped_confirms_what_the_runtime_saved(executor, h5handler, h5port):
    """Review RES-2/CT-1: a name pick may be saved under the label of the node
    the name denotes ('Tokyo' -> 'Tokyo-2', bound).  With no Core it is done
    when the saved key is the runtime's, and reports the saved label."""
    key = "id:" + GW_IDENTITY
    h5port.select = lambda name: {"selected": "Tokyo-2", "selection_key": key, "member": "",
                                  "core_running": False, "applied": False}
    h5port.saved_key = key
    job = run(executor, "gateway.select", {"proxy_name": "Tokyo"})
    assert job["state"] == "done", job
    assert job["result"]["selected"] == "Tokyo" and job["result"]["selection"] == "Tokyo-2"
    assert job["result"]["applied"] is True

    h5port.saved_key = "name:Tokyo"          # the file holds another selection
    job = run(executor, "gateway.select", {"proxy_name": "Tokyo"})
    assert job["state"] == "failed"
    assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"

    # A runtime answer without a key: compared by the saved label.
    h5port.select = lambda name: {"selected": "Tokyo-2", "core_running": False, "applied": False}
    h5port.saved_exit = "Tokyo-2"
    job = run(executor, "gateway.select", {"proxy_name": "Tokyo"})
    assert job["state"] == "done" and job["result"]["selection"] == "Tokyo-2"


def test_adapter_reads_the_persisted_selection_key():
    """Review CT-2: the real adapter's key reader (the no-Core node_id check)."""
    from types import SimpleNamespace
    from bridge_node_commands import RuntimeNodeCommandAdapter

    def adapter(local):
        def read():
            if isinstance(local, Exception):
                raise local
            return local
        return RuntimeNodeCommandAdapter(SimpleNamespace(read_local_config=read))

    bound = {"selected_proxy": "JP 01", "selected_node": {"identity": GW_IDENTITY, "label": "JP 01"}}
    assert adapter({"tiers": {"exit": bound}}).persisted_exit_selection_key() == "id:" + GW_IDENTITY
    old_build = {**bound, "selected": "JP 01-2"}        # an older build's pick wins
    assert adapter({"tiers": {"exit": old_build}}).persisted_exit_selection_key() == "name:JP 01-2"
    unresolved = {"selected_proxy": "JP 01", "selected_node": {"unresolved": "ambiguous", "label": "JP 01"}}
    assert adapter({"tiers": {"exit": unresolved}}).persisted_exit_selection_key() == "unresolved:JP 01"
    assert adapter({"tiers": {"exit": "garbage"}}).persisted_exit_selection_key() == ""
    assert adapter({"tiers": {}}).persisted_exit_selection_key() == ""
    with pytest.raises(OSError):
        adapter(OSError("unreadable")).persisted_exit_selection_key()


@pytest.mark.parametrize("name", [" Tokyo ", "   ", "Tokyo\n"])
def test_resolved_name_with_outer_whitespace_is_node_unusable(executor, h5handler, h5port, name):
    """Fix round 1 MUST-1, sidecar guard (also when a getter answers ok)."""
    h5port.proxy_names[GW_ID] = {"status": "ok", "proxy_name": name, "display_name": "Tokyo",
                                 "identity": GW_IDENTITY}
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["result"] is None
    assert job["error"]["code"] == "conflict" and job["error"]["reason_code"] == "node_unusable"
    assert not h5port.called("set_exit_selected_proxy")
    assert not h5port.called("set_exit_selection")


@pytest.mark.parametrize("case", ["member_unavailable", "readback_mismatch", "echo_mismatch"])
def test_runtime_member_unavailable_or_readback_mismatch_is_selection_unconfirmed(
        executor, h5handler, h5port, case):
    """Replaces test_name_moved_by_the_reload_is_selection_unconfirmed (fix
    round 1 S1/SHOULD-1). That test re-resolved the node after the reload,
    because EXIT-SELECT could then hold another node under the saved name.
    The pick is now saved as the node's identity and the runtime selects the
    member the reloaded EXIT-SELECT holds for that identity; what is still
    protected is the verdict whenever that member is not confirmed: the node
    is not exported any more (gateway_member_unavailable: EXIT-SELECT fails
    closed, nothing is put into it), or Mihomo's readback or the PUT echo is
    another proxy. Each is io/selection_unconfirmed with selection null, the
    raw names stay off the wire and the node is not resolved a second time."""
    raw = "Osaka Pw-Secret"
    if case == "member_unavailable":
        h5port.select_node = lambda name, identity: {
            "selected": name, "selection_key": "id:" + identity, "member": "",
            "core_running": True, "applied": False, "apply_error": "gateway_member_unavailable"}
    elif case == "readback_mismatch":
        h5port.readback = raw + "-2"  # Mihomo holds another proxy than the member
    else:
        # The runtime chose "-2" for the identity but the PUT went to the label.
        h5port.select_node = lambda name, identity: dict(
            picked(name, identity, member=name + "-2"),
            selection={"group": "EXIT-SELECT", "selected": name})
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert h5port.called("set_exit_selection") == [("set_exit_selection", raw, GW_IDENTITY)]
    assert h5port.called("exit_proxy_name") == [("exit_proxy_name", GW_ID)]
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["applied"] is False and job["result"]["selection"] is None
    assert job["result"]["selected"] == "Osaka [REDACTED]"
    assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"
    assert not h5port.called("display_proxy_name")
    assert "Pw-Secret" not in wire_text(job) and GW_IDENTITY not in wire_text(job)


def test_member_moved_by_the_reload_is_applied_under_its_display_name(executor, h5handler, h5port):
    """The reload can load the picked node under another proxy name (a latency
    reorder, a namesake leaving). The runtime selects that member for the
    saved identity: the pick is applied, and selected and selection are the
    member's display name (UUIDs redacted as in nodes.list), never raw."""
    moved = "Osaka Pw-Secret-2"
    h5port.select_node = lambda name, identity: picked(name, identity, member=moved)
    h5port.display_names[moved] = "Osaka [REDACTED]-2 123e4567-e89b-12d3-a456-426614174000"
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "done"
    assert job["result"] == {"selected": "Osaka [REDACTED]-2 [REDACTED]", "applied": True,
                             "apply_error": None, "selection": "Osaka [REDACTED]-2 [REDACTED]",
                             "pending": None}
    assert h5port.called("display_proxy_name") == [("display_proxy_name", moved)]
    assert h5port.called("exit_proxy_name") == [("exit_proxy_name", GW_ID)]
    assert "Pw-Secret" not in wire_text(job)
    # A readback of that member confirms it the same way.
    h5port.readback = moved
    assert run(executor, "gateway.select", {"node_id": GW_ID})["state"] == "done"
    # A member the runtime cannot display is never put on the wire raw.
    h5port.display_names.clear()
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed" and job["error"]["code"] == "internal"
    assert "Pw-Secret" not in wire_text(job)


def test_unchanged_name_after_the_reload_is_done(executor, h5handler, h5port):
    """The member the runtime selected is the name resolved before the
    reload: done under the getter's display name. Nothing is re-resolved
    after the reload any more (the runtime answers the member it chose for
    the saved identity), applied or not: one getter call per pick."""
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "done" and job["result"]["applied"] is True
    assert job["result"]["selected"] == job["result"]["selection"] == "Osaka [REDACTED]"
    assert h5port.called("exit_proxy_name") == [("exit_proxy_name", GW_ID)]
    assert not h5port.called("display_proxy_name")
    # A failed apply is not re-resolved either (nothing to confirm).
    h5port.calls.clear()
    h5port.select_node = lambda name, identity: {"selected": name, "member": name,
                                                 "applied": False, "core_running": True}
    job = run(executor, "gateway.select", {"node_id": GW_ID})
    assert job["state"] == "failed"
    assert h5port.called("exit_proxy_name") == [("exit_proxy_name", GW_ID)]


# ------------------------------------- end to end: real getter, real adapter

def reloadable(rt):
    """exportable() plus what a strict reload's loaded-contract commit calls;
    a runtime config file write or a Mihomo PUT fails the test (the stubs of
    tests/test_bridge_h8.prepared)."""
    rt = renderable(exportable(rt))
    rt._wake_parked_tier3_pool_sync_if_causal_key_changed = lambda: None
    rt.write_mihomo_runtime_config = lambda *a, **k: pytest.fail("config write")
    rt.select_mihomo_proxy = lambda *a, **k: pytest.fail("Mihomo write")
    return rt


def reload_saved_selection(rt):
    """The render and loaded-contract commit of one strict reload for the
    selection saved in rt.config, writing nothing (tests/test_bridge_h8's
    strict reload without its synthetic route publication): the real
    renderer with its receipt, the real receipt -> proxy map -> export
    contract -> loaded commit. Returns the document Core loads."""
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
    return yaml.safe_load(payload)


def exit_group(document):
    return next(g for g in document["proxy-groups"] if g["name"] == "EXIT-SELECT")["proxies"]


class CountingAdapter(RuntimeNodeCommandAdapter):
    """The real adapter; records each node_id resolution (getter call)."""

    def __init__(self, runtime):
        super().__init__(runtime)
        self.resolved = []

    def exit_proxy_name(self, node_id):
        self.resolved.append(node_id)
        return super().exit_proxy_name(node_id)


def through_the_adapter(rt, executor, node_id_value, during=None):
    """gateway.select{node_id} through the real adapter, getter and display
    filter. rt.set_exit_selection stands in for DesktopRuntime's: it saves
    label and identity as config_store.set_exit_selection does, lets
    ``during`` change what the reload sees, renders and loads the strict
    reload (reload_saved_selection) and answers as the runtime does after
    its PUTs: the member the loaded EXIT-SELECT holds for the saved
    selection (rt._loaded_gateway_member), else gateway_member_unavailable."""
    pick = types.SimpleNamespace(sent=[], loaded=[], by_name=[])

    def set_exit_selection(*, proxy_name, identity=None):
        label = str(proxy_name or "").strip()  # as DesktopRuntime.set_exit_selection
        pick.sent.append((proxy_name, identity))
        selection = save_selection(
            rt, GatewaySelection("bound", label, identity=identity) if identity else label)
        if during is not None:
            during()  # what changes while the runtime reloads
        document = reload_saved_selection(rt)
        pick.loaded.append(document)
        member = rt._loaded_gateway_member(rt._gateway_selection(), members=exit_group(document))
        answer = {"selected": label, "selection_key": selection.key, "member": member,
                  "core_running": True, "applied": bool(member)}
        if member:
            answer["selection"] = {"group": "EXIT-SELECT", "selected": member}  # the PUT echo
        else:
            answer["apply_error"] = "gateway_member_unavailable"
        return answer

    def set_exit_selected_proxy(proxy_name):
        pick.by_name.append(proxy_name)  # a node_id pick never saves a name alone
        return {"selected": proxy_name, "applied": False, "core_running": True}
    rt.set_exit_selection = set_exit_selection
    rt.set_exit_selected_proxy = set_exit_selected_proxy
    adapter = CountingAdapter(rt)
    handler = NodeCommands(adapter)
    executor.register("gateway.select", handler.gateway_select)
    pick.job = run(executor, "gateway.select", {"node_id": node_id_value})
    pick.resolved = adapter.resolved
    return pick


@pytest.mark.parametrize("case", ["cross_tier_duplicate", "redacted", "empty_name"])
def test_node_id_selects_the_one_correct_proxy(runtime, executor, case):
    rt = reloadable(runtime)
    secret = "GatewaySecret-88"
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1")
    if case == "cross_tier_duplicate":
        target = gw("Paris", address="paris-gw.example.invalid")
        rt.manager.runtime_nodes["tier1"] = [other("Paris", "tier1")]
        rt.manager.runtime_nodes["exit"] = [decoy, target]
        expected = "Paris-3"  # tier1 "Paris", decoy "Paris-2"
    elif case == "redacted":
        target = gw(f"Paris {secret}", address="paris-gw.example.invalid", password=secret)
        rt.manager.runtime_nodes["exit"] = [decoy, target]
        expected = target.name
    else:
        target = gw("", address="paris-gw.example.invalid")
        rt.manager.runtime_nodes["exit"] = [decoy, target]
        expected = "trojan-paris-gw.example.invalid:443"
    pick = through_the_adapter(rt, executor, node_id(rt, "exit", target))
    assert pick.sent == [(expected, gateway_identity(target))] and pick.by_name == []
    assert pick.resolved == [node_id(rt, "exit", target)]
    job = pick.job
    assert job["state"] == "done" and job["result"]["applied"] is True
    assert secret not in wire_text(job) and gateway_identity(target) not in wire_text(job)
    if case == "redacted":
        assert job["result"]["selected"] != target.name
    # The reloaded EXIT-SELECT holds exactly that proxy, and it is the target.
    (document,) = pick.loaded
    assert exit_group(document) == [expected]
    assert_renderer_agrees(rt, expected, target)
    # The page's display name (nodes.list) would not have been that proxy.
    shown = {row["id"]: row["name"] for row in rt.bridge_node_records()["items"]}
    assert shown[node_id(rt, "exit", target)] != expected


@pytest.mark.parametrize("name", [" Paris ", "   "])
def test_whitespace_named_gateway_is_never_written(runtime, executor, name):
    """Fix round 1 MUST-1 end to end: the runtime would save "Paris" (or "",
    deactivating the Gateway) while Mihomo only has the unstripped name."""
    rt = reloadable(runtime)
    target = gw(name, address="ws-gw.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    pick = through_the_adapter(rt, executor, node_id(rt, "exit", target))
    assert pick.sent == [] and pick.by_name == []
    assert pick.job["state"] == "failed" and pick.job["error"]["reason_code"] == "node_unusable"


def test_reorder_during_the_reload_applies_the_picked_node(runtime, executor):
    """Replaces test_reorder_during_the_reload_is_not_reported_applied (fix
    round 1 S1/SHOULD-1, end to end with the real exit order). The reload
    re-sorts the same-named Gateway nodes, so "Paris-2" (resolved for the
    target) becomes the decoy's name. The replaced test made sure the job
    never reported the decoy as the applied pick. The pick is now saved as
    the target's identity, so the reloaded EXIT-SELECT holds the target under
    its new name and that member is selected: applied, for the node that was
    picked, shown under its new name, and the decoy is never selected."""
    rt = real_order(reloadable(runtime))
    target = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["exit"] = [target, decoy]
    target_id = node_id(rt, "exit", target)

    def reload_reorders():
        target.latency_ms, decoy.latency_ms = 10, 50
    pick = through_the_adapter(rt, executor, target_id, reload_reorders)
    assert pick.sent == [("Paris-2", gateway_identity(target))]  # the name at pick time: a label
    assert pick.by_name == []
    assert pick.resolved == [target_id]  # resolved once, never again after the reload
    job = pick.job
    assert job["state"] == "done" and job["result"]["applied"] is True
    assert job["result"]["apply_error"] is None
    shown = rt.bridge_display_proxy_name("Paris")
    assert job["result"]["selected"] == job["result"]["selection"] == shown == "Paris"
    # Core's EXIT-SELECT holds the target under its new name, not the decoy.
    (document,) = pick.loaded
    assert exit_group(document) == ["Paris"]
    proxies = {proxy["name"]: proxy for proxy in document["proxies"]}
    assert (proxies["Paris"]["server"], proxies["Paris"]["password"]) == (
        target.address, target.password)
    assert proxies["Paris-2"]["server"] == decoy.address
    loaded = rt._runtime_loaded_export_contract["proxy_map"]
    assert loaded["Paris"]["node"].address == target.address
    assert loaded["Paris"]["selected_for"] == "id:" + gateway_identity(target)
    text = wire_text(job)
    for secret in (target.password, decoy.password, gateway_identity(target)):
        assert secret not in text
