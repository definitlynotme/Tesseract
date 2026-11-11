"""T-P4-C2: v1 contract additions that may follow the first release (no proto bump).

Items: 1 exit geography (G07), 2 identity trigger, 3 node endpoint / last
check / tier check interval (K-1, K-2), 4 slow (K-5), 5 protocol chain (K-3),
6 subscription recheck progress and refresh time (K-6), 7 pending count per
tier (G33), 8 diagnose protocol (K-9, not added), 9 Gateway deactivation and
selection semantics (K-10), 10 update notes / enabled / last check (R-09),
11 geo items (R-11), 12 logs.feed reason (logs R6), 13 import groups (G38).

Isolated: recording fakes and synthetic runtimes only; no live profile, no
network, no HKCU or firewall writes, no Mihomo.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge_exit as bx
import bridge_protocol as protocol
from bridge_nodes import NodeProjection, node_detail_extras
from bridge_runtime_access import (RuntimeAccess, geo_items, mask_host, note_node_pending,
                                   opaque_id)
from models import Node, NodeStatus
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)
from tests.test_bridge_p2_06 import facts, first
from tests.test_bridge_p2_14 import executor, handler, port, run  # noqa: F401  (fixtures)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema" / "fixtures"
V1_VALID, V1_INVALID = FIXTURES / "v1" / "valid", FIXTURES / "v1" / "invalid"
DOT = "•"
T0 = 1790812800000


def res_frame(cmd, data):
    return {"t": "res", "epoch": "p4c2", "produced_at_ms": 1, "sent_at_ms": 2, "id": 1,
            "req_id": "q-1", "owner": "page-1", "cmd": cmd, "ok": True, "data": data}


# ------------------------------------------------------------------ fixtures

def test_p4c2_fixtures_are_new_files_and_validate():
    valid = sorted(V1_VALID.glob("p4c2_*.json"))
    invalid = sorted(V1_INVALID.glob("p4c2_*.json"))
    assert len(valid) >= 11 and len(invalid) >= 18
    for path in valid:
        assert protocol.validate_frame(json.loads(path.read_bytes())) == [], path.name
    for path in invalid:
        case = json.loads(path.read_bytes())
        assert case["expected"] in protocol.validate_frame(case["frame"]), path.name


OLD_FIXTURES = (
    "response_nodes.detail.json", "topic_nodes.list.json", "topic_subscriptions.list.json",
    "topic_tiers.summary.json", "topic_exit.identity.json", "topic_update.state.json",
    "topic_geo.status.json", "response_geo.status.json", "result_geo.update.json",
    "topic_logs.feed.json", "result_import.preview.json", "command_gateway.select.json",
    "result_gateway.select.json", "command_nodes.diagnose.json", "hello_v1_caps.json",
)


def test_fields_absent_in_old_fixtures_still_pass():
    """Rule 1 (B10): every addition is optional; the frozen v1 fixtures stand."""
    for name in OLD_FIXTURES:
        frame = json.loads((V1_VALID / name).read_bytes())
        assert protocol.validate_frame(frame) == [], name
    p1 = json.loads((FIXTURES / "valid" / "topic_tiers_summary.json").read_bytes())
    assert protocol.validate_frame(p1) == []


def test_every_new_field_is_optional_in_the_schema():
    optional = {
        "IdentityLeg": {"city?", "timezone?", "asn?", "org?", "type?", "single_source?"},
        "IdentityResult": {"trigger?"},
        "Node": {"slow?", "last_checked_at_ms?"},
        "Subscription": {"recheck?", "last_refresh_at_ms?"},
        "Geo": {"items?"},
        "Update": {"notes?", "enabled?", "last_check_at_ms?"},
    }
    for dto, names in optional.items():
        assert names <= set(protocol.V1_DTOS[dto]), dto
    assert {"endpoint?", "protocols?"} <= set(protocol.V1_QUERY_SPECS["nodes.detail"][1])
    assert "groups?" in protocol.V1_COMMAND_SPECS["import.preview"][5]
    assert "reason?" in protocol.V1_TOPIC_SPECS["logs.feed"][2]


def test_hello_caps_and_command_topic_sets_unchanged():
    hello = json.loads((V1_VALID / "hello_v1_caps.json").read_bytes())
    assert protocol.validate_frame(hello) == []
    assert "nodes.diagnose" in protocol.V1_COMMANDS and "gateway.select" in protocol.V1_COMMANDS


# ------------------------------------------------------- 1. exit geography

def identity_payload(*, family_state="confirmed", location=None, network=None, v6=None):
    item = {"state": family_state, "ip": "203.0.113.7", "evidence": [],
            "location": location if location is not None else {
                "country_code": "JP", "city": "Tokyo", "timezone": "Asia/Tokyo",
                "state": "reported", "source": "ident.me"}}
    if network is not None:
        item["network"] = network
    return {"schema_version": 2, "state": "complete", "checked_at": "2026-10-05T01:02:03+00:00",
            "freshness": {"state": "fresh", "reason": "manual_lookup"},
            "route": {"binding_marker": "a1b2c3d4e5f6a1b2c3d4e5f6", "core_instance": "1:1:101",
                      "confirmed": True},
            "identity": {"ipv4": item, "ipv6": v6 or {"state": "unavailable", "ip": None,
                                                       "evidence": []}},
            "diagnostics": {}}


def project_identity(raw):
    return bx.identity_result_from_payload(raw, request_id="req-1", generation=1, fallback_ms=T0)


def test_ok_leg_carries_the_source_reported_context():
    network = {"asn": "AS13335", "organisation": "Example Networks, Inc.", "type": "Hosting",
               "source": "ident.me", "source_conflict": False, "conflicts": []}
    result = project_identity(identity_payload(network=network))
    v4 = result["v4"]
    assert v4["status"] == "ok"
    assert {k: v4[k] for k in ("city", "timezone", "asn", "org", "type", "single_source")} == {
        "city": "Tokyo", "timezone": "Asia/Tokyo", "asn": "13335",
        "org": "Example Networks, Inc.", "type": "hosting", "single_source": False}
    # The failed/unknown stack never carries context.
    assert not set(protocol._LEG_CONTEXT) & set(result["v6"])
    assert protocol.validate_topic_payload("exit.identity",
                                           {"current": result, "previous": None}) == []


def test_single_source_follows_the_unconfirmed_family_state():
    result = project_identity(identity_payload(family_state="unconfirmed"))
    assert result["v4"]["single_source"] is True


def test_conflicting_location_and_unknown_values_are_not_reported():
    location = {"country_code": "JP", "city": "Tokyo", "timezone": "Asia/Tokyo",
                "state": "conflict", "conflicting_fields": ["city"]}
    network = {"asn": "ASN-x", "organisation": "  ", "type": "satellite"}
    v4 = project_identity(identity_payload(location=location, network=network))["v4"]
    assert v4["country"] is None
    assert not {"city", "timezone", "asn", "org", "type"} & set(v4)


@pytest.mark.parametrize("org", [
    "token=Zq9-fixture-secret-Pw",            # the frame scanner rejects it
    "x" * 300,                                 # over 256 characters
    "line\nbreak", "sep\u2028arator",          # control / line separators
])
def test_untrusted_org_text_is_dropped_never_sent(org):
    v4 = project_identity(identity_payload(network={"organisation": org}))["v4"]
    assert "org" not in v4
    assert protocol.validate_topic_payload("exit.identity", {"current": project_identity(
        identity_payload(network={"organisation": org})), "previous": None}) == []


def test_context_on_a_non_ok_leg_is_an_invariant_error():
    leg = {"status": "failed", "address": None, "country": None, "reason_code": "x",
           "observed_at_ms": None, "org": "Example"}
    errors = []
    protocol._schema(leg, "IdentityLeg", "leg", errors)
    assert "invariant:leg.context" in errors


# ---------------------------------------------------------- 2. trigger

def test_manual_result_carries_trigger_and_previous_keeps_it():
    from bridge_jobs import JobContext
    from tests.test_bridge_p2_16 import FakePort, make_owner, payload
    port = FakePort()
    port.results.append(payload(v4="203.0.113.7"))
    port.results.append(payload(v4="203.0.113.8"))
    owner, published = make_owner(port)
    job_result = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert "trigger" not in job_result                     # job results do not carry it
    assert owner.snapshot()["current"]["trigger"] == "manual"
    owner.verify({"trigger": "manual"}, JobContext("job-2"))
    snap = owner.snapshot()
    assert snap["current"]["trigger"] == snap["previous"]["trigger"] == "manual"
    for dto in published:
        assert protocol.validate_topic_payload("exit.identity", dto) == []


def test_automatic_result_carries_trigger_auto():
    from tests.test_bridge_p2_17 import Clock, ExitPort, make_owner, payload, prime
    clock = Clock()
    port = ExitPort()
    port.results.append(payload(v4="198.51.100.9"))
    owner, published = make_owner(port, clock)
    assert prime(owner, clock).start
    assert owner.snapshot()["current"]["trigger"] == "auto"
    assert published and all(dto["current"].get("trigger") == "auto" for dto in published)


def test_idle_result_has_no_trigger():
    from tests.test_bridge_p2_16 import FakePort, make_owner
    owner, _ = make_owner(FakePort())
    assert "trigger" not in owner.snapshot()["current"]


# ------------------------------------- 3/5. endpoint, protocols, intervals

@pytest.mark.parametrize("host, masked", [
    ("gw-tyo01.example.org", f"g{DOT * 3}.org"),
    ("203.0.113.7", f"203.{DOT * 3}"),
    ("2001:db8::1", f"2001:{DOT * 3}"),
    ("[2001:db8::1]", f"2001:{DOT * 3}"),
    ("localhost", f"l{DOT * 3}"),
    ("é.example.org", f"{DOT * 4}.org"),
    ("a.b.123", f"a{DOT * 3}"),
    (" ", None), ("", None), (None, None),
])
def test_mask_host_keeps_only_the_first_character_and_tld(host, masked):
    assert mask_host(host) == masked
    if masked is not None:
        assert protocol._HOST_MASK.fullmatch(masked)


def tier1_variant(key, port, *, password):
    return Node(name=f"HK-01 :: {key}", protocol="vless" if key.startswith("vless") else key,
                tier="tier1", address="hk01.example.invalid", port=port, password=password,
                status=NodeStatus.ALIVE, latency_ms=20,
                extra={"physical_node": "HK-01", "protocol_key": key,
                       "fallback_order": ["hysteria2", "vless_reality"]})


def test_runtime_endpoint_getter_lists_the_physical_chain_in_fallback_order(runtime):
    manager = runtime.manager
    manager.runtime_nodes["tier1"] = [tier1_variant("vless_reality", 443, password="PwVariantOne"),
                                      tier1_variant("hysteria2", 8443, password="PwVariantTwo")]
    records = runtime.bridge_node_records()
    ids = {row["name"]: row["id"] for row in records["items"]}
    reality = ids["HK-01 :: vless_reality"]
    raw = runtime.bridge_node_endpoint(reality)
    assert raw["stale"] is False and raw["host"] == "hk01.example.invalid" and raw["port"] == 443
    assert [dict(item) for item in raw["protocols"]] == [
        {"key": "hysteria2", "port": 8443, "node_id": ids["HK-01 :: hysteria2"]},
        {"key": "vless_reality", "port": 443, "node_id": reality}]
    # A non-variant node lists only itself.
    gateway = ids["Gateway A"]
    assert [dict(p) for p in runtime.bridge_node_endpoint(gateway)["protocols"]] == [
        {"key": "trojan", "port": 443, "node_id": gateway}]
    assert runtime.bridge_node_endpoint("0" * 24) is None
    assert runtime.bridge_node_endpoint("") is None


def test_node_detail_extras_mask_the_host_and_carry_no_credential(runtime):
    runtime.manager.runtime_nodes["tier1"] = [
        tier1_variant("vless_reality", 443, password="PwVariantOne"),
        tier1_variant("hysteria2", 8443, password="PwVariantTwo")]
    access = RuntimeAccess(runtime)
    node_id = next(row["id"] for row in runtime.bridge_node_records()["items"]
                   if row["name"] == "HK-01 :: hysteria2")
    extras = node_detail_extras(access, node_id)
    assert extras["endpoint"] == {"host_masked": f"h{DOT * 3}.invalid", "port": 8443}
    assert [p["name"] for p in extras["protocols"]] == ["hysteria2", "vless_reality"]
    wire = json.dumps(extras, ensure_ascii=False)
    for secret in ("PwVariantOne", "PwVariantTwo", "hk01.example.invalid", "hk01"):
        assert secret not in wire
    node = {"id": node_id, "name": "HK-01 :: hysteria2", "tier": "vanguard", "kind": "manual",
            "enabled": True, "status": "healthy", "reason_code": None, "protocol": "hysteria2",
            "latency_ms": 20}
    data = {"node": node, "config_item": None, "last_diagnostic": None, "check_interval_s": 30,
            **extras}
    assert protocol.validate_frame(res_frame("nodes.detail", data)) == []


def test_node_detail_extras_unknown_when_the_getter_is_missing_or_stale():
    assert node_detail_extras(RuntimeAccess(SimpleNamespace()), "a" * 24) == {}
    stale = SimpleNamespace(bridge_node_endpoint=lambda node_id: {
        "stale": True, "host": "x.example", "port": 1, "protocols": ()})
    assert node_detail_extras(RuntimeAccess(stale), "a" * 24) == {}
    broken = SimpleNamespace(bridge_node_endpoint=lambda node_id: {
        "stale": False, "host": "", "port": 0,
        "protocols": [{"key": "Bad Key", "port": 1}, {"key": "trojan", "port": 70000}]})
    assert node_detail_extras(RuntimeAccess(broken), "a" * 24) == {}
    assert node_detail_extras(RuntimeAccess(stale), None) == {}


def test_full_host_is_rejected_by_the_frame_shape():
    errors = []
    protocol._schema({"host_masked": "gw-tyo01.example.org", "port": 443}, "Endpoint", "e", errors)
    assert "invariant:e.host_masked" in errors


class SummaryRuntime:
    def __init__(self, config=None, generation=7):
        self.config = config if config is not None else {
            "tiers": {"tier1": {"check_interval": 30}, "tier2": {"check_interval": "bad"},
                      "tier3": {"check_interval": 120}}}
        self.generation = generation
        self.summary = {"carrier": "vanguard", "offline_reason": None,
                        "order": ["vanguard", "sentinel", "bastion", "gateway"],
                        "degraded": False, "fallback": False,
                        "tiers": {name: {"enabled": True, "total": 3, "usable": 2}
                                  for name in ("vanguard", "sentinel", "bastion", "gateway")},
                        "pool": {"usable": 4, "total": 6}}

    def tiers_summary_status(self):
        return 5, self.summary

    def lifecycle_status(self):
        return {"intent": {"runtime_generation": self.generation}}


def test_summary_adds_the_manager_check_interval_without_mutating_the_cache():
    rt = SummaryRuntime()
    revision, summary = RuntimeAccess(rt).tiers_summary_status()
    assert revision == 5
    assert {name: row["check_interval_s"] for name, row in summary["tiers"].items()} == {
        "vanguard": 30, "sentinel": 60, "bastion": 120, "gateway": 60}
    assert "check_interval_s" not in rt.summary["tiers"]["vanguard"]   # cache untouched
    assert all("pending" not in row for row in summary["tiers"].values())
    assert protocol.validate_topic_payload("tiers.summary", summary) == []


def test_summary_without_tiers_or_config_passes_through():
    rt = SummaryRuntime(config=None)
    rt.config = None
    _rev, summary = RuntimeAccess(rt).tiers_summary_status()
    assert all("check_interval_s" not in row for row in summary["tiers"].values())
    rt.summary = {"weird": True}
    assert RuntimeAccess(rt).tiers_summary_status() == (5, {"weird": True})
    rt.tiers_summary_status = lambda: None
    assert RuntimeAccess(rt).tiers_summary_status() is None


def test_node_last_checked_at_comes_from_the_runtime_record():
    result = NodeProjection().project(*facts(checked=1000))
    node, _sub = first(result)
    assert node["last_checked_at_ms"] == 1000
    result = NodeProjection().project(*facts())
    assert first(result)[0]["last_checked_at_ms"] is None


# ----------------------------------------------------------------- 4. slow

def test_slow_is_the_runtime_latency_verdict_and_only_for_healthy_rows():
    nodes, subs, life = facts(checked=1000)
    alive = deepcopy(nodes["items"][0])
    # Manual rows: their status is the runtime's own check, no subscription gate.
    slow = deepcopy(alive)
    slow.update(id=opaque_id("slow-node"), status="slow", source_id=None, source_ids=())
    dead = deepcopy(alive)
    dead.update(id=opaque_id("dead-node"), status="dead", source_id=None, source_ids=())
    nodes["items"] = [alive, slow, dead]
    result = NodeProjection().project(nodes, subs, life)
    rows = {row["id"]: row for row in result["nodes"]["items"]}
    assert rows[alive["id"]]["slow"] is False
    assert rows[slow["id"]]["status"] == "healthy" and rows[slow["id"]]["slow"] is True
    assert rows[dead["id"]]["slow"] is False
    assert protocol.validate_topic_payload("nodes.list", result["nodes"]) == []
    errors = []
    protocol._schema(dict(rows[dead["id"]], slow=True), "Node", "n", errors)
    assert "invariant:n.slow" in errors


# ---------------------------------------- 6. subscription recheck & refresh

def test_recheck_progress_appears_only_while_members_wait():
    projector = NodeProjection()
    _node, sub = first(projector.project(*facts()))
    assert sub["recheck"] == {"done": 0, "total": 1}
    _node, sub = first(projector.project(*facts(checked=1000)))
    assert "recheck" not in sub
    _node, sub = first(projector.project(*facts(enabled=False)))
    assert "recheck" not in sub


def test_last_refresh_time_is_tier_level_and_omitted_when_unknown():
    projector = NodeProjection()
    result = projector.project(*facts(checked=1000), refresh_times={"sentinel": 1234, "gateway": None})
    assert first(result)[1]["last_refresh_at_ms"] == 1234
    result = projector.project(*facts(checked=1000), refresh_times={"sentinel": None, "gateway": 99})
    assert first(result)[1]["last_refresh_at_ms"] is None        # never refreshed
    assert protocol.validate_topic_payload("subscriptions.list", result["subscriptions"]) == []
    result = projector.project(*facts(checked=1000), refresh_times={"gateway": 99})
    assert "last_refresh_at_ms" not in first(result)[1]          # tier not timed: unknown
    result = projector.project(*facts(checked=1000))
    assert "last_refresh_at_ms" not in first(result)[1]          # runtime unknown
    assert protocol.validate_topic_payload("subscriptions.list", result["subscriptions"]) == []


def test_bastion_subscription_has_no_refresh_time_never_a_false_never_refreshed():
    # Fix round 1 (MUST): the runtime getter times only Sentinel and Gateway;
    # a Bastion subscription row omits the field instead of claiming null.
    nodes, subs, life = facts(checked=1000)
    nodes["items"][0]["tier"] = "bastion"
    subs["items"][0]["tier"] = "bastion"
    result = NodeProjection().project(nodes, subs, life,
                                      refresh_times={"sentinel": 1234, "gateway": None})
    rows = result["subscriptions"]["items"]
    assert [(row["tier"], "last_refresh_at_ms" in row) for row in rows] == [("bastion", False)]
    assert protocol.validate_topic_payload("subscriptions.list", result["subscriptions"]) == []


def test_runtime_refresh_times_getter_reads_the_manager_snapshot_times(runtime):
    runtime.manager.tier2_last_refresh_at = "2026-10-01T10:00:00+00:00"
    raw = runtime.bridge_subscription_refresh_times()
    assert raw["stale"] is False
    assert dict(raw["tiers"]) == {"sentinel": 1790848800000, "gateway": None}
    from bridge_nodes import _refresh_times
    assert dict(_refresh_times(RuntimeAccess(runtime))) == {"sentinel": 1790848800000,
                                                            "gateway": None}
    assert _refresh_times(RuntimeAccess(SimpleNamespace())) is None


# ---------------------------------------------------------- 7. pending count

def test_projection_counts_pending_rows_per_tier():
    result = NodeProjection().project(*facts())
    assert result["pending"] == {"vanguard": 0, "sentinel": 1, "bastion": 0, "gateway": 0}
    assert result["generation"] == 7
    assert NodeProjection().project(*facts(checked=1000))["pending"]["sentinel"] == 0


def test_pending_reaches_the_summary_only_for_the_same_generation():
    rt = SummaryRuntime(generation=7)
    access = RuntimeAccess(rt)
    note_node_pending(access, 7, {"sentinel": 2, "gateway": 1, "bogus": 5, "vanguard": -1})
    _rev, summary = access.tiers_summary_status()
    assert summary["tiers"]["sentinel"]["pending"] == 2
    assert summary["tiers"]["gateway"]["pending"] == 1
    assert "pending" not in summary["tiers"]["vanguard"]
    assert protocol.validate_topic_payload("tiers.summary", summary) == []
    rt.generation = 8                                     # runtime replaced: stale counts
    _rev, summary = access.tiers_summary_status()
    assert all("pending" not in row for row in summary["tiers"].values())
    note_node_pending(SimpleNamespace(), 8, {"sentinel": 1})   # not a RuntimeAccess: ignored


@pytest.mark.parametrize("reason", ["stopped", "stopping"])
def test_stopped_summary_of_the_same_generation_carries_no_pending(reason):
    # Fix round 1 (MUST): the generation only advances on start, so after a
    # stop the pre-stop count still matches it; PROTOCOL §5.3 omits pending.
    rt = SummaryRuntime(generation=7)
    access = RuntimeAccess(rt)
    note_node_pending(access, 7, {"sentinel": 5})
    rt.summary = dict(rt.summary, carrier=None, offline_reason=reason,
                      tiers={name: {"enabled": True, "total": 0, "usable": 0}
                             for name in ("vanguard", "sentinel", "bastion", "gateway")})
    _rev, summary = access.tiers_summary_status()
    assert all("pending" not in row for row in summary["tiers"].values())
    assert summary["tiers"]["sentinel"]["check_interval_s"] == 60
    assert protocol.validate_topic_payload("tiers.summary", summary) == []
    rt.summary = dict(rt.summary, offline_reason=None)        # running again, same generation
    assert access.tiers_summary_status()[1]["tiers"]["sentinel"]["pending"] == 5


def test_pending_older_than_the_limit_is_not_merged(monkeypatch):
    # Fix round 1 (SHOULD): with nothing subscribed to the node lists the
    # projection stops; a frozen count expires instead of being merged forever.
    import bridge_runtime_access as bra
    clock = [1000.0]
    monkeypatch.setattr(bra, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    access = RuntimeAccess(SummaryRuntime(generation=7))
    note_node_pending(access, 7, {"sentinel": 3})
    clock[0] += bra.PENDING_MAX_AGE_S
    assert access.tiers_summary_status()[1]["tiers"]["sentinel"]["pending"] == 3
    clock[0] += 0.5
    assert "pending" not in access.tiers_summary_status()[1]["tiers"]["sentinel"]
    note_node_pending(access, 7, {"sentinel": 4})              # projection resumed
    assert access.tiers_summary_status()[1]["tiers"]["sentinel"]["pending"] == 4


def test_sample_hands_the_pending_counts_to_the_access():
    nodes, subs, life = facts()
    fake = SimpleNamespace(bridge_node_records=lambda: nodes,
                           bridge_subscription_records=lambda: subs)

    class Access(RuntimeAccess):
        def lifecycle_status(self):
            return life

    access = Access(fake)
    result = NodeProjection(access).sample()
    assert result["pending"]["sentinel"] == 1
    assert access._node_pending[:2] == (7, result["pending"])
    # No refresh-time getter on this runtime: the field is omitted, not invented.
    assert result["subscriptions"] is None or all(
        "last_refresh_at_ms" not in row for row in result["subscriptions"]["items"])


# ----------------------------------------------------- 8. diagnose protocol

def test_diagnose_args_have_no_protocol_field():
    frame = json.loads((V1_VALID / "command_nodes.diagnose.json").read_bytes())
    assert protocol.validate_frame(frame) == []
    frame["args"]["protocol"] = "hysteria2"
    assert protocol.validate_frame(frame)
    import inspect
    from desktop_runtime import DesktopRuntime
    assert "protocol" not in inspect.signature(DesktopRuntime.diagnose_node).parameters


# ------------------------------------------------------ 9. Gateway semantics

FALLBACK = {"group": "FINAL-EXIT", "selected": "PROXY-FALLBACK"}


def test_empty_proxy_name_deactivates_the_gateway(executor, handler, port):
    port.select = lambda name: {"selected": name, "applied": True, "core_running": True,
                                "selection": dict(FALLBACK)}
    job = run(executor, "gateway.select", {"proxy_name": ""})
    assert job["state"] == "done"
    assert job["result"] == {"selected": "", "applied": True, "apply_error": None,
                             "selection": None, "pending": None}
    assert ("set_exit_selected_proxy", "") in port.calls


@pytest.mark.parametrize("raw, reason", [
    ({"applied": True, "core_running": True,
      "selection": {"group": "EXIT-SELECT", "selected": "Gateway A"}}, "selection_unconfirmed"),
    ({"applied": False, "core_running": True,
      "apply_error": "exit selection was superseded by newer user intent"}, "superseded"),
    ({"applied": False, "core_running": True, "apply_error": "reload not confirmed"},
     "reload_unconfirmed"),
    ({"applied": False, "core_running": True}, "apply_unconfirmed"),
])
def test_deactivation_never_pretends(executor, handler, port, raw, reason):
    port.select = lambda name: dict(raw, selected=name)
    job = run(executor, "gateway.select", {"proxy_name": ""})
    assert job["state"] == "failed"
    assert job["result"]["selected"] == "" and job["result"]["applied"] is False
    assert job["result"]["selection"] is None
    assert job["result"]["apply_error"]["reason_code"] == reason


@pytest.mark.parametrize("saved, done", [("", True), (None, True), ("Gateway A", False)])
def test_deactivation_with_stopped_core_reads_the_saved_selection(executor, handler, port,
                                                                  saved, done):
    port.saved_exit = saved
    port.select = lambda name: {"selected": name, "applied": False, "core_running": False}
    job = run(executor, "gateway.select", {"proxy_name": ""})
    assert job["state"] == ("done" if done else "failed")
    assert job["result"]["applied"] is done and job["result"]["selection"] is None


@pytest.mark.parametrize("name", [" ", " Gateway A", "vless://x@y:1"])
def test_non_empty_names_keep_the_old_validation(executor, handler, port, name):
    job = run(executor, "gateway.select", {"proxy_name": name})
    assert job["state"] == "failed" and job["error"]["code"] == "validation"
    assert ("set_exit_selected_proxy", name) not in port.calls


def test_done_selection_invariant_follows_the_selected_value():
    frame = json.loads((V1_VALID / "p4c2_jobs_gateway.select_deactivated.json").read_bytes())
    assert protocol.validate_frame(frame) == []
    frame["data"]["result"]["selection"] = "Gateway A"
    assert "invariant:jobs.result.selection" in protocol.validate_frame(frame)
    old = json.loads((V1_VALID / "result_gateway.select.json").read_bytes())
    old["data"]["result"]["selection"] = None
    assert "invariant:jobs.result.selection" in protocol.validate_frame(old)


# ------------------------------------------------------------- 10. update

def test_update_dto_carries_plain_notes_enabled_and_last_check(tmp_path):
    from tests.test_bridge_p2_26 import FakeUpdater, release
    from bridge_update import UpdateOwner
    import app_update_state as state
    from dataclasses import replace
    updater = FakeUpdater()
    updater.info = replace(release(), release_notes="## 0.3\r\n- faster\n- see https://github.com/x/y")
    wall = [T0]
    owner = UpdateOwner(state.update_state_path(tmp_path), tmp_path / "downloads",
                        current_version="1.0.0", updater=updater, wall_ms=lambda: wall[0],
                        update_enabled=False)
    before = owner.snapshot()["data"]
    assert (before["notes"], before["enabled"], before["last_check_at_ms"]) == (None, False, None)
    wall[0] = T0 + 5
    dto = owner.check()
    assert dto["notes"] == "## 0.3\n- faster\n- see github.com/x/y"
    assert dto["last_check_at_ms"] == T0 + 5 and dto["enabled"] is False
    assert protocol.validate_topic_payload("update.state", dto) == []
    updater.info = release(available=False)
    wall[0] = T0 + 9
    dto = owner.check()
    assert dto["notes"] is None and dto["last_check_at_ms"] == T0 + 9


def test_failed_check_still_records_the_check_time(tmp_path):
    from tests.test_bridge_p2_26 import FakeUpdater
    from bridge_update import UpdateOwner
    from bridge_jobs import JobFailure
    import app_update_state as state
    updater = FakeUpdater()
    updater.failure = OSError("offline")
    owner = UpdateOwner(state.update_state_path(tmp_path), tmp_path / "downloads",
                        current_version="1.0.0", updater=updater, wall_ms=lambda: T0)
    with pytest.raises(JobFailure):
        owner.check()
    assert owner.snapshot()["data"]["last_check_at_ms"] == T0


@pytest.mark.parametrize("text, expected", [
    ("", None), (None, None), ("  \n ", None),
    ("hello\x00 world\x1b", "hello world"),
    ("password=hunter2", None),                      # the frame scanner would reject it
    ("see HTTPS://Example.org/a and http://b", "see Example.org/a and b"),
    # Judged before the scheme is dropped (fix round 1): a node URI, any
    # non-link scheme or a link with userinfo withholds the whole notes.
    ("get vless://8a1e7c2b@host.example.invalid:443 now", None),
    ("vmess://eyJ2IjoiMiJ9", None), ("ftp://b", None),
    ("see https://user:pw@example.invalid/x", None),
])
def test_plain_release_notes(text, expected):
    from bridge_update import plain_release_notes
    assert plain_release_notes(text) == expected


def test_long_notes_are_cut_with_an_ellipsis():
    from bridge_update import MAX_NOTES_CHARS, plain_release_notes
    notes = plain_release_notes("x" * (MAX_NOTES_CHARS + 50))
    assert len(notes) == MAX_NOTES_CHARS and notes.endswith("…")


# ---------------------------------------------------------------- 11. geo

def test_geo_items_follow_the_updater_order_and_never_carry_error_text():
    raw = {"running": False,
           "last_update": {"geosite": "2026-10-01T00:00:00+00:00", "geoip": "2026-10-02T00:00:00+00:00",
                           "junk": "2026-10-01T00:00:00+00:00"},
           "errors": {"mmdb": "https://user:pw@example.invalid/x failed"}}
    items = geo_items(raw)
    assert items == [{"kind": "geoip", "updated_at_ms": 1790899200000},
                     {"kind": "geosite", "updated_at_ms": 1790812800000},
                     {"kind": "mmdb", "updated_at_ms": None}]
    assert "example.invalid" not in json.dumps(items)
    status = {"running": False, "updated_at_ms": 1790899200000, "errors": [], "items": items}
    assert protocol.validate_topic_payload("geo.status", status) == []
    assert geo_items({}) == [] and geo_items(None) == []


def test_geo_item_kinds_match_the_updater():
    import geo_updater
    from bridge_runtime_access import GEO_KINDS
    assert tuple(geo_updater.ASSET_KEYS) == GEO_KINDS


# ---------------------------------------------------------- 12. logs.feed

def feed_collector():
    from bridge_logs import LogCollector
    from bridge_state import PublicationState
    return LogCollector(PublicationState("p4c2"))


@pytest.mark.parametrize("facts_, error_code, expected", [
    ({"core_running": False, "controller_enabled": True}, None, "core_not_running"),
    ({"core_running": True, "controller_enabled": False}, None, "controller_disabled"),
    ({"core_running": True, "controller_enabled": True}, "stream_disconnected", "stream_error"),
    ({"core_running": True, "controller_enabled": True}, "stream_unavailable", "stream_error"),
    ({"core_running": True, "controller_enabled": True}, "controller_disabled", "controller_disabled"),
    ({"core_running": True, "controller_enabled": True}, None, None),
    (None, "stream_disconnected", "stream_error"),
])
def test_feed_reason_runtime_facts_first_then_stream_error(facts_, error_code, expected):
    logs = feed_collector()
    logs.stream = SimpleNamespace(error_code=error_code, level="info")
    if facts_ is not None:
        logs.set_reason_source(lambda: facts_)
    dto = logs.feed()
    assert dto["reason"] == expected
    assert protocol.validate_topic_payload("logs.feed", dto) == []


def test_connected_feed_has_no_reason_and_a_failing_source_is_unknown():
    logs = feed_collector()
    logs.stream = SimpleNamespace(error_code="stream_disconnected", level="info")
    logs.set_reason_source(lambda: 1 / 0)
    assert logs.feed()["reason"] == "stream_error"
    logs._feed_state = "connected"
    logs.set_reason_source(lambda: {"core_running": False})
    assert logs.feed()["reason"] is None


def test_register_attaches_cheap_runtime_facts():
    import bridge_logs
    logs = feed_collector()
    core = {"running": True}
    session = SimpleNamespace(logs=logs, runtime=SimpleNamespace(config={}, api_client=None),
                              access=SimpleNamespace(core_fact=lambda: dict(core)))
    assert bridge_logs.register(session) is logs
    assert logs.feed()["reason"] == "controller_disabled"
    core["running"] = False
    assert logs.feed()["reason"] == "core_not_running"
    session.runtime.api_client = object()
    core["running"] = True
    assert logs.feed()["reason"] is None


# --------------------------------------------------------- 13. import groups

def test_preview_groups_protocol_rows_of_one_physical_node(tmp_path):
    from tests.test_bridge_p2_15 import Context, assert_preview_data, make
    _rt, handler, _ = make(tmp_path)
    text = ("trojan://Zq9pw@203.0.113.9:8443?sni=a.example#HK-01-Trojan\n"
            "ss://YWVzLTI1Ni1nY206cGFzcw@203.0.113.9:8388#HK-01-SS\n"
            "trojan://Zq9pw@198.51.100.4:443?sni=b.example#JP-02\n")
    data = handler.preview({"tier": "vanguard", "text": text}, Context())
    assert_preview_data(data)
    assert data["accepted"] == 3
    assert data["groups"] == [{"key": "g0", "row_indexes": [0, 1], "label": "HK-01"},
                              {"key": "g1", "row_indexes": [2]}]
    wire = json.dumps(data["groups"])
    assert "203.0.113.9" not in wire and "Zq9pw" not in wire


@pytest.mark.parametrize("names", [
    ("550e8400-e29b-41d4-a716-446655440000 T", "550e8400-e29b-41d4-a716-446655440000 S"),
    ("Zq9pwSecret77 T", "Zq9pwSecret77 S"),                 # the trojan password
])
def test_group_label_is_filtered_like_node_names(names):
    # Fix round 1 (SHOULD): the frame scanner alone accepts these labels;
    # the nodes.list name filter (own secrets, UUIDs) withholds them.
    from bridge_import import physical_groups
    text = (f"trojan://Zq9pwSecret77@203.0.113.9:8443?sni=a.example#{names[0]}\n"
            f"ss://YWVzLTI1Ni1nY206cGFzcw@203.0.113.9:8388#{names[1]}\n")
    groups = physical_groups(text, source="manual-import", layer="tier1", accepted=2)
    assert groups == [{"key": "g0", "row_indexes": [0, 1]}]
    clean = physical_groups(text.replace(names[0], "HK-01-T").replace(names[1], "HK-01-S"),
                            source="manual-import", layer="tier1", accepted=2)
    assert clean == [{"key": "g0", "row_indexes": [0, 1], "label": "HK-01"}]


def test_group_label_filter_drops_any_parsed_host():
    from bridge_import import _label_filter
    from parser import analyze_content_blob
    nodes = list(analyze_content_blob(
        "trojan://Zq9pwSecret77@Edge.Example.invalid:8443?sni=a.example#x\n",
        source="manual-import", tier="tier1").accepted_nodes)
    clean = _label_filter(nodes)
    assert clean("HK edge.example.INVALID") != "HK edge.example.INVALID"
    assert clean("HK-01") == "HK-01"


def test_group_key_must_be_the_group_position():
    errors = []
    protocol._schema({"key": "hk.example.invalid:443", "row_indexes": [0]},
                     "ImportGroup", "g", errors)
    assert "invariant:g.key" in errors
    errors = []
    protocol._schema({"key": "g12", "row_indexes": [0]}, "ImportGroup", "g", errors)
    assert errors == []


def test_preview_groups_only_for_vanguard(tmp_path):
    from tests.test_bridge_p2_15 import Context, TROJAN_B, make
    _rt, handler, _ = make(tmp_path)
    data = handler.preview({"tier": "sentinel", "text": TROJAN_B}, Context())
    assert "groups" not in data


def test_group_disagreeing_with_the_service_count_is_omitted():
    from bridge_import import physical_groups
    text = "trojan://Zq9pw@198.51.100.4:443?sni=b.example#JP-02\n"
    assert physical_groups(text, source="manual-import", layer="tier1", accepted=1) == [
        {"key": "g0", "row_indexes": [0]}]
    assert physical_groups(text, source="manual-import", layer="tier1", accepted=2) is None
    assert physical_groups(text, source="manual-import", layer="tier1", accepted=1,
                           physical_count=2) is None
    assert physical_groups(text, source="manual-import", layer="tier2", accepted=1) is None
    assert physical_groups(text, source="manual-import", layer="tier1", accepted=0) is None


def test_group_rows_must_partition_the_accepted_nodes():
    frame = json.loads((V1_VALID / "p4c2_response_import.preview_groups.json").read_bytes())
    assert protocol.validate_frame(frame) == []
    for groups in ([{"key": "g0", "row_indexes": [0, 0]}],
                   [{"key": "g0", "row_indexes": [0]}, {"key": "g1", "row_indexes": [0]}],
                   [{"key": "g0", "row_indexes": [frame["data"]["accepted"]]}],
                   [{"key": "g0", "row_indexes": []}]):
        bad = deepcopy(frame)
        bad["data"]["groups"] = groups
        assert protocol.validate_frame(bad), groups
