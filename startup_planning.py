from __future__ import annotations

from typing import Any

from config_utils import config_entry_enabled, coerce_bool
from tier3_defaults import DEFAULT_TIER3_VALIDATION_EMPTY_RETRY_LIMIT
from tier3_threshold_policy import resolve_tier3_thresholds


def safe_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    raw_value = value
    if raw_value is None:
        raw_value = default
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped or stripped.lower() in {"inherit", "null", "none"}:
            raw_value = default
        else:
            raw_value = stripped
    try:
        return max(minimum, int(raw_value))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def summary_enabled_node_count(tier: dict[str, Any]) -> int:
    if "enabled_total" in tier:
        return safe_count(tier.get("enabled_total"))
    nodes = tier.get("nodes")
    if isinstance(nodes, list):
        return sum(1 for entry in nodes if config_entry_enabled(entry))
    return safe_count(tier.get("total"))


def summary_enabled_subscription_count(tier: dict[str, Any]) -> int:
    if "enabled_subscription_count" in tier:
        return safe_count(tier.get("enabled_subscription_count"))
    subscriptions = tier.get("subscriptions")
    if isinstance(subscriptions, list) and subscriptions:
        return sum(1 for entry in subscriptions if config_entry_enabled(entry))
    return safe_count(tier.get("subscription_count"))


def tier3_user_config_count(tier3: dict[str, Any]) -> int:
    return summary_enabled_node_count(tier3) + summary_enabled_subscription_count(tier3)


def summary_tier3b_source_count(tier3: dict[str, Any], sources: dict[str, Any]) -> int:
    explicit_count = safe_count(tier3.get("tier3b_source_count")) if isinstance(tier3, dict) else 0
    source_count = (
        safe_count(sources.get("github_repos") if isinstance(sources, dict) else 0)
        + safe_count(sources.get("telegram_channels") if isinstance(sources, dict) else 0)
        + safe_count(sources.get("subscription_urls") if isinstance(sources, dict) else 0)
        + safe_count(sources.get("local_files") if isinstance(sources, dict) else 0)
    )
    if explicit_count > 0 or source_count > 0:
        return max(explicit_count, source_count)
    if isinstance(tier3, dict) and "bootstrap_source_count" in tier3:
        return safe_count(tier3.get("bootstrap_source_count"))
    return 0


def configured_start_source_count(summary: dict[str, Any]) -> int:
    tier1 = summary.get("tier1", {}) if isinstance(summary, dict) else {}
    tier2 = summary.get("tier2", {}) if isinstance(summary, dict) else {}
    tier3 = summary.get("tier3", {}) if isinstance(summary, dict) else {}
    exit_tier = summary.get("exit", {}) if isinstance(summary, dict) else {}
    sources = summary.get("sources", {}) if isinstance(summary, dict) else {}

    tier3_enabled = coerce_bool(tier3.get("enabled"), False) if isinstance(tier3, dict) else False
    source_count = 0
    if tier3_enabled:
        source_count = (
            (tier3_user_config_count(tier3) if isinstance(tier3, dict) else 0)
            + (
                summary_tier3b_source_count(tier3, sources)
                if isinstance(tier3, dict) and coerce_bool(tier3.get("bootstrap_enabled", True), True)
                else 0
            )
        )
    return (
        (summary_enabled_node_count(tier1) if isinstance(tier1, dict) else 0)
        + (summary_enabled_node_count(tier2) if isinstance(tier2, dict) else 0)
        + (summary_enabled_subscription_count(tier2) if isinstance(tier2, dict) else 0)
        + (summary_enabled_node_count(exit_tier) if isinstance(exit_tier, dict) else 0)
        + (summary_enabled_subscription_count(exit_tier) if isinstance(exit_tier, dict) else 0)
        + source_count
    )


def tier3_recent_source_collection_status(summary: dict[str, Any]) -> dict[str, int]:
    tier3 = summary.get("tier3", {}) if isinstance(summary, dict) else {}
    collection = tier3.get("collection", {}) if isinstance(tier3, dict) else {}
    if not isinstance(collection, dict):
        collection = {}
    source_count = max(
        safe_count(collection.get("configured_source_count")),
        safe_count(collection.get("source_count", collection.get("source_stream_source_count"))),
    )
    fetched = safe_count(
        collection.get("source_fetch_ok", collection.get("source_stream_source_fetch_ok"))
    )
    failed = safe_count(
        collection.get("source_fetch_failed", collection.get("source_stream_source_fetch_failed"))
    )
    pending_default = max(0, source_count - fetched - failed)
    pending = safe_count(
        collection.get("source_fetch_pending", collection.get("source_stream_source_fetch_pending", pending_default))
    )
    pending = max(pending, pending_default)
    retry_due = safe_count(
        collection.get("source_retry_due_count", collection.get("retryable_failed_sources", 0))
    )
    if retry_due <= 0 and collection.get("source_retry_due"):
        retry_due = max(1, failed)
    if source_count <= 0 and (fetched > 0 or failed > 0 or pending > 0):
        source_count = fetched + failed + pending
    return {
        "source_count": source_count,
        "fetched": fetched,
        "failed": failed,
        "pending": pending,
        "retry_due": retry_due,
    }


def recent_tier3_sources_available(summary: dict[str, Any]) -> bool:
    status = tier3_recent_source_collection_status(summary)
    return status["source_count"] > 0 or status["pending"] > 0 or status["retry_due"] > 0


def bastion_disabled_empty_start_prompt_required(summary: dict[str, Any]) -> bool:
    if not isinstance(summary, dict):
        return False
    tier1 = summary.get("tier1", {}) if isinstance(summary.get("tier1"), dict) else {}
    tier2 = summary.get("tier2", {}) if isinstance(summary.get("tier2"), dict) else {}
    tier3 = summary.get("tier3", {}) if isinstance(summary.get("tier3"), dict) else {}
    exit_tier = summary.get("exit", {}) if isinstance(summary.get("exit"), dict) else {}
    sources = summary.get("sources", {}) if isinstance(summary.get("sources"), dict) else {}

    primary_count = (
        summary_enabled_node_count(tier1)
        + summary_enabled_node_count(tier2)
        + summary_enabled_subscription_count(tier2)
        + summary_enabled_node_count(exit_tier)
        + summary_enabled_subscription_count(exit_tier)
    )
    if primary_count > 0 or coerce_bool(tier3.get("enabled"), False):
        return False

    user_tier3_count = tier3_user_config_count(tier3)
    tier3b_count = (
        summary_tier3b_source_count(tier3, sources)
        if coerce_bool(tier3.get("bootstrap_enabled", True), True)
        else 0
    )
    return user_tier3_count + tier3b_count > 0


def bastion_empty_pool_prompt_required(summary: dict[str, Any], *, prompt_available: bool = True) -> bool:
    if not prompt_available or not isinstance(summary, dict):
        return False
    tier1 = summary.get("tier1", {}) if isinstance(summary.get("tier1"), dict) else {}
    tier2 = summary.get("tier2", {}) if isinstance(summary.get("tier2"), dict) else {}
    tier3 = summary.get("tier3", {}) if isinstance(summary.get("tier3"), dict) else {}
    sources = summary.get("sources", {}) if isinstance(summary.get("sources"), dict) else {}

    primary_count = (
        summary_enabled_node_count(tier1)
        + summary_enabled_node_count(tier2)
        + summary_enabled_subscription_count(tier2)
    )
    user_tier3_count = tier3_user_config_count(tier3)
    runtime = tier3.get("runtime", {}) if isinstance(tier3.get("runtime"), dict) else {}
    runtime_healthy = safe_count(runtime.get("healthy"))
    tier3b_count = (
        summary_tier3b_source_count(tier3, sources)
        if coerce_bool(tier3.get("bootstrap_enabled", True), True)
        else 0
    )
    return (
        primary_count == 0
        and coerce_bool(tier3.get("enabled"), False)
        and user_tier3_count == 0
        and runtime_healthy == 0
        and tier3b_count > 0
    )


def ceil_div(value: int, divisor: int) -> int:
    divisor = max(1, int(divisor))
    value = max(0, int(value))
    return (value + divisor - 1) // divisor


def nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key) if isinstance(payload, dict) else {}
    return value if isinstance(value, dict) else {}


def tier3_config_from_runtime(config: dict[str, Any]) -> dict[str, Any]:
    tiers = nested_dict(config, "tiers")
    return nested_dict(tiers, "tier3")


def bastion_startup_plan(config: dict[str, Any]) -> dict[str, int]:
    tier3 = tier3_config_from_runtime(config)
    testing = nested_dict(config, "testing")
    helper = nested_dict(testing, "mihomo_helper")

    thresholds = resolve_tier3_thresholds(tier3)
    background_target = thresholds.effective_target_alive
    startup_gate = thresholds.min_alive_nodes

    retry_limit = max(
        coerce_int(
            tier3.get("validation_empty_retry_limit"),
            DEFAULT_TIER3_VALIDATION_EMPTY_RETRY_LIMIT,
            minimum=1,
        ),
        background_target * 4,
    )
    retry_limit = max(retry_limit, startup_gate)
    fast_probe_limit = retry_limit
    request_timeout = coerce_int(testing.get("request_timeout"), 15, minimum=1)
    retries = coerce_int(testing.get("retries"), 0, minimum=0)
    tcp_timeout = coerce_int(testing.get("tcp_timeout"), 5, minimum=1)
    probe_timeout = coerce_int(helper.get("probe_timeout"), 5, minimum=1)
    helper_timeout = coerce_int(helper.get("startup_timeout"), 5, minimum=1)
    concurrency = coerce_int(
        helper.get("concurrency"),
        coerce_int(testing.get("concurrency"), 3, minimum=1),
        minimum=1,
    )

    collection_seconds = request_timeout * (retries + 1) + 2
    tcp_probe_seconds = ceil_div(fast_probe_limit, concurrency) * tcp_timeout if fast_probe_limit else 0
    validation_seconds = ceil_div(retry_limit, concurrency) * (tcp_timeout + helper_timeout + probe_timeout)
    estimate_seconds = max(30, collection_seconds + tcp_probe_seconds + validation_seconds)
    return {
        "target": startup_gate,
        "gate": startup_gate,
        "background_target": background_target,
        "candidate_budget": retry_limit,
        "estimate_seconds": estimate_seconds,
        "validation_concurrency": concurrency,
    }


def format_startup_duration(seconds: Any) -> str:
    total = coerce_int(seconds, 0, minimum=0)
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        if secs:
            return f"{minutes} 分 {secs} 秒"
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} 小时 {minutes} 分"
    return f"{hours} 小时"
