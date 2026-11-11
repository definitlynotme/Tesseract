from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from config_utils import config_entry_enabled, enabled_subscription_urls
from clash_formatter import node_to_clash_proxy
from models import NodeStatus, count_healthy_nodes
from tier3_verification_state import (
    node_has_startup_core_verification,
    node_has_tcp_prelaunch_marker,
    tier3_startup_core_verified_nodes,
)


_MISSING_REPORT = object()
_URL_WITH_AUTHORITY_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s;,\]\[(){}<>\"']+"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|authorization|credential|secret)"
    r"(\s*[=:]\s*)([^\s;,]+)"
)
_BARE_USERINFO_RE = re.compile(
    r"(?<![\w@])[^\s:/@]+:[^\s/@]+@([A-Za-z0-9.-]+(?::\d+)?)"
)

_TIER3_SOURCE_SUPPLY_PHASE_CODES = frozenset(
    {
        "cache_revalidation",
        "local_sources",
        "pre_core_remote",
        "runtime_mixed_port",
        "startup_floor_reached",
        "blocked_pre_core",
        "blocked_core",
    }
)
_TIER3_SOURCE_SUPPLY_REASON_CODES = frozenset(
    {
        "blocked_no_route",
        "prerequisite_missing",
        "rule_blocked",
        "runtime_source_retry_disabled",
        "runtime_proxy_unavailable_after_recovery",
        "pre_gate_admitted_source_assist_blocked",
        "runtime_egress_recovery_failed",
        "source_fetch_failed",
        "dns_unresolvable",
    }
)
_TIER3_SOURCE_SUPPLY_DIAGNOSIS_CODES = frozenset(
    {
        "collecting",
        "target_met",
        "source_unit_coverage_pending",
        "source_fetch_pending",
        "source_fetch_failed_final",
        "source_fetch_blocked_no_route",
        "source_assist_blocked",
        "pre_gate_source_retry_due",
        "post_gate_runtime_source_retry_due",
        "runtime_source_retry_not_triggered",
        "input_exhausted",
        "startup_supply_exhausted",
        "source_supply_no_egress",
        "dns_rung_escalating",
        "candidate_sampling_empty",
        "candidate_sampling_not_draining",
        "tcp_stage_not_running",
        "tcp_deadline_violation",
        "tcp_handoff_slow",
        "tcp_wave_deadline_blocked",
        "tcp_responsive_low",
        "full_validation_not_queued",
        "full_validation_not_attempted",
        "full_validation_worker_exception",
        "full_validation_deferred_high",
        "full_validation_backlog",
        "validator_scheduler_idle_with_backlog",
        "validator_facility_failure",
        "validator_capacity_limited",
        "validation_root_starved",
        "transaction_head_of_line",
        "endpoint_all_transient",
        "candidate_real_probe_failed",
        "probe_portfolio_all_transient",
        "commit_queue_blocked",
        "commit_blocked",
        "admission_not_committed",
        "source_pass_rate_low",
        "unsupported_dominant",
        "manager_closed",
    }
)
_TIER3_SOURCE_SUPPLY_TERMINAL_REASON_CODES = frozenset(
    {
        "rule_blocked",
        "no_proxy",
        "transport_failed",
        "insufficient_healthy_nodes",
        "no_egress",
    }
)


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_report_code(
    value: Any,
    allowed: frozenset[str],
    *,
    fallback: str,
) -> str:
    code = str(value or "").strip()
    return code if code in allowed else fallback


def _safe_source_supply_reason_code(value: Any, *, failed_count: int) -> str:
    reason = str(value or "").strip()
    for code in _TIER3_SOURCE_SUPPLY_REASON_CODES:
        if reason == code or reason.startswith(code + ":"):
            return code
    return "source_fetch_failed" if failed_count else "none"


def _safe_startup_error_summary(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""

    def redact_url(match: re.Match[str]) -> str:
        candidate = match.group(0)
        try:
            parsed = urlsplit(candidate)
            scheme = str(parsed.scheme or "source").lower()
            host = str(parsed.hostname or "").strip()
            port = parsed.port
        except (TypeError, ValueError):
            return "source://redacted"
        if not host:
            return f"{scheme}://redacted"
        safe_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        safe_port = f":{port}" if port is not None else ""
        return f"{scheme}://{safe_host}{safe_port}"

    text = _URL_WITH_AUTHORITY_RE.sub(redact_url, text)
    text = _CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    text = _BARE_USERINFO_RE.sub(
        lambda match: f"<redacted-userinfo>@{match.group(1)}",
        text,
    )
    return text[:300]


@dataclass(frozen=True)
class Tier3StartupBackgroundExpansionDecision:
    background_expansion_due: bool
    next_last_collect_tier3: float | None


@dataclass(frozen=True)
class Tier3CachedStartVerification:
    nodes: list[Any]
    requires_core_verification: bool
    discarded_count: int


@dataclass(frozen=True)
class Tier3CoreRepairTargets:
    target_verified: int
    minimum_verified: int


@dataclass(frozen=True)
class Tier3CoreRepairApplied:
    nodes: list[Any]
    pool_nodes: list[Any]
    observed_failed_nodes: list[Any]
    current_node_count: int
    retained_node_count: int
    working_count: int
    failed_tested_count: int
    retained_failed_count: int
    evicted_failed_count: int
    selected_proxy_name: str
    selected_delay_ms: float


@dataclass(frozen=True)
class Tier3CoreProbePlan:
    concurrency: int
    batches: list[list[Any]]


@dataclass(frozen=True)
class CoreReloadFailureAction:
    unavailable_reason: str | None
    keep_existing_reason: str | None


@dataclass(frozen=True)
class CoreReloadContext:
    runtime_config_path: Path
    previous_config_text: str | None
    controller: dict[str, Any]
    current_tier3_nodes: list[Any]
    rollback_tier3_nodes: list[Any]
    provider_path: Path | None = None
    previous_provider_bytes: bytes = b""
    previous_provider_exists: bool = False
    previous_provider_projection: Any = None


class Tier3CoreSyncAction(Enum):
    NONE = "none"
    DEFERRED_START = "deferred_start"
    RELOAD_RUNNING_CORE = "reload_running_core"


def _dict_value(source: dict[str, Any] | None, key: str) -> dict[str, Any]:
    value = source.get(key, {}) if isinstance(source, dict) else {}
    return value if isinstance(value, dict) else {}


def tier3_core_delay_from_probe_result(result: Any) -> float:
    if not isinstance(result, dict):
        raise ValueError("Mihomo delay response must be a mapping")
    return float(result.get("delay", 0) or 0)


def primary_layers_configured(config: dict[str, Any]) -> bool:
    tiers = _dict_value(config, "tiers")
    tier1 = _dict_value(tiers, "tier1")
    tier2 = _dict_value(tiers, "tier2")
    tier1_nodes = tier1.get("nodes") if isinstance(tier1.get("nodes"), list) else []
    tier2_nodes = tier2.get("nodes") if isinstance(tier2.get("nodes"), list) else []
    return bool(
        any(isinstance(entry, dict) and config_entry_enabled(entry) for entry in tier1_nodes)
        or any(isinstance(entry, (dict, str)) and config_entry_enabled(entry) for entry in tier2_nodes)
        or enabled_subscription_urls(tier2.get("subscription_urls"))
    )


def primary_runtime_healthy_count(runtime_nodes: Any) -> int:
    if not isinstance(runtime_nodes, dict):
        return 0
    return count_healthy_nodes(runtime_nodes.get("tier1", [])) + count_healthy_nodes(runtime_nodes.get("tier2", []))


def primary_startup_unhealthy_message(failures: list[str]) -> str:
    suffix = f" ({'; '.join(failures[:2])})" if failures else ""
    return "Primary startup checks found no healthy nodes" + suffix


def has_unverified_tier3_tcp_candidates(nodes: list[Any]) -> bool:
    for node in nodes:
        extra = getattr(node, "extra", {})
        if not isinstance(extra, dict):
            continue
        if extra.get("startup_prelaunch_validation") != "tcp":
            continue
        if extra.get("startup_core_probe_url") or extra.get("startup_core_delay_ms") is not None:
            continue
        return True
    return False


def should_prime_tier3_pool_before_hot_reload(nodes: list[Any]) -> bool:
    return count_healthy_nodes(nodes) <= 0 or has_unverified_tier3_tcp_candidates(nodes)


def tier3_startup_background_expansion_decision(
    *,
    healthy_count: int,
    background_target: int,
    mark_collect_complete_when_full: bool,
    last_check_monotonic: Any,
    fallback_now: float,
) -> Tier3StartupBackgroundExpansionDecision:
    background_expansion_due = int(healthy_count) < background_target
    if background_expansion_due:
        return Tier3StartupBackgroundExpansionDecision(
            background_expansion_due=True,
            next_last_collect_tier3=0.0,
        )
    if mark_collect_complete_when_full:
        next_collect = fallback_now
        if isinstance(last_check_monotonic, dict):
            next_collect = last_check_monotonic.get("tier3", fallback_now)
        return Tier3StartupBackgroundExpansionDecision(
            background_expansion_due=False,
            next_last_collect_tier3=next_collect,
        )
    return Tier3StartupBackgroundExpansionDecision(
        background_expansion_due=False,
        next_last_collect_tier3=None,
    )


def tier3_alive_target_from_manager(manager: Any, *, default: int = 1) -> int:
    target = max(1, int(default or 1))
    getter = getattr(manager, "_tier3_min_alive_nodes", None)
    if callable(getter):
        try:
            target = max(target, int(getter()))
        except (TypeError, ValueError):
            pass
    return max(1, target)


def tier3_startup_readiness_error(
    *,
    required_for_start: bool,
    core_sync_marker: Any,
    current_healthy_count: int,
    required_healthy_count: int,
    failure_detail: str | Callable[[], str],
) -> str | None:
    if not required_for_start:
        return None

    def detail_suffix() -> str:
        detail = failure_detail() if callable(failure_detail) else failure_detail
        return f" ({detail})" if detail else ""

    if not core_sync_marker:
        return (
            "Fallback has no validated proxy nodes; startup remains fail-closed"
            + detail_suffix()
        )
    if current_healthy_count < required_healthy_count:
        return (
            f"Fallback has only {current_healthy_count}/{required_healthy_count} validated proxy nodes; "
            "startup remains fail-closed"
            + detail_suffix()
        )
    return None


def tier3_cache_requires_core_verification(nodes: list[Any], report: Any) -> bool:
    report = report if isinstance(report, dict) else {}
    validation = report.get("validation", {})
    validation = validation if isinstance(validation, dict) else {}
    return (
        validation.get("prelaunch_validation") == "tcp"
        or any(node_has_tcp_prelaunch_marker(node) for node in nodes)
    )


def tier3_cached_start_verification(nodes: list[Any], report: Any) -> Tier3CachedStartVerification:
    cached_nodes = list(nodes)
    if not tier3_cache_requires_core_verification(cached_nodes, report):
        return Tier3CachedStartVerification(
            nodes=cached_nodes,
            requires_core_verification=False,
            discarded_count=0,
        )
    verified = tier3_startup_core_verified_nodes(cached_nodes)
    return Tier3CachedStartVerification(
        nodes=verified,
        requires_core_verification=True,
        discarded_count=len(cached_nodes) - len(verified),
    )


def ensure_tier3_collection_report(manager: Any) -> dict[str, Any]:
    report = getattr(manager, "tier3_last_collection_report", _MISSING_REPORT)
    if not isinstance(report, dict):
        report = {}
        setattr(manager, "tier3_last_collection_report", report)
    return report


def build_tier3_startup_core_repair_report(
    *,
    tested_count: int,
    candidate_count: int,
    concurrency: int,
    working_count: int,
    current_node_count: int,
    retained_node_count: int,
    prune_to_successful: bool,
    target_verified_count: int,
    minimum_verified_count: int,
    selected: str,
    delay_ms: float,
    failed_tested_count: int = 0,
    retained_failed_count: int = 0,
    evicted_failed_count: int = 0,
) -> dict[str, Any]:
    return {
        "tested_count": tested_count,
        "candidate_count": candidate_count,
        "concurrency": concurrency,
        "working_count": working_count,
        "core_verified_count": working_count,
        "core_failed_count": max(0, int(failed_tested_count or 0)),
        "retained_failed_count": max(0, int(retained_failed_count or 0)),
        "evicted_failed_count": max(0, int(evicted_failed_count or 0)),
        "pruned_count": max(0, current_node_count - retained_node_count),
        "pruned_to_successful": bool(prune_to_successful),
        "target_verified_count": target_verified_count,
        "minimum_verified_count": minimum_verified_count,
        "stopped_after_target": bool(prune_to_successful and working_count >= target_verified_count),
        "selected": selected,
        "delay_ms": round(delay_ms, 2),
    }


def apply_tier3_core_repair_results(
    current_nodes: list[Any],
    successful_results: list[tuple[float, Any, str, str]],
    *,
    prune_to_successful: bool,
    slow_threshold_ms: int,
    identity_fn: Callable[[Any], str],
    now_iso_fn: Callable[[], str],
    failed_tested_identities: set[str] | None = None,
    retain_failed_until_threshold: bool = False,
    failure_threshold: int = 3,
) -> Tier3CoreRepairApplied:
    sorted_successful = sorted(successful_results, key=lambda item: item[0])
    preferred: dict[str, tuple[int, float, str, str]] = {}
    for index, (delay, node, proxy_name, probe_url) in enumerate(sorted_successful):
        preferred[identity_fn(node)] = (index, delay, proxy_name, probe_url)

    current_nodes = list(current_nodes)
    current_by_identity = {identity_fn(node): node for node in current_nodes}
    verified_nodes: list[Any] = []
    seen_verified: set[str] = set()
    for _delay, node, _proxy_name, _probe_url in sorted_successful:
        identity = identity_fn(node)
        current = current_by_identity.get(identity)
        if current is None or identity in seen_verified:
            continue
        verified_nodes.append(current)
        seen_verified.add(identity)

    for node in current_nodes:
        identity = identity_fn(node)
        match = preferred.get(identity)
        if not match:
            continue
        _rank, delay, _proxy_name, probe_url = match
        node.latency_ms = round(delay, 2)
        node.status = NodeStatus.ALIVE if delay <= slow_threshold_ms else NodeStatus.SLOW
        node.suspicious_reason = None
        node.last_checked_at = now_iso_fn()
        node.extra["startup_core_probe_url"] = probe_url
        node.extra["startup_core_delay_ms"] = round(delay, 2)

    def sort_key(node: Any) -> tuple[int, float]:
        identity = identity_fn(node)
        match = preferred.get(identity)
        if match:
            return (0, match[1])
        return (1, float(getattr(node, "latency_ms", None) or 999999))

    failed_tested = set(failed_tested_identities or set())
    threshold = max(1, int(failure_threshold or 1))
    failed_at = now_iso_fn()
    retained_failed_nodes: list[Any] = []
    evicted_failed_nodes: list[Any] = []
    failed_by_identity: dict[str, Any] = {}
    if retain_failed_until_threshold:
        for node in current_nodes:
            identity = identity_fn(node)
            if identity in seen_verified or identity not in failed_tested:
                continue
            failure_count = int(getattr(node, "extra", {}).get("tier3_consecutive_failures", 0) or 0) + 1
            failed = node.clone() if hasattr(node, "clone") else node
            failed.status = NodeStatus.SUSPICIOUS
            failed.latency_ms = None
            failed.suspicious_reason = "mihomo_validation_failed: core export verification failed"
            failed.last_checked_at = failed_at
            failed.extra["tier3_consecutive_failures"] = failure_count
            failed.extra["tier3_last_failure_reason"] = failed.suspicious_reason
            failed.extra["tier3_last_failure_at"] = failed_at
            failed.extra["tier3_retained_after_failure"] = True
            failed.extra.pop("tier3_retained_unchecked", None)
            failed.extra.pop("startup_core_probe_url", None)
            failed.extra.pop("startup_core_delay_ms", None)
            if failure_count >= threshold:
                evicted_failed_nodes.append(failed)
                continue
            retained_failed_nodes.append(failed)
            failed_by_identity[identity] = failed

    pool_nodes: list[Any] = []
    evicted_identities = {identity_fn(node) for node in evicted_failed_nodes}
    for node in current_nodes:
        identity = identity_fn(node)
        if identity in evicted_identities:
            continue
        pool_nodes.append(failed_by_identity.get(identity, node))

    retained_nodes = (
        [*verified_nodes, *retained_failed_nodes]
        if prune_to_successful and retain_failed_until_threshold
        else verified_nodes
        if prune_to_successful
        else pool_nodes
    )
    sorted_nodes = sorted(retained_nodes, key=sort_key)
    if sorted_successful:
        selected_delay, _selected_node, selected_proxy_name, _selected_probe_url = sorted_successful[0]
    else:
        selected_delay = 0.0
        selected_proxy_name = ""
    return Tier3CoreRepairApplied(
        nodes=sorted_nodes,
        pool_nodes=pool_nodes,
        observed_failed_nodes=[*retained_failed_nodes, *evicted_failed_nodes],
        current_node_count=len(current_nodes),
        retained_node_count=len(sorted_nodes) if prune_to_successful else len(pool_nodes),
        working_count=len(sorted_successful),
        failed_tested_count=len(failed_tested - seen_verified),
        retained_failed_count=len(retained_failed_nodes),
        evicted_failed_count=len(evicted_failed_nodes),
        selected_proxy_name=selected_proxy_name,
        selected_delay_ms=selected_delay,
    )


def tier3_core_probe_plan(
    candidates: list[Any],
    *,
    configured_concurrency: Any,
) -> Tier3CoreProbePlan:
    if not candidates:
        return Tier3CoreProbePlan(concurrency=0, batches=[])
    try:
        requested_concurrency = max(1, int(configured_concurrency))
    except (TypeError, ValueError):
        requested_concurrency = 1
    concurrency = min(len(candidates), requested_concurrency)
    return Tier3CoreProbePlan(
        concurrency=concurrency,
        batches=[
            candidates[index : index + concurrency]
            for index in range(0, len(candidates), concurrency)
        ],
    )


def should_stop_tier3_core_probe(
    *,
    prune_to_successful: bool,
    successful_count: int,
    target_verified: int,
) -> bool:
    return bool(prune_to_successful and successful_count >= target_verified)


def build_tier3_startup_core_verification_failed_report(
    *,
    reason: str,
    rejected_count: int,
) -> dict[str, Any]:
    return {
        "reason": str(reason or "Fallback egress verification failed")[:500],
        "rejected_count": rejected_count,
        "will_retry_collection": True,
    }


def build_tier3_core_reload_rollback_report(
    *,
    retained_count: int,
) -> dict[str, Any]:
    return {
        "core_reload_rollback": True,
        "core_reload_rollback_retained_count": retained_count,
    }


def should_restore_tier3_pool_after_failed_reload(previous_nodes: list[Any]) -> bool:
    return bool(previous_nodes) and count_healthy_nodes(previous_nodes) > 0


def tier3_reload_rollback_source_nodes(
    *,
    last_synced_nodes: list[Any],
    current_nodes: list[Any],
) -> list[Any]:
    return last_synced_nodes if last_synced_nodes else current_nodes


def build_core_reload_context(
    *,
    runtime_config_path: Path,
    previous_config_text: str | None,
    controller: dict[str, Any],
    current_tier3_nodes: list[Any],
    last_synced_tier3_nodes: list[Any],
    clone_nodes: Callable[[list[Any]], list[Any]],
    provider_path: Path | None = None,
    previous_provider_bytes: bytes = b"",
    previous_provider_exists: bool = False,
    previous_provider_projection: Any = None,
) -> CoreReloadContext:
    current_nodes = list(current_tier3_nodes)
    rollback_source = tier3_reload_rollback_source_nodes(
        last_synced_nodes=last_synced_tier3_nodes,
        current_nodes=current_nodes,
    )
    return CoreReloadContext(
        runtime_config_path=Path(runtime_config_path),
        previous_config_text=previous_config_text,
        controller=dict(controller),
        current_tier3_nodes=current_nodes,
        rollback_tier3_nodes=clone_nodes(rollback_source),
        provider_path=(Path(provider_path) if provider_path is not None else None),
        previous_provider_bytes=bytes(previous_provider_bytes),
        previous_provider_exists=bool(previous_provider_exists),
        previous_provider_projection=previous_provider_projection,
    )


def build_keep_existing_core_reload_failure_result(
    *,
    reason: str,
    restored_config: bool,
    restore_reload: dict[str, Any],
    core_status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "reloaded": False,
        "reason": reason,
        "kept_existing_core": True,
        "restored_config": restored_config,
        "restore_reload": restore_reload,
        "core": core_status,
    }


def build_core_reload_unavailable_result(
    *,
    reason: str,
    core_status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "reloaded": False,
        "reason": reason,
        "core": core_status,
    }


def build_tier3_core_reload_failure_result(
    *,
    reason: str,
    stop_sensitive: bool,
    reload_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "reloaded": False,
        "reason": reason,
        "stop_sensitive": bool(stop_sensitive),
    }
    if reload_result is not None:
        result["reload"] = reload_result
    return result


def tier3_is_primary_export(*, primary_configured: bool, tier3_required_for_start: bool, core_sync_marker: tuple[Any, ...]) -> bool:
    return (not primary_configured or tier3_required_for_start) and bool(core_sync_marker)


def should_attempt_deferred_tier3_core_start(
    *,
    running: bool,
    marker: Any,
    last_marker: Any,
) -> bool:
    # ``last_marker`` describes what a previous Core loaded. Once Core is
    # stopped it must not suppress a recovered pool with the same identities.
    # DesktopRuntime applies its separate failed-marker cooldown before start.
    del last_marker
    return bool(running and marker)


def should_reload_tier3_core_config_for_marker(
    *,
    marker: Any,
    last_marker: Any,
) -> bool:
    return marker is not None and marker != last_marker


def tier3_core_sync_action_after_pool_update(
    *,
    stop_requested: bool,
    runtime_running: bool,
    core_running: bool,
    marker: Any,
    last_marker: Any,
) -> Tier3CoreSyncAction:
    if stop_requested:
        return Tier3CoreSyncAction.NONE
    if not core_running:
        if should_attempt_deferred_tier3_core_start(
            running=runtime_running,
            marker=marker,
            last_marker=last_marker,
        ):
            return Tier3CoreSyncAction.DEFERRED_START
        return Tier3CoreSyncAction.NONE
    if should_reload_tier3_core_config_for_marker(
        marker=marker,
        last_marker=last_marker,
    ):
        return Tier3CoreSyncAction.RELOAD_RUNNING_CORE
    return Tier3CoreSyncAction.NONE


def tier3_core_sync_failure_is_recent(
    *,
    marker: Any,
    last_failed_marker: Any,
    last_failed_at: float,
    now: float,
    retry_interval: float,
) -> bool:
    if marker is None or marker != last_failed_marker:
        return False
    return (now - last_failed_at) < retry_interval


def tier3_core_sync_retry_interval(config: dict[str, Any]) -> float:
    tiers = config.get("tiers", {}) if isinstance(config.get("tiers"), dict) else {}
    tier3 = tiers.get("tier3", {}) if isinstance(tiers.get("tier3"), dict) else {}
    raw_value = tier3.get("core_sync_retry_interval", tier3.get("min_collect_interval", 120))
    try:
        return max(1.0, float(raw_value))
    except (TypeError, ValueError):
        return 120.0


def reload_core_config_succeeded(result: Any) -> bool:
    if not isinstance(result, dict):
        return result is None
    if result.get("kept_existing_core"):
        return False
    if result.get("reloaded"):
        return True
    core = result.get("core")
    return isinstance(core, dict) and bool(core.get("running"))


def stop_sensitive_core_reload_failure_action(
    result: Any,
    *,
    stop_requested: bool,
) -> CoreReloadFailureAction:
    if isinstance(result, dict) and result.get("reloaded"):
        return CoreReloadFailureAction(unavailable_reason=None, keep_existing_reason=None)
    if isinstance(result, dict):
        if result.get("stop_sensitive") and stop_requested:
            return CoreReloadFailureAction(unavailable_reason="runtime stopping", keep_existing_reason=None)
        reason = result.get("reason")
    else:
        reason = result
    return CoreReloadFailureAction(
        unavailable_reason=None,
        keep_existing_reason=str(reason),
    )


def tier3_enabled_without_primary(*, tier3_enabled: bool, primary_configured: bool) -> bool:
    return bool(tier3_enabled and not primary_configured)


def tier3_runtime_export_limit(
    *,
    configured_limit: int,
    startup_probe_limit: int,
    tier3_required_for_start: bool,
    primary_configured: bool,
    uses_tcp_prelaunch_validation: bool,
) -> int:
    if tier3_required_for_start or not primary_configured or uses_tcp_prelaunch_validation:
        return max(configured_limit, startup_probe_limit)
    return configured_limit


def tier3_core_repair_uses_startup_target(
    *,
    tier3_required_for_start: bool,
    uses_tcp_prelaunch_validation: bool,
) -> bool:
    return bool(tier3_required_for_start or uses_tcp_prelaunch_validation)


def tier3_core_repair_targets(
    *,
    prune_to_successful: bool,
    target_alive: int | None,
    minimum_alive: int | None,
    default_target_alive: int,
) -> Tier3CoreRepairTargets:
    if not prune_to_successful:
        return Tier3CoreRepairTargets(target_verified=1, minimum_verified=1)
    if target_alive is None:
        target_alive = default_target_alive
    target_verified = max(1, int(target_alive))
    if minimum_alive is None:
        minimum_alive = target_verified
    minimum_verified = max(1, int(minimum_alive))
    return Tier3CoreRepairTargets(
        target_verified=max(target_verified, minimum_verified),
        minimum_verified=minimum_verified,
    )


def tier3_export_uses_tcp_prelaunch_validation(*, report: Any, nodes: list[Any]) -> bool:
    report = report if isinstance(report, dict) else {}
    if report.get("startup_mode") is False:
        return False
    validation = report.get("validation", {})
    if isinstance(validation, dict) and validation.get("prelaunch_validation") == "tcp":
        return True
    for node in nodes:
        extra = getattr(node, "extra", {})
        if isinstance(extra, dict) and extra.get("startup_prelaunch_validation") == "tcp":
            return True
    return False


def tier3_startup_failure_detail(
    *,
    primary_startup_error: str | None,
    report: Any,
    nodes: list[Any],
    last_startup_error: str | None,
) -> str:
    report = report if isinstance(report, dict) else {}
    pieces: list[str] = []
    if primary_startup_error:
        pieces.append("primary=" + _safe_startup_error_summary(primary_startup_error))
    if report:
        source_count = _safe_count(report.get("source_count", 0))
        fetched_sources = _safe_count(report.get("fetched_sources", 0))
        failed_sources_count = _safe_count(report.get("failed_sources", 0))
        pending_sources = max(0, source_count - fetched_sources - failed_sources_count)
        pieces.append(
            "collection "
            f"sources={source_count}, "
            f"fetched={fetched_sources}, "
            f"failed={failed_sources_count}, "
            f"parsed_nodes={report.get('node_count', 0)}"
        )
        if pending_sources:
            pieces.append(f"pending_sources={pending_sources}")
        source_phase = _safe_report_code(
            report.get("source_supply_phase"),
            _TIER3_SOURCE_SUPPLY_PHASE_CODES,
            fallback="unknown",
        )
        raw_blocked_reason = report.get("source_retry_blocked_reason")
        source_reason = _safe_source_supply_reason_code(
            raw_blocked_reason,
            failed_count=failed_sources_count,
        )
        source_diagnosis = _safe_report_code(
            report.get("source_stream_sampling_diagnosis"),
            _TIER3_SOURCE_SUPPLY_DIAGNOSIS_CODES,
            fallback="none",
        )
        blocked_count = _safe_count(report.get("source_supply_blocked_count"))
        if source_reason == "blocked_no_route" and blocked_count <= 0:
            blocked_count = max(1, failed_sources_count)
        failure_class = "none"
        terminal_reason = _safe_report_code(
            report.get("source_supply_terminal_reason"),
            _TIER3_SOURCE_SUPPLY_TERMINAL_REASON_CODES,
            fallback="none",
        )
        if terminal_reason in {"rule_blocked", "no_proxy", "transport_failed"}:
            failure_class = terminal_reason
        elif source_reason in {
            "prerequisite_missing",
            "rule_blocked",
            "runtime_source_retry_disabled",
            "pre_gate_admitted_source_assist_blocked",
        }:
            failure_class = "rule_blocked"
        elif source_reason in {
            "blocked_no_route",
            "runtime_proxy_unavailable_after_recovery",
        } or source_phase in {"blocked_pre_core", "blocked_core"}:
            failure_class = "no_proxy"
        elif failed_sources_count or source_reason == "runtime_egress_recovery_failed":
            failure_class = "transport_failed"
        classified_failure_count = max(blocked_count, failed_sources_count)
        rule_blocked_count = (
            classified_failure_count if failure_class == "rule_blocked" else 0
        )
        no_proxy_count = classified_failure_count if failure_class == "no_proxy" else 0
        transport_failed_count = (
            classified_failure_count if failure_class == "transport_failed" else 0
        )
        if (
            failed_sources_count
            or blocked_count
            or source_phase != "unknown"
            or source_reason != "none"
            or source_diagnosis != "none"
        ):
            pieces.append(
                "source_supply "
                f"phase={source_phase}, "
                f"reason={source_reason}, "
                f"diagnosis={source_diagnosis}, "
                f"terminal={terminal_reason}, "
                f"failure_class={failure_class}, "
                f"blocked={blocked_count}, "
                f"failed={failed_sources_count}"
            )
            pieces.append(
                "source_failures "
                f"rule_blocked={rule_blocked_count}, "
                f"no_proxy={no_proxy_count}, "
                f"transport_failed={transport_failed_count}"
            )
        cache_local_healthy = _safe_count(report.get("cache_local_healthy_count"))
        cache_local_required = _safe_count(report.get("cache_local_required_count"))
        if cache_local_healthy or cache_local_required:
            pieces.append(
                f"cache_local healthy={cache_local_healthy}/{cache_local_required}"
            )
        validation = report.get("validation", {})
        if isinstance(validation, dict) and validation:
            pieces.append(
                "validation "
                f"tested={validation.get('tested_count', 0)}/"
                f"{validation.get('candidate_count', 0)}, "
                f"limit={validation.get('limit', 0)}"
            )
        source_stream_seen = _safe_count(report.get("source_stream_seen_candidate_count"))
        source_stream_tested = _safe_count(report.get("source_stream_tested_count"))
        source_stream_passed = _safe_count(report.get("source_stream_full_validation_passed_count"))
        source_stream_admitted = _safe_count(report.get("source_stream_committed_alive_count"))
        source_stream_facility = _safe_count(report.get("source_stream_validator_facility_failure_count"))
        source_stream_diagnosis = source_diagnosis
        has_source_stream_diagnosis = source_stream_diagnosis != "none"
        if any(
            value > 0
            for value in (
                source_stream_seen,
                source_stream_tested,
                source_stream_passed,
                source_stream_admitted,
                source_stream_facility,
            )
        ) or has_source_stream_diagnosis:
            detail = (
                "streaming "
                f"seen={source_stream_seen}, "
                f"tested={source_stream_tested}, "
                f"passed={source_stream_passed}, "
                f"admitted={source_stream_admitted}"
            )
            if source_stream_facility:
                detail += f", validator_facility_failures={source_stream_facility}"
            if has_source_stream_diagnosis:
                detail += f", diagnosis={source_stream_diagnosis}"
            pieces.append(detail)
    if nodes:
        healthy = count_healthy_nodes(nodes)
        pieces.append(f"validation healthy={healthy}/{len(nodes)}")
        reasons: list[str] = []
        for node in nodes:
            reason = str(getattr(node, "suspicious_reason", "") or "").strip()
            reason = _safe_startup_error_summary(reason)
            if reason and reason not in reasons:
                reasons.append(reason)
            if len(reasons) >= 2:
                break
        if reasons:
            pieces.append("sample_validation_errors=" + " | ".join(reasons))
    if last_startup_error:
        pieces.append("startup_error=" + _safe_startup_error_summary(last_startup_error))
    return "; ".join(pieces)


@dataclass(frozen=True, slots=True)
class CoreExportSemanticDigest:
    """Secret-safe identity of the exact rendered Mihomo configuration."""

    digest: str
    proxy_count: int
    proxy_names: tuple[str, ...]
    group_membership: tuple[tuple[str, tuple[str, ...]], ...]
    listener_names: tuple[str, ...]

    @classmethod
    def from_rendered_config(
        cls,
        rendered_config: dict[str, Any],
        *,
        overlay_membership: tuple[str, ...] = (),
    ) -> "CoreExportSemanticDigest":
        config = rendered_config if isinstance(rendered_config, dict) else {}
        proxies = config.get("proxies")
        proxy_entries = proxies if isinstance(proxies, list) else []
        proxy_names = tuple(
            str(item.get("name") or "")
            for item in proxy_entries
            if isinstance(item, dict) and str(item.get("name") or "")
        )
        groups = config.get("proxy-groups")
        group_entries = groups if isinstance(groups, list) else []
        group_membership = tuple(
            (
                str(item.get("name") or ""),
                tuple(str(value) for value in item.get("proxies", [])),
            )
            for item in group_entries
            if isinstance(item, dict)
            and str(item.get("name") or "")
            and isinstance(item.get("proxies"), list)
        )
        listeners = config.get("listeners")
        listener_entries = listeners if isinstance(listeners, list) else []
        listener_names = tuple(
            str(item.get("name") or "")
            for item in listener_entries
            if isinstance(item, dict) and str(item.get("name") or "")
        )
        canonical_payload = {
            "rendered_config": config,
            "overlay_membership": list(overlay_membership),
        }
        encoded = json.dumps(
            canonical_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return cls(
            digest=hashlib.sha256(encoded).hexdigest(),
            proxy_count=len(proxy_entries),
            proxy_names=proxy_names,
            group_membership=group_membership,
            listener_names=listener_names,
        )


@dataclass(frozen=True, slots=True)
class Tier3RuntimeQualificationSnapshot:
    config_generation: int
    config_digest: str
    admitted_count: int
    exported_count: int
    qualified_count: int
    qualified_identities: tuple[str, ...]
    final_exit_ok: bool
    traffic_capture_active: bool
    observed_at_monotonic: float
    oldest_qualification_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class Tier3CoreMembershipSnapshot:
    """Durable Tier3B membership independently of transient health state."""

    pool_revision: int
    identities: tuple[str, ...]
    proxy_signatures: tuple[tuple[Any, ...], ...]


def _rendered_proxy_signature(
    node: Any,
    *,
    seen_names: set[str],
) -> tuple[Any, ...]:
    proxy = node_to_clash_proxy(node, seen_names=seen_names)
    if not isinstance(proxy, dict):
        return ()
    encoded = json.dumps(
        proxy,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return (
        str(proxy.get("name") or ""),
        hashlib.sha256(encoded).hexdigest(),
    )


def tier3_export_node_signature(node: Any) -> tuple[Any, ...]:
    return _rendered_proxy_signature(node, seen_names=set())


def tier3_export_signature(nodes: list[Any]) -> tuple[Any, ...]:
    seen_names: set[str] = set()
    signatures: list[tuple[Any, ...]] = []
    for node in nodes:
        signature = _rendered_proxy_signature(node, seen_names=seen_names)
        if signature:
            signatures.append(signature)
    return tuple(signatures)


def tier3_probe_urls_for_node(node: Any, default_urls: list[str]) -> list[str]:
    urls: list[str] = []
    extra = getattr(node, "extra", {})
    extra = extra if isinstance(extra, dict) else {}
    validation_url = str(extra.get("validation_probe_url") or "").strip()
    if validation_url:
        urls.append(validation_url)
    validation_urls = extra.get("validation_probe_urls")
    if isinstance(validation_urls, list):
        for item in validation_urls:
            url = str(item or "").strip()
            if url and url not in urls:
                urls.append(url)
    for url in default_urls:
        if url not in urls:
            urls.append(url)
    return urls


def should_keep_existing_core_for_empty_tier3_export(
    *,
    tier3_enabled: bool,
    primary_configured: bool,
    tier3_required_for_start: bool,
    primary_healthy_count: int,
) -> bool:
    if not tier3_enabled:
        return False
    if not primary_configured:
        return True
    if tier3_required_for_start:
        return True
    return primary_healthy_count <= 0


def should_keep_existing_core_for_empty_tier3_marker(
    *,
    marker: Any,
    keep_existing_for_empty_export: bool,
) -> bool:
    return marker == () and bool(keep_existing_for_empty_export)
