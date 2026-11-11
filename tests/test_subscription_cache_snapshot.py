from __future__ import annotations

from pathlib import Path

from collector import save_snapshot
from models import Node, NodeStatus, PoolSnapshot
from subscription_cache_snapshot import persisted_subscription_node_snapshot


TIER2_URL = "https://subscriptions.example/tier2"
EXIT_URL = "https://subscriptions.example/exit"


def _config(
    tmp_path: Path,
    *,
    tier2_urls: list[object] | None = None,
    exit_urls: list[object] | None = None,
) -> dict[str, object]:
    return {
        "paths": {
            "tier2_cache_file": str(tmp_path / "tier2-cache.json"),
            "exit_cache_file": str(tmp_path / "exit-cache.json"),
        },
        "tiers": {
            "tier2": {"subscription_urls": tier2_urls or []},
            "exit": {"subscription_urls": exit_urls or []},
            "tier3": {
                "subscription_urls": ["https://subscriptions.example/tier3"],
                "cache_file": str(tmp_path / "tier3-cache.json"),
            },
        },
    }


def _node(
    name: str,
    *,
    tier: str,
    source: str,
    port: int,
    status: NodeStatus = NodeStatus.ALIVE,
    latency_ms: float | None = 42.0,
    last_checked_at: str | None = "2026-07-30T01:02:03+00:00",
    sources: list[str] | None = None,
) -> Node:
    return Node(
        name=name,
        protocol="vless",
        address=f"{name}.example",
        port=port,
        source=source,
        sources=list(sources or []),
        tier=tier,
        status=status,
        latency_ms=latency_ms,
        last_checked_at=last_checked_at,
    )


def _write_cache(
    path: Path,
    *,
    tier: str,
    source_urls: list[str],
    nodes: list[Node],
    generated_at: str = "2026-07-30T00:00:00+00:00",
) -> None:
    assert save_snapshot(
        PoolSnapshot(
            generated_at=generated_at,
            metadata={"tier": tier, "source_urls": source_urls},
            nodes=nodes,
        ),
        path,
    )


def test_cold_start_restores_tier2_and_exit_as_persisted_observations(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        tier2_urls=[TIER2_URL],
        exit_urls=[EXIT_URL],
    )
    tier2_checked_at = "2026-07-29T23:50:00+00:00"
    exit_generated_at = "2026-07-29T23:55:00+00:00"
    _write_cache(
        tmp_path / "tier2-cache.json",
        tier="tier2",
        source_urls=[TIER2_URL],
        nodes=[
            _node(
                "tier2-cached",
                tier="tier2",
                source=TIER2_URL,
                port=443,
                status=NodeStatus.SLOW,
                latency_ms=812.5,
                last_checked_at=tier2_checked_at,
            )
        ],
    )
    _write_cache(
        tmp_path / "exit-cache.json",
        tier="exit",
        source_urls=[EXIT_URL],
        nodes=[
            _node(
                "exit-cached",
                tier="exit",
                source=EXIT_URL,
                port=8443,
                status=NodeStatus.ALIVE,
                latency_ms=67.0,
                last_checked_at=None,
            )
        ],
        generated_at=exit_generated_at,
    )

    snapshot = persisted_subscription_node_snapshot(config)

    assert snapshot["snapshot_origin"] == "persisted_cache"
    assert set(snapshot["tiers"]) == {"tier2", "exit"}
    tier2 = snapshot["tiers"]["tier2"]
    exit_tier = snapshot["tiers"]["exit"]
    assert (tier2["total"], tier2["healthy"], tier2["cache_state"]) == (
        1,
        1,
        "loaded",
    )
    assert (exit_tier["total"], exit_tier["healthy"], exit_tier["cache_state"]) == (
        1,
        0,
        "loaded",
    )
    assert tier2["nodes"][0]["status"] == "slow"
    assert tier2["nodes"][0]["latency_ms"] == 812.5
    assert tier2["nodes"][0]["observation_origin"] == "persisted_cache"
    assert tier2["nodes"][0]["observed_at"] == tier2_checked_at
    assert tier2["nodes"][0]["cached_at"] == "2026-07-30T00:00:00+00:00"
    assert exit_tier["nodes"][0]["status"] == "alive"
    assert exit_tier["nodes"][0]["observation_origin"] == "persisted_cache"
    assert exit_tier["nodes"][0]["observed_at"] is None
    assert exit_tier["nodes"][0]["cached_at"] == exit_generated_at
    assert exit_tier["cached_at"] == exit_generated_at


def test_missing_and_corrupt_caches_are_distinguished_without_nodes(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        tier2_urls=[TIER2_URL],
        exit_urls=[EXIT_URL],
    )
    (tmp_path / "exit-cache.json").write_text(
        "{this is not a snapshot",
        encoding="utf-8",
    )

    snapshot = persisted_subscription_node_snapshot(config)

    tier2 = snapshot["tiers"]["tier2"]
    exit_tier = snapshot["tiers"]["exit"]
    assert tier2["cache_state"] == "missing"
    assert tier2["cache_generated_at"] is None
    assert tier2["nodes"] == []
    assert tier2["total"] == 0
    assert exit_tier["cache_state"] == "invalid"
    assert exit_tier["nodes"] == []
    assert exit_tier["total"] == 0


def test_disabled_and_deleted_subscription_urls_are_filtered(
    tmp_path: Path,
) -> None:
    enabled_url = "https://subscriptions.example/enabled"
    disabled_url = "https://subscriptions.example/disabled"
    deleted_url = "https://subscriptions.example/deleted"
    config = _config(
        tmp_path,
        tier2_urls=[
            {"url": enabled_url, "enabled": True},
            {"url": disabled_url, "enabled": False},
        ],
    )
    _write_cache(
        tmp_path / "tier2-cache.json",
        tier="tier2",
        source_urls=[enabled_url, disabled_url, deleted_url],
        nodes=[
            _node(
                "enabled",
                tier="tier2",
                source=enabled_url,
                port=443,
            ),
            _node(
                "disabled",
                tier="tier2",
                source=disabled_url,
                port=444,
            ),
            _node(
                "deleted",
                tier="tier2",
                source=deleted_url,
                port=445,
            ),
        ],
    )

    snapshot = persisted_subscription_node_snapshot(config)

    nodes = snapshot["tiers"]["tier2"]["nodes"]
    assert [node["name"] for node in nodes] == ["enabled"]
    assert nodes[0]["source"] == enabled_url


def test_every_cached_subscription_source_uses_persisted_origin(
    tmp_path: Path,
) -> None:
    first_url = "https://subscriptions.example/first"
    second_url = "https://subscriptions.example/second"
    config = _config(tmp_path, tier2_urls=[first_url, second_url])
    _write_cache(
        tmp_path / "tier2-cache.json",
        tier="tier2",
        source_urls=[first_url, second_url],
        nodes=[
            _node(
                "first",
                tier="tier2",
                source=first_url,
                port=443,
            ),
            _node(
                "second",
                tier="tier2",
                source="",
                sources=[second_url],
                port=444,
            ),
        ],
    )

    snapshot = persisted_subscription_node_snapshot(config)

    nodes_by_name = {
        node["name"]: node for node in snapshot["tiers"]["tier2"]["nodes"]
    }
    assert nodes_by_name["first"]["observation_origin"] == "persisted_cache"
    assert nodes_by_name["second"]["observation_origin"] == "persisted_cache"
    assert nodes_by_name["first"]["observed_at"] == "2026-07-30T01:02:03+00:00"
    assert nodes_by_name["second"]["observed_at"] == "2026-07-30T01:02:03+00:00"


def test_tier3_cache_is_never_restored_by_persisted_subscription_snapshot(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, tier2_urls=[TIER2_URL])
    tier3_url = "https://subscriptions.example/tier3"
    _write_cache(
        tmp_path / "tier3-cache.json",
        tier="tier3",
        source_urls=[tier3_url],
        nodes=[
            _node(
                "tier3-unverified",
                tier="tier3",
                source=tier3_url,
                port=9443,
            )
        ],
    )

    snapshot = persisted_subscription_node_snapshot(config)

    assert set(snapshot["tiers"]) == {"tier2", "exit"}
    assert "tier3" not in snapshot["tiers"]
    assert all(
        node["name"] != "tier3-unverified"
        for tier in snapshot["tiers"].values()
        for node in tier["nodes"]
    )
