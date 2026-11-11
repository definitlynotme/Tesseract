"""T-GW-ID: the Manager's Gateway readers follow the saved node identity.

The selection is saved as a node identity with its proxy name as a label
(gateway_selection).  The Manager's readers (health checks through the
Gateway, the effective-egress failure reason) must name the picked node,
never a same-named twin, and an old name-only selection only when one node
provably carries it.  The legacy inventory is what the runtime's one-time
binding of old name-only selections judges ambiguity on.

Isolated: the reader methods are the real ProxyTierManager functions bound
onto a minimal fake; the inventory test uses a real ProxyTierManager on
temporary files (no start, Core, helper or network).
"""

from __future__ import annotations

import json
import logging
import types
from pathlib import Path
from unittest.mock import patch

from alerts import AlertManager
from clash_formatter import gateway_legacy_family
from exit_nodes import exit_nodes_from_entry
from gateway_selection import (
    GatewaySelection,
    clear_gateway_selection,
    gateway_identity,
    read_selection,
    write_bound_selection,
    write_name_only_selection,
    write_unresolved_selection,
)
from manager import ProxyTierManager
from models import Node, NodeStatus, PoolSnapshot


def exit_node(name, address, *, status=NodeStatus.ALIVE, password="pw", source="config.yaml"):
    return Node(name=name, protocol="trojan", tier="exit", address=address, port=443,
                password=password, status=status, source=source, sources=[source])


def identities(nodes):
    return {gateway_identity(node) for node in nodes}


def ids(nodes):
    return [id(node) for node in nodes]


# ============================================================ 1. reader methods

class _ReaderFake:
    """Only the state the readers touch; every reader is the real code."""

    def __init__(self, exit_block, exits):
        self.config = {"tiers": {"exit": exit_block}}
        self.runtime_nodes = {"exit": exits}

    def _exit_tier_configured(self):
        return True

    def effective_node_status(self, tier_id, node):
        assert tier_id == "exit"
        return node.status

    def effective_node_usable(self, tier_id, node):
        return self.effective_node_status(tier_id, node) in {NodeStatus.ALIVE, NodeStatus.SLOW}


_READERS = (
    "_tier_config",
    "_exit_config",
    "active_exit_selection",
    "active_exit_proxy_name",
    "_exit_active",
    "_selected_gateway_nodes",
    "_gateway_nodes_for_testing",
    "_selected_gateway_is_healthy",
    "_gateway_egress_failure_reason",
)


def reader_manager(exit_block, exits):
    fake = _ReaderFake(exit_block, exits)
    for name in _READERS:
        setattr(fake, name, types.MethodType(getattr(ProxyTierManager, name), fake))
    return fake


def test_manager_gateway_readers_follow_the_identity():
    twin1 = exit_node("JP 01", "jp-1.example.invalid", status=NodeStatus.DEAD)
    twin2 = exit_node("JP 01", "jp-2.example.invalid", status=NodeStatus.ALIVE)
    hk = exit_node("HK", "hk.example.invalid", status=NodeStatus.ALIVE)
    assert gateway_identity(twin1) != gateway_identity(twin2)
    block = {}
    manager = reader_manager(block, [twin2, twin1, hk])

    # Bound to the DEAD twin: its ALIVE namesake never stands in for it.
    write_bound_selection(block, "JP 01", gateway_identity(twin1))
    assert manager.active_exit_selection() == GatewaySelection("bound", "JP 01", identity=gateway_identity(twin1))
    assert manager.active_exit_proxy_name() == "JP 01"            # the label only
    assert ids(manager._selected_gateway_nodes()) == [id(twin1)]
    assert manager._selected_gateway_is_healthy() is False
    assert manager._gateway_egress_failure_reason() == "selected Gateway exit is unavailable: JP 01 (dead)"
    assert ids(manager._gateway_nodes_for_testing("tier1")) == [id(twin1)]
    assert manager._gateway_nodes_for_testing("exit") == []

    # Bound to the ALIVE twin under the same label: healthy.
    write_bound_selection(block, "JP 01", gateway_identity(twin2))
    assert ids(manager._selected_gateway_nodes()) == [id(twin2)]
    assert manager._gateway_egress_failure_reason() == ""

    # Bound to a Gateway whose Core name is 'HK-2' (a Tier2 'HK' renders
    # first): the identity finds it, the suffixed label is no 'missing'.
    write_bound_selection(block, "HK-2", gateway_identity(hk))
    assert ids(manager._selected_gateway_nodes()) == [id(hk)]
    assert manager._selected_gateway_is_healthy() is True
    assert manager._gateway_egress_failure_reason() == ""
    assert ids(manager._gateway_nodes_for_testing("tier2")) == [id(hk)]

    # Name only: 'JP 01' names two identities, 'HK-2' a render position.
    for label in ("JP 01", "HK-2"):
        write_name_only_selection(block, label)
        assert manager.active_exit_selection() == GatewaySelection("legacy", label)
        assert manager._selected_gateway_nodes() == []
        assert manager._gateway_nodes_for_testing("tier1") == []
        assert manager._selected_gateway_is_healthy() is False
        assert manager._gateway_egress_failure_reason() == (
            f"selected Gateway exit is unavailable: {label} (missing)"
        )
    # A name only one node carries still names that node.
    write_name_only_selection(block, "HK")
    assert ids(manager._selected_gateway_nodes()) == [id(hk)]
    assert manager._gateway_egress_failure_reason() == ""

    # An ambiguous old name, or a record this build cannot verify: pick again.
    write_unresolved_selection(block, "JP 01-2")
    assert manager.active_exit_selection().state == "unresolved"
    assert manager._exit_active() is True
    assert manager._selected_gateway_nodes() == []
    assert manager._gateway_nodes_for_testing("tier1") == []
    assert manager._gateway_egress_failure_reason() == "selected Gateway exit must be picked again: JP 01-2"
    block["selected_node"] = {"identity": "v2:" + "0" * 64, "label": "JP 01-2"}
    assert read_selection(block).state == "invalid"
    assert manager._selected_gateway_nodes() == []
    assert manager._gateway_egress_failure_reason() == "selected Gateway exit must be picked again: JP 01-2"

    # No selection: the Gateway is off and nothing is reported.
    assert clear_gateway_selection(block) is True
    assert manager._exit_active() is False
    assert manager._selected_gateway_nodes() == []
    assert manager._gateway_nodes_for_testing("tier1") == []
    assert manager._gateway_egress_failure_reason() == ""


# ======================================================= 2. legacy exit inventory

SUB_URL = "https://exit.example.invalid/sub-u"
SECOND_URL = "https://exit.example.invalid/sub-v"
MANUAL = {"name": "Manual GW", "uri": "trojan://manual-pass@manual.example.invalid:443#Manual"}
MANUAL_OFF = {"name": "Manual Off", "uri": "trojan://off-pass@off.example.invalid:443#Off", "enabled": False}
MANUAL_LATER = {"name": "Manual Later", "uri": "trojan://later-pass@later.example.invalid:443#Later"}


def entry_identity(entry):
    (node,) = exit_nodes_from_entry(entry, 0)
    return gateway_identity(node)


def write_exit_cache(path, nodes, urls):
    snapshot = PoolSnapshot(generated_at="2026-11-01T00:00:00+00:00", nodes=nodes,
                            metadata={"tier": "exit", "source_urls": list(urls)})
    path.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")


def real_manager(temp_path: Path, exit_block: dict) -> ProxyTierManager:
    nodes_file = temp_path / "nodes.json"
    nodes_file.write_text('{"generated_at":"","metadata":{"tier":"tier3","source_count":0},"nodes":[]}', encoding="utf-8")
    tier2_cache_file = temp_path / "tier2_cache.json"
    tier2_cache_file.write_text('{"generated_at":"","metadata":{"tier":"tier2","source_urls":[]},"nodes":[]}', encoding="utf-8")
    sources_file = temp_path / "sources.yaml"
    sources_file.write_text("github_repos: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")
    config = {
        "paths": {
            "sources_file": str(sources_file),
            "tier3_nodes_file": str(nodes_file),
            "tier2_cache_file": str(tier2_cache_file),
            "exit_cache_file": str(temp_path / "exit_cache.json"),
            "tier_observation_cache_file": str(temp_path / "tier_observation_cache.json"),
        },
        "tiers": {
            "tier1": {"nodes": [], "check_interval": 30},
            "tier2": {"subscription_urls": [], "cache_file": str(tier2_cache_file), "check_interval": 60},
            "tier3": {"enabled": False, "check_interval": 120, "auto_collect_interval": 3600, "max_pool_size": 100},
            "exit": {"cache_file": str(temp_path / "exit_cache.json"), "check_interval": 60, **exit_block},
        },
        "testing": {"tcp_timeout": 1, "concurrency": 5, "slow_threshold_ms": 1000, "validation_mode": "tcp_only"},
    }
    return ProxyTierManager(
        config=config,
        logger=logging.getLogger("test-manager-gateway-identity"),
        alerts=AlertManager({"desktop": {"enabled": False}, "telegram": {"enabled": False},
                             "sound": {"enabled": False}}, logging.getLogger("test-alerts-gateway-identity")),
    )


def test_legacy_inventory_includes_cache_and_manual_exits_and_settledness(tmp_path):
    cached = [exit_node(f"Exit {index:02d}", f"exit-{index:02d}.example.invalid", source=SUB_URL)
              for index in range(60)]
    # A twin of the first runtime exit (same name, another server) that the
    # 50-node pool cap leaves out of the runtime list.
    capped_twin = cached[55] = exit_node("Exit 00", "exit-00-twin.example.invalid", source=SUB_URL)
    exit_cache = tmp_path / "exit_cache.json"
    write_exit_cache(exit_cache, cached, [SUB_URL])
    manager = real_manager(tmp_path, {"subscription_urls": [SUB_URL], "nodes": [dict(MANUAL), dict(MANUAL_OFF)]})
    assert manager._exit_max_pool_size() == 50
    runtime_exits = [node.clone() for node in cached[:50]]
    manager.runtime_nodes["exit"] = runtime_exits
    assert gateway_identity(capped_twin) not in identities(runtime_exits)
    assert len(gateway_legacy_family("Exit 00", manager.gateway_exit_inventory())) == 1

    nodes, settled = manager.gateway_legacy_exit_inventory()
    assert settled is True
    found = identities(nodes)
    assert identities(runtime_exits) <= found
    assert gateway_identity(capped_twin) in found
    assert entry_identity(MANUAL) in found
    assert entry_identity({**MANUAL_OFF, "enabled": True}) not in found      # disabled entries carry nothing
    assert gateway_legacy_family("Exit 00", nodes) == {gateway_identity(cached[0]), gateway_identity(capped_twin)}
    assert all(node.tier == "exit" for node in nodes)
    assert not any(node is runtime for node in nodes for runtime in runtime_exits)   # clones only

    # No file or configuration change: the memo serves, no snapshot load.
    with patch.object(manager, "_load_exit_cache_snapshot", wraps=manager._load_exit_cache_snapshot) as load, \
            patch.object(manager, "_load_exit_static_nodes", wraps=manager._load_exit_static_nodes) as static:
        again, settled_again = manager.gateway_legacy_exit_inventory()
    assert (load.call_count, static.call_count) == (0, 0)
    assert settled_again is True
    assert sorted(gateway_identity(node) for node in again) == sorted(gateway_identity(node) for node in nodes)

    # A second enabled subscription with no nodes yet: not settled.
    manager.config["tiers"]["exit"]["subscription_urls"].append(SECOND_URL)
    with patch.object(manager, "_load_exit_cache_snapshot", wraps=manager._load_exit_cache_snapshot) as load:
        nodes, settled = manager.gateway_legacy_exit_inventory()
    assert load.call_count == 1
    assert settled is False
    assert gateway_identity(capped_twin) in identities(nodes)

    # The refreshed cache represents it: read again, settled.
    fresh = exit_node("Fresh", "fresh.example.invalid", source=SECOND_URL)
    write_exit_cache(exit_cache, [*cached, fresh], [SUB_URL, SECOND_URL])
    with patch.object(manager, "_load_exit_cache_snapshot", wraps=manager._load_exit_cache_snapshot) as load:
        nodes, settled = manager.gateway_legacy_exit_inventory()
    assert load.call_count == 1
    assert settled is True
    assert gateway_identity(fresh) in identities(nodes)

    # A reloaded manual list (a new list object) is read again.
    manager.config["tiers"]["exit"]["nodes"] = [dict(MANUAL), dict(MANUAL_LATER)]
    nodes, settled = manager.gateway_legacy_exit_inventory()
    assert {entry_identity(MANUAL), entry_identity(MANUAL_LATER)} <= identities(nodes)
    assert settled is True


def test_legacy_inventory_follows_manual_entry_edits_whatever_the_list_object(tmp_path):
    """The memo is keyed on the manual entries' content: two replacements of
    the list (a freed list's id can come back) and an in-place edit (the id
    stays) are both read again."""

    write_exit_cache(tmp_path / "exit_cache.json", [], [SUB_URL])
    manager = real_manager(tmp_path, {"subscription_urls": [], "nodes": [dict(MANUAL)]})
    manager.runtime_nodes["exit"] = []   # only the configured entries count here
    nodes, _settled = manager.gateway_legacy_exit_inventory()
    assert entry_identity(MANUAL) in identities(nodes)

    for _round in range(20):
        manager.config["tiers"]["exit"]["nodes"] = [dict(MANUAL_LATER)]   # E2 (freed next)
        manager.config["tiers"]["exit"]["nodes"] = [dict(MANUAL)]         # E3 may reuse E1's id
        manager.config["tiers"]["exit"]["nodes"] = [dict(MANUAL_LATER)]
        nodes, _settled = manager.gateway_legacy_exit_inventory()
        assert entry_identity(MANUAL_LATER) in identities(nodes)
        assert entry_identity(MANUAL) not in identities(nodes)
        manager.config["tiers"]["exit"]["nodes"] = [dict(MANUAL)]
        nodes, _settled = manager.gateway_legacy_exit_inventory()
        assert entry_identity(MANUAL) in identities(nodes)

    in_place = manager.config["tiers"]["exit"]["nodes"]
    in_place.append(dict(MANUAL_LATER))
    nodes, _settled = manager.gateway_legacy_exit_inventory()
    assert {entry_identity(MANUAL), entry_identity(MANUAL_LATER)} <= identities(nodes)


def test_an_empty_subscription_the_last_refresh_covered_is_settled(tmp_path):
    """Review NF1: an enabled subscription that yields no node (expired,
    empty) is accounted for once a completed refresh covered it, so a unique
    old name can still be bound; one added after that refresh is awaited."""

    cached = [exit_node("Exit 00", "exit-00.example.invalid", source=SUB_URL)]
    write_exit_cache(tmp_path / "exit_cache.json", cached, [SUB_URL, SECOND_URL])
    manager = real_manager(tmp_path, {"subscription_urls": [SUB_URL, SECOND_URL], "nodes": []})
    _nodes, settled = manager.gateway_legacy_exit_inventory()
    assert settled is True
    manager.config["tiers"]["exit"]["subscription_urls"].append("https://exit.example.invalid/sub-w")
    _nodes, settled = manager.gateway_legacy_exit_inventory()
    assert settled is False


def test_dedupe_losers_are_no_twins(tmp_path):
    """Review CMP-2: two cached nodes the runtime merges (same server, port,
    protocol and password; another SNI) are one exit to the user: the merged
    copy never reached the export, so it cannot make an old name ambiguous."""

    first = exit_node("JP 01", "jp.example.invalid", source=SUB_URL)
    second = exit_node("JP 01", "jp.example.invalid", source=SUB_URL)
    second.sni = "other-sni.example.invalid"
    assert gateway_identity(first) != gateway_identity(second)
    write_exit_cache(tmp_path / "exit_cache.json", [first, second], [SUB_URL])
    manager = real_manager(tmp_path, {"subscription_urls": [SUB_URL], "nodes": []})
    manager.runtime_nodes["exit"] = manager._dedupe_runtime_nodes([first.clone(), second.clone()])
    assert len(manager.runtime_nodes["exit"]) == 1
    nodes, settled = manager.gateway_legacy_exit_inventory()
    assert settled is True
    assert len(gateway_legacy_family("JP 01", nodes)) == 1


def test_gateway_exit_inventory_lists_every_status_and_feeds_the_twin_check(tmp_path):
    """Review CT-3: the real inventory holds runtime exits of every status
    (clones), and a DEAD twin in it makes an old name-only selection fail
    closed in the render, never served as the surviving namesake."""

    from clash_formatter import build_full_clash_config
    import yaml

    manager = real_manager(tmp_path, {"subscription_urls": [], "nodes": []})
    dead = exit_node("JP 01", "dead.example.invalid", status=NodeStatus.DEAD, password="pw-d")
    slow = exit_node("JP 01", "slow.example.invalid", status=NodeStatus.SLOW, password="pw-s")
    manager.runtime_nodes["exit"] = [dead, slow]
    inventory = manager.gateway_exit_inventory()
    assert identities(inventory) == identities([dead, slow])
    assert not any(node is original for node in inventory for original in (dead, slow))

    document = yaml.safe_load(build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": [], "exit": [slow]},   # the export keeps ALIVE/SLOW
        active_exit_proxy="JP 01",
        exit_inventory=inventory,
    ))
    groups = {group["name"]: group["proxies"] for group in document["proxy-groups"]}
    assert groups["EXIT-SELECT"] == ["REJECT"]
