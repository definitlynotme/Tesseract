"""Service stopped: nodes.list follows the configuration and its ids resolve.

A stopped runtime has no Manager. bridge_node_records used to answer
``stale`` then, so the node projection never re-published: an import while
stopped never reached the list, and a listed id (or the frozen list of the last
run) never resolved for nodes.delete. The list is now the configured inventory
(bridge_stopped_node_inventory), and RuntimeNodeCommandAdapter resolves the
same inventory. Real DesktopRuntime methods on ``build_runtime()``; no app
start, network or Mihomo.
"""

from __future__ import annotations

import pytest

from bridge_node_commands import NodeCommands, RuntimeNodeCommandAdapter
from bridge_nodes import NodeProjection
from bridge_runtime_access import RuntimeAccess
from config_items_view import configured_config_items
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from tests.test_bridge_p2_rg import build_runtime


TWIN_A = "trojan://TwinSecretA@twin-a.example.invalid:443#Twin"
TWIN_B = "trojan://TwinSecretB@twin-b.example.invalid:443#Twin"
FRESH = "trojan://FreshSecret@fresh.example.invalid:443#Fresh"
BASTION = "trojan://BastionSecret@bastion.example.invalid:443#Mine"
GATEWAY = "trojan://GatewaySecret@gateway.example.invalid:443#Exit"


def stopped_runtime(tmp_path):
    rt = build_runtime()
    rt.config["tiers"]["tier2"] = {"nodes": [{"name": "Twin", "uri": TWIN_A},
                                             {"name": "Twin", "uri": TWIN_B}],
                                   "cache_file": str(tmp_path / "tier2_cache.json")}
    rt.config["tiers"]["tier3"] = {"nodes": [{"name": "Mine", "uri": BASTION}]}
    rt.config["tiers"]["exit"] = {"nodes": [GATEWAY],
                                  "cache_file": str(tmp_path / "exit_cache.json")}
    # A completed stop: no Manager, no Core, FAIL_CLOSED readiness at the stop's revision.
    rt.manager = None
    rt.running = False
    rt._desired_running = False
    rt.core_runner.fact["running"] = False
    rt._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.FAIL_CLOSED, validation_active=False,
        lifecycle_revision=rt._lifecycle_revision, route_epoch=rt._runtime_route_epoch,
        network_context_epoch=rt._runtime_network_context_epoch)
    rt.configured_items = lambda: configured_config_items(rt.config, {})

    def delete(item):
        # The store removes the entry and reload_config installs a new config object.
        config = {**rt.config, "tiers": {**rt.config["tiers"]}}
        block = dict(config["tiers"][item["tier"]])
        block["nodes"] = [entry for entry in block["nodes"]
                          if (entry.get("uri") if isinstance(entry, dict) else entry) != item["key"]]
        config["tiers"][item["tier"]] = block
        rt.config = config
        return {"deleted": True}

    rt.delete_config_item = delete
    return rt


def rows(rt):
    return [(row["tier"], row["name"], row["id"]) for row in rt.bridge_node_records()["items"]]


def test_stopped_records_are_the_configured_inventory(tmp_path):
    rt = stopped_runtime(tmp_path)
    records = rt.bridge_node_records()
    assert not records["stale"]
    by_tier = {}
    for row in records["items"]:
        by_tier.setdefault(row["tier"], []).append(row)
    assert [row["name"] for row in by_tier["sentinel"]] == ["Twin", "Twin"]
    assert len({row["id"] for row in by_tier["sentinel"]}) == 2   # same name, two ids
    # The user's own Bastion node is listed (user pool), the Gateway node too.
    assert [(row["name"], row["pool_kind"]) for row in by_tier["bastion"]] == [("Mine", "user")]
    assert [row["name"] for row in by_tier["gateway"]] == ["Exit"]
    assert all(row["status"] == "unknown" and row["latency_ms"] is None
               for row in records["items"])


def test_projection_publishes_while_stopped_and_follows_an_import(tmp_path):
    rt = stopped_runtime(tmp_path)
    projection = NodeProjection(RuntimeAccess(rt))
    first = projection.sample()        # used to raise StaleNodeSample with no Manager
    assert sorted(row["name"] for row in first["nodes"]["items"]
                  if row["tier"] == "sentinel") == ["Twin", "Twin"]
    tiers = {**rt.config["tiers"]}
    tiers["tier2"] = {**tiers["tier2"], "nodes": [*tiers["tier2"]["nodes"],
                                                  {"name": "Fresh", "uri": FRESH}]}
    rt.config = {**rt.config, "tiers": tiers}   # an import while stopped (reload_config)
    after = projection.sample()
    assert sorted(row["name"] for row in after["nodes"]["items"]
                  if row["tier"] == "sentinel") == ["Fresh", "Twin", "Twin"]


def test_each_stopped_row_resolves_and_same_name_nodes_delete_one_by_one(tmp_path):
    rt = stopped_runtime(tmp_path)
    commands = NodeCommands(RuntimeNodeCommandAdapter(rt))
    twins = [node_id for tier, name, node_id in rows(rt) if name == "Twin"]
    keys = {commands.resolve(node_id, need_config=True).config_item["key"] for node_id in twins}
    assert keys == {TWIN_A, TWIN_B}

    result = commands.nodes_delete({"item_id": twins[0]}, None)
    assert result["saved"] and result["applied"]
    left = [(name, node_id) for tier, name, node_id in rows(rt) if tier == "sentinel"]
    assert [name for name, _ in left] == ["Twin"]
    assert left[0][1] == twins[1]          # the other twin keeps its id
    result = commands.nodes_delete({"item_id": left[0][1]}, None)
    assert result["saved"]
    assert [name for tier, name, _ in rows(rt) if tier == "sentinel"] == []
    # Bastion and Gateway manual nodes are deletable the same way.
    for tier in ("bastion", "gateway"):
        node_id = next(node_id for row_tier, _, node_id in rows(rt) if row_tier == tier)
        assert commands.nodes_delete({"item_id": node_id}, None)["saved"]
    assert rows(rt) == []


def test_stopped_ids_go_stale_when_the_service_starts(tmp_path):
    from bridge_node_commands import JobFailure
    rt = stopped_runtime(tmp_path)
    commands = NodeCommands(RuntimeNodeCommandAdapter(rt))
    node_id = rows(rt)[0][2]
    rt._runtime_generation += 1            # a start: ids carry the runtime generation
    with pytest.raises(JobFailure):
        commands.resolve(node_id, need_config=False)


def test_subscriptions_publish_while_stopped_and_follow_an_import(tmp_path):
    rt = stopped_runtime(tmp_path)
    commands = NodeCommands(RuntimeNodeCommandAdapter(rt))
    projection = NodeProjection(RuntimeAccess(rt), refresh_owner=commands.refresh_owner_snapshot)
    assert projection.sample()["subscriptions"]["items"] == []
    tiers = {**rt.config["tiers"]}
    tiers["exit"] = {**tiers["exit"], "subscription_urls": [
        {"url": "https://example.invalid/s/9f3a7c41d2", "name": "Morning feed"}]}
    rt.config = {**rt.config, "tiers": tiers}   # a subscription import while stopped
    subs = projection.sample()["subscriptions"]["items"]
    assert [row["tier"] for row in subs] == ["gateway"]
    assert commands.resolve(subs[0]["id"], need_config=True).kind == "subscription"
