from __future__ import annotations

from typing import Any, Callable

from collector import source_entries_from_config
from config_utils import coerce_bool
from models import Node
from tier3_collection_plan import (
    tier3_collection_entry_groups,
)
from tier3_collection_report import merge_collection_reports
from tier3_refresh_controller import Tier3ControllerDecision, Tier3RefreshPlan
from tier3_refresh_types import Tier3RefreshInputs, Tier3SourceCollectionResult
from tier3_sources import CollectionReport, source_entries_from_tier3_config


Tier3EntryCollector = Callable[[list[dict[str, Any]], dict[str, Any]], tuple[list[Node], CollectionReport]]
Tier3CollectionProgressObserver = Callable[[CollectionReport, list[Node]], None]
Tier3CollectionBatchObserver = Callable[[list[Node], CollectionReport], bool | None]


def _canonical_source_type(value: Any) -> str:
    source_type = str(value or "subscription_url").strip().lower() or "subscription_url"
    if source_type in {"github", "github_repo", "github_repos"}:
        return "github_repo"
    if source_type in {"telegram", "telegram_channel", "telegram_public_channel"}:
        return "telegram_channel"
    if source_type in {"file", "local", "local_file", "local_files"}:
        return "local_file"
    return "subscription_url"


def _dedupe_source_entries(
    entries: list[dict[str, Any]],
    *,
    seen: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    known = seen if seen is not None else set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip()
        if not value:
            continue
        key = (_canonical_source_type(entry.get("type")), value)
        if key in known:
            continue
        known.add(key)
        deduped.append(entry)
    return deduped, known


def build_tier3_refresh_inputs(
    *,
    previous_nodes: list[Node],
    sources_payload: dict[str, Any],
    refresh_plan: Tier3RefreshPlan,
    settings: dict[str, Any],
    tier3_config: dict[str, Any],
) -> Tier3RefreshInputs:
    return Tier3RefreshInputs(
        previous_nodes=list(previous_nodes),
        sources_payload=sources_payload,
        settings=settings,
        tier3_config=tier3_config,
        decision=refresh_plan.decision,
        target_alive=refresh_plan.target_alive,
        intensive_expansion=refresh_plan.intensive_expansion,
        validation_budget=refresh_plan.validation_budget,
    )


def collect_tier3_source_candidates(
    *,
    tier3_config: dict[str, Any],
    sources_payload: dict[str, Any],
    settings: dict[str, Any],
    decision: Tier3ControllerDecision,
    previous_nodes: list[Node],
    previous_alive_count: int,
    target_alive: int,
    min_alive_nodes: int,
    intensive_expansion: bool,
    startup_mode: bool,
    collect_entries: Tier3EntryCollector,
    progress_observer: Tier3CollectionProgressObserver | None = None,
    batch_observer: Tier3CollectionBatchObserver | None = None,
    stream_sources: bool = False,
) -> Tier3SourceCollectionResult:
    collected_nodes: list[Node] = []
    reports: list[CollectionReport] = []
    merged_report = CollectionReport()
    tier3a_entries, configured_source_keys = _dedupe_source_entries(
        source_entries_from_tier3_config(tier3_config)
    )
    tier3b_source_enabled = coerce_bool(tier3_config.get("bootstrap_enabled", True), True)
    tier3b_entries, _configured_source_keys = _dedupe_source_entries(
        source_entries_from_config(sources_payload) if tier3b_source_enabled else [],
        seen=configured_source_keys,
    )
    configured_entry_groups = tier3_collection_entry_groups(
        tier3a_entries,
        tier3b_entries,
        source_budget=(len(tier3b_entries) if stream_sources else decision.source_budget),
    )
    local_entries = [
        entry
        for group in configured_entry_groups
        for entry in group
        if _canonical_source_type(entry.get("type")) == "local_file"
    ]
    remote_groups = [
        [
            entry
            for entry in group
            if _canonical_source_type(entry.get("type")) != "local_file"
        ]
        for group in configured_entry_groups
    ]
    remote_groups = [group for group in remote_groups if group]
    if stream_sources and len(remote_groups) > 1:
        remote_groups = [[entry for group in remote_groups for entry in group]]
    # This is an execution barrier, not cosmetic ordering: collect_entries is
    # not invoked for any remote root until the local-file call has returned.
    entry_groups = ([local_entries] if local_entries else []) + remote_groups
    for entries in entry_groups:
        nodes, report = collect_entries(entries, settings)
        collected_nodes.extend(nodes)
        reports.append(report)
        merged_report = merge_collection_reports(reports)
        if progress_observer is not None:
            progress_observer(merged_report, list(collected_nodes))
        if batch_observer is not None and nodes and batch_observer(list(nodes), merged_report):
            return Tier3SourceCollectionResult(nodes=collected_nodes, report=merged_report)
    return Tier3SourceCollectionResult(nodes=collected_nodes, report=merged_report)
