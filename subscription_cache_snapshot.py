from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from collector import load_snapshot
from config_utils import enabled_subscription_urls
from models import Node, PoolSnapshot, count_healthy_nodes, sort_nodes_for_use
from node_status_view import node_identity, node_status_snapshot
from runtime_settings import as_string_list, paths_block, tier_block


PERSISTED_SUBSCRIPTION_TIERS = ("tier2", "exit")
DEFAULT_CACHE_FILES = {
    "tier2": "data/tier2_cache.json",
    "exit": "data/exit_cache.json",
}


def node_has_subscription_source(node: Node, url: str) -> bool:
    target = str(url or "").strip()
    if not target:
        return False
    values: list[Any] = [node.source, *list(node.sources or [])]
    extra = node.extra if isinstance(node.extra, dict) else {}
    for key in ("source", "source_value", "subscription_url"):
        values.append(extra.get(key))
    sources = extra.get("sources")
    if isinstance(sources, (list, tuple, set)):
        values.extend(sources)
    return target in {
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    }


def cache_nodes_for_current_subscription_urls(
    snapshot: PoolSnapshot,
    current_urls: Iterable[str],
) -> list[Node]:
    configured = {
        str(url or "").strip()
        for url in current_urls
        if str(url or "").strip()
    }
    if not configured:
        return []
    snapshot_urls = {
        str(url or "").strip()
        for url in as_string_list(snapshot.metadata.get("source_urls", []))
        if str(url or "").strip()
    }
    if snapshot_urls == configured:
        return list(snapshot.nodes)
    if snapshot_urls and not (snapshot_urls & configured):
        return []
    filtered = [
        node
        for node in snapshot.nodes
        if any(node_has_subscription_source(node, url) for url in configured)
    ]
    if filtered or snapshot_urls:
        return filtered
    # Legacy snapshots did not record source URLs. Preserve their historical
    # behavior until a successful refresh writes source metadata.
    return list(snapshot.nodes)


def subscription_cache_file(config: dict[str, Any], tier: str) -> Path:
    tier_id = str(tier or "").strip()
    if tier_id not in PERSISTED_SUBSCRIPTION_TIERS:
        raise ValueError(f"persisted subscription cache is unsupported for {tier_id}")
    tier_config = tier_block(config, tier_id)
    paths = paths_block(config)
    configured = (
        tier_config.get("cache_file")
        or paths.get(f"{tier_id}_cache_file")
        or DEFAULT_CACHE_FILES[tier_id]
    )
    return Path(str(configured))


def persisted_subscription_node_snapshot(
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build a read-only Tier2/Exit status snapshot from persisted caches.

    Tier3 is deliberately excluded: its cached automatic pool must continue
    through the existing startup revalidation path.
    """

    tiers: dict[str, Any] = {}
    for tier in PERSISTED_SUBSCRIPTION_TIERS:
        configured_urls = enabled_subscription_urls(
            tier_block(config, tier).get("subscription_urls", [])
        )
        cache_path = subscription_cache_file(config, tier)
        try:
            existed = cache_path.exists()
        except OSError:
            existed = True
        snapshot = load_snapshot(cache_path)
        nodes = sort_nodes_for_use(
            cache_nodes_for_current_subscription_urls(
                snapshot,
                configured_urls,
            )
        )
        node_payloads: list[dict[str, Any]] = []
        for node in nodes:
            node.tier = tier
            payload = node_status_snapshot(node, identity=node_identity(node))
            payload["observation_origin"] = "persisted_cache"
            # Cache generation records when the membership inventory was
            # stored.  It is not evidence that a health probe completed then.
            payload["cached_at"] = snapshot.generated_at or None
            payload["observed_at"] = node.last_checked_at or None
            node_payloads.append(payload)
        load_error = str(snapshot.metadata.get("load_error") or "").strip()
        if load_error:
            cache_state = "invalid"
        elif not existed:
            cache_state = "missing"
        elif nodes:
            cache_state = "loaded"
        else:
            cache_state = "empty"
        tiers[tier] = {
            "total": len(nodes),
            "healthy": count_healthy_nodes(
                [node for node in nodes if node.last_checked_at]
            ),
            "last_check_at": max(
                (
                    str(node.last_checked_at)
                    for node in nodes
                    if node.last_checked_at
                ),
                default=None,
            ),
            "cached_at": snapshot.generated_at if existed else None,
            "cache_generated_at": snapshot.generated_at if existed else None,
            "cache_state": cache_state,
            "nodes": node_payloads,
        }
    return {
        "snapshot_origin": "persisted_cache",
        "tiers": tiers,
    }
