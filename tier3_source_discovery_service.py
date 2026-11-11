from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from config_utils import coerce_bool, coerce_positive_int
from github_free_node_finder import (
    CandidateSource,
    discover_github_sources,
    existing_source_values,
    github_discovery_limits,
    merge_discovered_sources,
)


Tier3SourceDiscoverer = Callable[..., list[CandidateSource]]


@dataclass(frozen=True)
class Tier3SourceDiscoveryOutcome:
    result: dict[str, Any]
    merged_sources: dict[str, Any] | None


def discover_tier3_sources_update(
    *,
    config: dict[str, Any],
    sources_config: dict[str, Any],
    limit: int,
    force: bool,
    logger: logging.Logger,
    discoverer: Tier3SourceDiscoverer = discover_github_sources,
    proxy_url: str | None = None,
) -> Tier3SourceDiscoveryOutcome:
    discovery_config = (
        sources_config.get("github_discovery", {})
        if isinstance(sources_config.get("github_discovery"), dict)
        else {}
    )
    if not force and not coerce_bool(discovery_config.get("enabled", True), True):
        return Tier3SourceDiscoveryOutcome(
            result={"enabled": False, "discovered": 0, "added": 0, "sources": []},
            merged_sources=None,
        )
    limits = github_discovery_limits(discovery_config)
    candidates = discoverer(
        config,
        sources_config,
        max_repositories=limits.max_repositories,
        min_nodes_per_file=limits.min_nodes_per_file,
        limit=coerce_positive_int(limit, 10),
        logger=logger,
        proxy_url=proxy_url,
    )
    existing = existing_source_values(sources_config)
    new_candidates = [candidate for candidate in candidates if candidate.url not in existing]
    if not new_candidates:
        return Tier3SourceDiscoveryOutcome(
            result={"enabled": True, "discovered": len(candidates), "added": 0, "sources": []},
            merged_sources=None,
        )
    merged, added = merge_discovered_sources(sources_config, new_candidates)
    return Tier3SourceDiscoveryOutcome(
        result={
            "enabled": True,
            "discovered": len(candidates),
            "added": added,
            "sources": [
                {
                    "url": candidate.url,
                    "repo": candidate.repo,
                    "path": candidate.path,
                    "node_count": candidate.node_count,
                }
                for candidate in new_candidates
            ],
        },
        merged_sources=merged,
    )
