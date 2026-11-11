from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

import yaml

from collection_settings import collection_max_bytes, collection_timeout_seconds, collection_user_agents
from config_item_identity import source_entry_value
from config_utils import coerce_bool, coerce_positive_int
from collector import DEFAULT_USER_AGENTS, load_yaml, proxy_url_from_config
from parser import parse_content_blob
from tier3_sources import DEFAULT_GITHUB_CANDIDATE_PATHS, DEFAULT_SOURCE_MAX_BYTES


DEFAULT_CANDIDATE_PATHS = list(DEFAULT_GITHUB_CANDIDATE_PATHS)

DEFAULT_QUERIES = [
    "v2ray free subscription",
    "vless vmess trojan free",
    "clash free nodes",
    "proxy subscription v2ray",
]


@dataclass(frozen=True)
class CandidateSource:
    url: str
    repo: str
    path: str
    node_count: int


@dataclass(frozen=True)
class GitHubDiscoveryLimits:
    max_repositories: int
    min_nodes_per_file: int


def github_discovery_limits(
    discovery_config: dict[str, Any],
    *,
    max_repositories_override: int | None = None,
    min_nodes_override: int | None = None,
) -> GitHubDiscoveryLimits:
    config = discovery_config if isinstance(discovery_config, dict) else {}
    return GitHubDiscoveryLimits(
        max_repositories=coerce_positive_int(
            max_repositories_override if max_repositories_override is not None else config.get("max_repositories", 30),
            30,
        ),
        min_nodes_per_file=coerce_positive_int(
            min_nodes_override if min_nodes_override is not None else config.get("min_nodes_per_file", 3),
            3,
        ),
    )


def _headers(user_agent: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _build_opener(proxy_url: str | None):
    if not proxy_url:
        raise RuntimeError(
            "GitHub discovery requires a routed proxy; host-default direct access is disabled"
        )
    return build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))


def _fetch_text(
    opener,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    logger: logging.Logger,
    *,
    max_bytes: int = DEFAULT_SOURCE_MAX_BYTES,
) -> str | None:
    try:
        request = Request(url, headers=headers)
        with opener.open(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    expected_bytes = int(content_length)
                except ValueError:
                    expected_bytes = 0
                if expected_bytes > max_bytes:
                    logger.debug("Skipping %s because Content-Length exceeds %s bytes", url, max_bytes)
                    return None
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    logger.debug("Skipping %s because response exceeds %s bytes", url, max_bytes)
                    return None
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="ignore")
    except HTTPError as exc:
        logger.debug("HTTP %s for %s", exc.code, url)
    except URLError as exc:
        logger.debug("URL error for %s: %s", url, exc)
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
    return None


def _quote_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


def raw_github_url(repo_full_name: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo_full_name}/{quote(branch, safe='')}/{_quote_path(path)}"


def existing_source_values(sources_config: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for section in ("github_repos", "telegram_channels", "subscription_urls", "local_files"):
        for entry in sources_config.get(section, []) or []:
            value = source_entry_value(entry)
            if value:
                values.add(value)
    return values


def merge_discovered_sources(
    sources_config: dict[str, Any],
    candidates: Iterable[CandidateSource],
) -> tuple[dict[str, Any], int]:
    merged = dict(sources_config)
    github_repos = list(merged.get("github_repos", []) or [])
    existing = existing_source_values(merged)
    added = 0

    for candidate in candidates:
        if candidate.url in existing:
            continue
        github_repos.append(
            {
                "url": candidate.url,
                "enabled": True,
                "tags": ["github-discovered", candidate.repo],
                "node_count": candidate.node_count,
            }
        )
        existing.add(candidate.url)
        added += 1

    merged["github_repos"] = github_repos
    return merged, added


def _search_repositories(
    opener,
    queries: Iterable[str],
    headers: dict[str, str],
    timeout_seconds: int,
    max_repositories: int,
    logger: logging.Logger,
    max_bytes: int = DEFAULT_SOURCE_MAX_BYTES,
) -> list[dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    seen: set[str] = set()

    for query in queries:
        params = urlencode(
            {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": min(50, max(1, max_repositories)),
            }
        )
        url = f"https://api.github.com/search/repositories?{params}"
        body = _fetch_text(opener, url, headers, timeout_seconds, logger, max_bytes=max_bytes)
        if not body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.debug("GitHub search returned non-JSON payload for query %s", query)
            continue

        for item in payload.get("items", []):
            full_name = item.get("full_name")
            if not full_name or full_name in seen:
                continue
            repositories.append(item)
            seen.add(full_name)
            if len(repositories) >= max_repositories:
                return repositories
        time.sleep(0.5)

    return repositories


def discover_github_sources(
    config: dict[str, Any],
    sources_config: dict[str, Any],
    *,
    max_repositories: int,
    min_nodes_per_file: int,
    limit: int,
    logger: logging.Logger,
    proxy_url: str | None = None,
) -> list[CandidateSource]:
    discovery_config = sources_config.get("github_discovery", {}) or {}
    candidate_paths = list(discovery_config.get("candidate_paths") or DEFAULT_CANDIDATE_PATHS)
    queries = list(discovery_config.get("queries") or DEFAULT_QUERIES)
    settings = config.get("testing", {}) or {}
    timeout_seconds = collection_timeout_seconds(settings)
    max_bytes = collection_max_bytes(settings)
    user_agents = collection_user_agents(settings, DEFAULT_USER_AGENTS)
    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    proxy_url = proxy_url or proxy_url_from_config(
        config.get("egress_proxy"),
        purpose="collection",
    )
    opener = _build_opener(proxy_url)
    headers = _headers(random.choice(user_agents), token=token)

    repositories = _search_repositories(
        opener,
        queries,
        headers,
        timeout_seconds,
        max_repositories,
        logger,
        max_bytes=max_bytes,
    )

    candidates: list[CandidateSource] = []
    for repo in repositories:
        full_name = repo.get("full_name")
        default_branch = repo.get("default_branch") or "main"
        if not full_name:
            continue

        for path in candidate_paths:
            raw_url = raw_github_url(full_name, default_branch, path)
            text = _fetch_text(opener, raw_url, headers, timeout_seconds, logger, max_bytes=max_bytes)
            if not text:
                continue
            nodes = parse_content_blob(text, source=raw_url, tier="tier3", logger=logger)
            if len(nodes) < min_nodes_per_file:
                continue
            candidates.append(
                CandidateSource(
                    url=raw_url,
                    repo=full_name,
                    path=path,
                    node_count=len(nodes),
                )
            )
            break

        if len(candidates) >= limit:
            break

    return candidates


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover GitHub-hosted free proxy node sources.")
    parser.add_argument("--config", default="config.yaml", help="Path to the main proxy-manager config.")
    parser.add_argument("--sources", default="sources.yaml", help="Path to the tier3 sources file.")
    parser.add_argument("--apply", action="store_true", help="Write discovered sources back to sources.yaml.")
    parser.add_argument("--force", action="store_true", help="Run even when github_discovery.enabled is false.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum new raw sources to report.")
    parser.add_argument("--max-repositories", type=int, default=None, help="Maximum GitHub repositories to inspect.")
    parser.add_argument("--min-nodes", type=int, default=None, help="Minimum parsed nodes required for a file.")
    parser.add_argument("--verbose", action="store_true", help="Show debug logs for failed candidates.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("github-free-node-finder")

    config_path = Path(args.config)
    sources_path = Path(args.sources)
    config = load_yaml(config_path) if config_path.exists() else {}
    sources_config = load_yaml(sources_path)
    discovery_config = sources_config.get("github_discovery", {}) or {}

    if not args.force and not coerce_bool(discovery_config.get("enabled", True), True):
        logger.info("GitHub discovery is disabled in %s", sources_path)
        return 0

    limits = github_discovery_limits(
        discovery_config,
        max_repositories_override=args.max_repositories,
        min_nodes_override=args.min_nodes,
    )
    candidates = discover_github_sources(
        config,
        sources_config,
        max_repositories=limits.max_repositories,
        min_nodes_per_file=limits.min_nodes_per_file,
        limit=max(1, args.limit),
        logger=logger,
    )

    existing = existing_source_values(sources_config)
    new_candidates = [candidate for candidate in candidates if candidate.url not in existing]
    if not new_candidates:
        logger.info("No new valid GitHub sources found.")
        return 0

    for candidate in new_candidates:
        print(f"{candidate.node_count:4d} nodes  {candidate.url}")

    if not args.apply:
        logger.info("Dry run only. Re-run with --apply to update %s.", sources_path)
        return 0

    merged, added = merge_discovered_sources(sources_config, new_candidates)
    sources_path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    logger.info("Added %s new GitHub sources to %s.", added, sources_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
