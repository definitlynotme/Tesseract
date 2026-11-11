"""A stable Tier3 name is the Core's handle for a node, not its display name.

With verified provider writes (tiers.tier3.verified_provider_writes) Tier3
members load as ``name·digest``.  The shell marks the Bastion row that carries
traffic by ``route.published.leaf == Node.name`` (ui/src/rt/project_tiers.js
isCurrent) and finds the overview carrier row by name in ``selected_chain``
(ui/src/rt/project_overview.js currentNode), so published_route_status shows
the node's own name for such a leaf.  Positional names are published as before.

The published proxy map is built by the production path: the real renderer,
the real TIER3B-RUNTIME file-provider split, the render-receipt map and the
exact loaded-contract map, published next to the snapshot as
_refresh_runtime_route_snapshot does.
"""

from __future__ import annotations

import threading

import pytest
import yaml

from bridge_protocol import validate_route_published
from clash_formatter import (
    MIHOMO_RESERVED_PROXY_NAMES,
    build_clash_proxy_list,
    build_full_clash_config,
    is_stable_proxy_name,
)
from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus
from runtime_egress_recovery import (
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeNodeRef,
)
from tier3_core_provider import project_tier3b_file_provider


def _node(name: str, port: int) -> Node:
    return Node(
        name=name,
        protocol="trojan",
        tier="tier3",
        address="127.0.0.1",
        port=port,
        password=f"pw-{port}",
        status=NodeStatus.ALIVE,
        latency_ms=10,
        extra={"tier3_auto_pool": True},
    )


def _rendered(nodes: list[Node], *, stable: bool) -> list[dict]:
    pairs: list = []
    build_clash_proxy_list(
        nodes,
        seen_names=set(MIHOMO_RESERVED_PROXY_NAMES),
        rendered_pairs=pairs,
        stable_names=stable,
    )
    return [proxy for _node, proxy in pairs]


def _runtime(nodes: list[Node], *, stable: bool, serving: int):
    """A runtime whose published snapshot is what Core loaded for ``nodes``.

    FINAL-EXIT -> PROXY-FALLBACK -> TIER3-AUTO -> member ``serving``.
    """

    receipt: dict = {}
    text = build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": list(nodes), "exit": []},
        stable_tier3_names=stable,
        render_receipt=receipt,
    )
    names = [entry["proxy"]["name"] for entry in receipt["proxies"] if entry["tier"] == "tier3"]
    projection = project_tier3b_file_provider(text, names)
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.manager = None
    runtime._tier3_core_provider_name = "TIER3B-RUNTIME"
    contract = runtime._runtime_export_contract_for_payload(
        projection.main_yaml,
        provider_payload=projection.provider_yaml,
        rendered_proxy_map=runtime._runtime_proxy_map_from_render_receipt(receipt),
    )
    proxy_map = contract["proxy_map"]
    assert sorted(proxy_map) == sorted(names)
    members = tuple(projection.proxy_names)
    states = (
        CoreLoadedGroupState(
            name="FINAL-EXIT", type_name="Selector",
            members=("PROXY-FALLBACK", "REJECT"), now="PROXY-FALLBACK",
        ),
        CoreLoadedGroupState(
            name="PROXY-FALLBACK", type_name="Fallback",
            members=("TIER3-AUTO", "REJECT"), now="TIER3-AUTO",
        ),
        CoreLoadedGroupState(
            name="TIER3-AUTO", type_name="Fallback",
            members=(*members, "REJECT"), now=members[serving],
        ),
    )
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_route_epoch = 7
    runtime._runtime_network_context_epoch = 2
    runtime._runtime_confirmed_fail_closed_snapshot_key = None
    runtime._runtime_route_snapshot = CoreLoadedRouteSnapshot(
        process_pid=42, process_generation=3, core_generation=8,
        semantic_digest=contract["semantic_digest"], route_epoch=7, network_context_epoch=2,
        group_states=states,
        proxy_nodes=tuple(
            RuntimeNodeRef(
                tier=str(info["tier"]), pool_kind=str(info["pool_kind"]),
                identity=str(info["identity"]), proxy_name=str(name),
            )
            for name, info in proxy_map.items()
        ),
        loaded_at_monotonic=100.0,
    )
    runtime._runtime_route_snapshot_payload = {"proxies": {}, "proxy_map": dict(proxy_map)}
    runtime._runtime_route_snapshot_state = lambda: pytest.fail("deep copy")
    runtime.core_fact = lambda: {
        "running": True, "pid": 42, "process_generation": 3, "core_generation": 8,
    }
    return runtime, members


def _wire(value):
    return {
        **value,
        "owner": dict(value["owner"]) if value["owner"] else None,
        "selected_chain": list(value["selected_chain"]),
    }


def test_is_stable_proxy_name_accepts_only_names_made_from_that_definition():
    twins = [_node("X", 1001), _node("X", 1001)]  # one definition twice: the digest widens
    other = _node("JP · Tokyo", 1002)
    stable = _rendered([*twins, other], stable=True)
    assert [len(proxy["name"].rpartition("·")[2]) for proxy in stable] == [6, 10, 6]
    for proxy in stable:
        assert is_stable_proxy_name(proxy["name"], proxy)
        # Core's copy comes back from the provider file through YAML.
        assert is_stable_proxy_name(proxy["name"], yaml.safe_load(yaml.safe_dump(proxy)))
    assert not is_stable_proxy_name(stable[2]["name"], stable[0])
    for proxy in _rendered([*twins, other], stable=False):
        # 'X', 'X-2' and 'JP · Tokyo' (a middle dot in a positional name).
        assert not is_stable_proxy_name(proxy["name"], proxy)
    assert not is_stable_proxy_name(stable[0]["name"], None)
    assert not is_stable_proxy_name("", stable[0])


@pytest.mark.parametrize("serving,shown", [(0, "US-01"), (1, "JP · Tokyo")])
def test_a_stable_bastion_leaf_is_published_with_the_node_name(serving, shown):
    runtime, members = _runtime(
        [_node("US-01", 1001), _node("JP · Tokyo", 1002)], stable=True, serving=serving,
    )
    assert members[serving] != shown and members[serving].startswith(f"{shown}·")
    value = runtime.published_route_status()
    assert value["valid"] and value["managed_tier"] == "bastion"
    assert value["leaf"] == shown
    assert value["selected_chain"] == ("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", shown)
    assert not value["fail_closed_confirmed"]
    assert validate_route_published(_wire(value)) == []


def test_positional_leaves_are_published_unchanged():
    runtime, members = _runtime(
        [_node("US-01", 1001), _node("US-01", 1002)], stable=False, serving=1,
    )
    assert members[1] == "US-01-2"
    value = runtime.published_route_status()
    assert value["leaf"] == "US-01-2"
    assert value["selected_chain"][-1] == "US-01-2"


def test_a_builtin_display_name_keeps_the_core_name():
    # A free node may be called anything; a managed leaf never reads as REJECT.
    runtime, members = _runtime([_node("REJECT", 1001)], stable=True, serving=0)
    value = runtime.published_route_status()
    assert value["leaf"] == members[0] != "REJECT"
    assert value["managed_tier"] == "bastion" and not value["fail_closed_confirmed"]


def test_the_core_name_stays_when_the_published_map_cannot_prove_it():
    runtime, members = _runtime([_node("US-01", 1001)], stable=True, serving=0)
    runtime._runtime_route_snapshot_payload = None
    assert runtime.published_route_status()["leaf"] == members[0]

    runtime, members = _runtime([_node("US-01", 1001)], stable=True, serving=0)
    entry = runtime._runtime_route_snapshot_payload["proxy_map"][members[0]]
    entry["proxy"] = {**entry["proxy"], "port": 1999}  # not the definition the name came from
    assert runtime.published_route_status()["leaf"] == members[0]
