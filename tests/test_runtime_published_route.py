import threading
from dataclasses import replace

import pytest

from bridge_protocol import validate_route_published
from desktop_runtime import DesktopRuntime
from runtime_egress_recovery import CoreLoadedGroupState, CoreLoadedRouteSnapshot, RuntimeNodeRef


class Forbidden:
    def __getattr__(self, name):
        pytest.fail(f"unexpected HTTP access: {name}")


def runtime_with_snapshot():
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_route_epoch = 7
    runtime._runtime_network_context_epoch = 2
    runtime._runtime_confirmed_fail_closed_snapshot_key = None
    runtime._runtime_route_snapshot = CoreLoadedRouteSnapshot(
        process_pid=42, process_generation=3, core_generation=8,
        semantic_digest="a" * 64, route_epoch=7, network_context_epoch=2,
        group_states=(CoreLoadedGroupState(name="FINAL-EXIT", type_name="Selector",
                                            members=("node-a",), now="node-a"),),
        proxy_nodes=(RuntimeNodeRef(tier="tier1", pool_kind="user",
                                    identity="a", proxy_name="node-a"),),
        loaded_at_monotonic=100.0,
    )
    runtime.api_client = Forbidden()
    runtime._runtime_route_snapshot_state = lambda: pytest.fail("deep copy")
    runtime.core_fact = lambda: {"running": True, "pid": 42,
                                 "process_generation": 3, "core_generation": 8}
    return runtime


def wire(value):
    return {**value, "owner": dict(value["owner"]) if value["owner"] else None,
            "selected_chain": list(value["selected_chain"])}


def test_published_route_is_immutable_and_protocol_valid_without_http():
    runtime = runtime_with_snapshot()
    value = runtime.published_route_status()
    assert value["valid"] and value["selected_chain"] == ("FINAL-EXIT", "node-a")
    assert value["leaf"] == "node-a" and value["managed_tier"] == "vanguard"
    assert dict(value["owner"]) == {
        "process_pid": 42, "process_generation": 3, "core_generation": 8,
        "semantic_digest": "a" * 64, "route_epoch": 7,
        "network_context_epoch": 2,
    }
    assert validate_route_published(wire(value)) == []
    with pytest.raises(TypeError):
        value["valid"] = False
    with pytest.raises(TypeError):
        value["owner"]["route_epoch"] = 9
    # Zero HTTP also prevents a read from clearing Mihomo's fixed selection.


def test_invalid_snapshot_retains_owner_and_no_snapshot_clears_it():
    runtime = runtime_with_snapshot()
    first = runtime.published_route_status()
    runtime._runtime_route_epoch = 8
    stale = runtime.published_route_status()
    assert not stale["valid"] and stale["invalid_reason"] == "route_owner_changed"
    assert stale["owner"] == first["owner"]
    assert stale["publication_revision"] > first["publication_revision"]
    assert validate_route_published(wire(stale)) == []
    runtime._runtime_route_snapshot = None
    missing = runtime.published_route_status()
    assert missing["owner"] is None and missing["invalid_reason"] == "no_published_route"
    assert missing["selected_chain"] == () and not missing["fail_closed_confirmed"]
    assert validate_route_published(wire(missing)) == []


@pytest.mark.parametrize("runtime_tier,managed_tier", [
    ("tier1", "vanguard"), ("tier2", "sentinel"),
    ("tier3", "bastion"), ("exit", "gateway"),
])
def test_managed_tier_maps_runtime_node_roles(runtime_tier, managed_tier):
    runtime = runtime_with_snapshot()
    snapshot = runtime._runtime_route_snapshot
    runtime._runtime_route_snapshot = replace(
        snapshot, proxy_nodes=(replace(snapshot.proxy_nodes[0], tier=runtime_tier),))
    value = runtime.published_route_status()
    assert value["valid"] and value["managed_tier"] == managed_tier
    assert validate_route_published(wire(value)) == []


@pytest.mark.parametrize("states", [
    (),
    (CoreLoadedGroupState(name="FINAL-EXIT", type_name="Selector",
                          members=("missing",), now="missing"),),
    (CoreLoadedGroupState(name="FINAL-EXIT", type_name="Selector",
                          members=("NEXT",), now="NEXT"),
     CoreLoadedGroupState(name="NEXT", type_name="Selector",
                          members=("FINAL-EXIT",), now="FINAL-EXIT")),
])
def test_missing_root_broken_chain_and_cycle_are_unresolved(states):
    runtime = runtime_with_snapshot()
    runtime._runtime_route_snapshot = replace(runtime._runtime_route_snapshot,
                                              group_states=states)
    value = runtime.published_route_status()
    assert not value["valid"] and value["invalid_reason"] == "route_unresolved"
    assert value["owner"] is not None and value["leaf"] is None
    assert validate_route_published(wire(value)) == []


@pytest.mark.parametrize("leaf", ["DIRECT", "REJECT"])
def test_built_in_leaf_remains_resolved(leaf):
    runtime = runtime_with_snapshot()
    runtime._runtime_route_snapshot = replace(
        runtime._runtime_route_snapshot,
        group_states=(CoreLoadedGroupState(name="FINAL-EXIT", type_name="Selector",
                                            members=(leaf,), now=leaf),))
    value = runtime.published_route_status()
    assert value["valid"] and value["leaf"] == leaf
    assert value["managed_tier"] is None
    assert not value["fail_closed_confirmed"]
    assert validate_route_published(wire(value)) == []


def test_core_exit_and_generation_change_increment_observed_revision():
    runtime = runtime_with_snapshot()
    fact = {"running": True, "pid": 42, "process_generation": 3,
            "core_generation": 8}
    runtime.core_fact = lambda: fact.copy()
    first = runtime.published_route_status()
    assert runtime.published_route_status()["publication_revision"] == first["publication_revision"]
    fact["running"] = False
    exited = runtime.published_route_status()
    assert not exited["valid"] and exited["invalid_reason"] == "core_owner_changed"
    assert exited["publication_revision"] > first["publication_revision"]
    assert runtime.published_route_status()["publication_revision"] == exited["publication_revision"]
    fact.update(running=True, pid=99, process_generation=4, core_generation=9)
    changed = runtime.published_route_status()
    assert not changed["valid"] and changed["invalid_reason"] == "core_owner_changed"
    assert changed["owner"] == first["owner"]
    assert changed["publication_revision"] > exited["publication_revision"]
    assert runtime.published_route_status()["publication_revision"] == changed["publication_revision"]


def test_network_epoch_invalidates_without_losing_owner():
    runtime = runtime_with_snapshot()
    first = runtime.published_route_status()
    runtime._runtime_network_context_epoch += 1
    changed = runtime.published_route_status()
    assert not changed["valid"] and changed["invalid_reason"] == "route_owner_changed"
    assert changed["owner"] == first["owner"]
    assert changed["publication_revision"] > first["publication_revision"]
    assert validate_route_published(wire(changed)) == []


def test_reject_confirmation_is_independent_of_snapshot_validity():
    runtime = runtime_with_snapshot()
    snapshot = runtime._runtime_route_snapshot
    runtime._runtime_route_snapshot = replace(
        snapshot, group_states=(CoreLoadedGroupState(
            name="FINAL-EXIT", type_name="Selector", members=("REJECT",), now="REJECT"),))
    unconfirmed = runtime.published_route_status()
    assert unconfirmed["valid"] and not unconfirmed["fail_closed_confirmed"]
    runtime._runtime_confirmed_fail_closed_snapshot_key = snapshot.snapshot_key[:-1] + (99,)
    mismatched = runtime.published_route_status()
    assert mismatched["valid"] and not mismatched["fail_closed_confirmed"]
    runtime._runtime_confirmed_fail_closed_snapshot_key = snapshot.snapshot_key
    confirmed = runtime.published_route_status()
    assert confirmed["valid"] and confirmed["fail_closed_confirmed"]
    runtime._runtime_network_context_epoch += 1
    stale = runtime.published_route_status()
    assert not stale["valid"] and stale["fail_closed_confirmed"]
    assert validate_route_published(wire(stale)) == []
