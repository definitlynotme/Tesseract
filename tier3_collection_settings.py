from __future__ import annotations

from typing import Any, Iterable


def collection_settings(testing_config: dict[str, Any], egress_proxy: Any) -> dict[str, Any]:
    settings = dict(testing_config)
    if egress_proxy:
        settings["egress_proxy"] = egress_proxy
    return settings


def merge_github_candidate_paths(
    configured_paths: Iterable[Any],
    default_paths: Iterable[str],
) -> list[str]:
    merged_paths: list[str] = []
    for path in [*default_paths, *[str(value) for value in configured_paths if str(value).strip()]]:
        if path and path not in merged_paths:
            merged_paths.append(path)
    return merged_paths


def tier3_collection_settings(
    base_settings: dict[str, Any],
    sources_payload: dict[str, Any],
    *,
    default_github_candidate_paths: Iterable[str],
) -> dict[str, Any]:
    settings = dict(base_settings)
    discovery = sources_payload.get("github_discovery", {}) if isinstance(sources_payload.get("github_discovery"), dict) else {}
    if discovery.get("candidate_paths"):
        settings["github_candidate_paths"] = merge_github_candidate_paths(
            discovery.get("candidate_paths") or [],
            default_github_candidate_paths,
        )
    # The current source file, not a stale testing snapshot, owns delivery.
    delivery = sources_payload.get("github_delivery")
    if "github_delivery" in sources_payload or "github_delivery" in settings:
        settings["github_delivery"] = dict(delivery) if isinstance(delivery, dict) else {}
    profiles = sources_payload.get('fetch_profiles')
    if 'fetch_profiles' in sources_payload or 'fetch_profiles' in settings:
        settings['fetch_profiles'] = dict(profiles) if isinstance(profiles, dict) else {}
    return settings
