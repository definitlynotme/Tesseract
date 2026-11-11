"""Report/configuration gates; not a substitute for packet-path verification."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CLOSED_RESOURCE_COUNTS = (
    "candidate_scheduler_active_ticket_count",
    "candidate_scheduler_reserved_handoff_count",
    "candidate_scheduler_tcp_handoff_lease_count",
    "candidate_scheduler_parked_deep_debt",
    "candidate_scheduler_reward_exact_outstanding_count",
    "candidate_scheduler_reward_service_outstanding_count",
    "candidate_scheduler_reward_discovery_outstanding_count",
    "source_stream_tcp_prestart_reserved_count",
    "source_stream_tcp_prestart_claimed_count",
    "source_stream_tcp_started_active_count",
    "source_stream_admission_commit_pending_count",
    "source_stream_validation_submit_pending_count",
    "source_stream_validation_feedback_pending_commit_count",
    "source_stream_process_ingress_depth",
    "source_stream_process_fast_ingress_tcp_pending",
    "source_stream_process_fast_ingress_actor_pending",
    "source_stream_process_fast_ingress_actor_event_pending",
    "source_stream_process_active_count",
    "source_stream_process_active_helper_count",
    "source_supply_supervisor_active_leases",
)


def evaluate_tier3_audit(
    report: Mapping[str, Any],
    isolation: Mapping[str, Any],
    *,
    target: int = 25,
    clean_exit: bool,
    source_unchanged: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    required_isolation = (
        "physical_interface_bound", "negative_tcp_control_rejected",
        "owned_dns_control_passed", "capture_disabled", "external_network_unchanged",
        "outbound_socket_guard_installed", "auxiliary_proxy_disabled",
        "non_owned_loopback_blocked",
        "all_owned_processes_guarded",
    )
    failures.extend(f"isolation_unproven:{key}" for key in required_isolation
                    if isolation.get(key) is not True)
    if not clean_exit:
        failures.append("unclean_exit")
    if not source_unchanged:
        failures.append("build_inputs_changed")
    if (report.get("source_stream_runtime_identity_match") is not True
            or report.get("source_stream_performance_sample_valid") is not True):
        failures.append("build_identity_unproven")
    # Maintenance/refill may replace the hot report after the cold expansion.
    # Never borrow its already-healthy inventory as cold-start performance.
    performance = dict(report)
    summaries = report.get("completed_session_summaries") or []
    generation = report.get("runtime_generation")
    summaries = [item for item in summaries if isinstance(item, Mapping)
                 and item.get("runtime_generation") == generation]
    if summaries:
        first = min(summaries, key=lambda item: int(item.get("refresh_epoch", 0)))
        if first.get("initial_healthy") != 0:
            failures.append("initial_empty_pool_session_unproven")
        for report_key, summary_key in (
            ("source_stream_time_to_pool_revision_3_ms", "time_to_3_ms"),
            ("source_stream_time_to_pool_target_ms", "time_to_target_ms"),
            ("source_stream_alive_at_20s", "alive_at_20s"),
            ("source_stream_committed_alive_count", "final_healthy"),
        ):
            performance[report_key] = first.get(summary_key)
    if report.get("running") is not False or report.get("source_stream_shutdown_confirmed") is not True:
        failures.append("closed_snapshot_unconfirmed")
    for key in CLOSED_RESOURCE_COUNTS:
        value = report.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value != 0:
            failures.append(f"active_resource:{key}")
    if report.get("source_supply_worker_alive") is not False:
        failures.append("source_worker_exit_unconfirmed")
    if report.get("source_stream_process_hard_stall") is not False:
        failures.append("validation_liveness_unproven")
    starts = [report.get(key) for key in (
        "source_stream_tcp_start_acked_count",
        "source_stream_tcp_prestart_started_total",
        "candidate_scheduler_selected_total",
        "source_stream_candidate_scan_total_granted",
    )]
    if (any(not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in starts) or len(set(starts)) != 1):
        failures.append("tcp_start_conservation_unproven")
    timings = (
        ("source_stream_time_to_pool_revision_3_ms", 13000),
        ("source_stream_time_to_pool_target_ms", 61000),
    )
    for key, ceiling in timings:
        value = performance.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= ceiling:
            failures.append(f"milestone_failed:{key}")
    if int(performance.get("source_stream_alive_at_20s") or 0) < 15:
        failures.append("n20_below_15")
    if int(performance.get("source_stream_committed_alive_count") or 0) < target:
        failures.append("target_not_reached")
    passed = report.get("source_stream_full_validation_passed_count")
    committed = report.get("source_stream_admission_commit_committed_count")
    skipped = report.get("source_stream_admission_commit_skipped_count")
    if any(not isinstance(value, int) or isinstance(value, bool)
           for value in (passed, committed, skipped)) or passed != committed + skipped:
        failures.append("pass_commit_conservation_unproven")
    # The current caller provides configuration receipts and TCP binding samples,
    # not a verified capture covering every owned flow. Fail closed until a
    # capture verifier can establish independent TCP/UDP/IPv4/IPv6/DNS paths.
    checks_passed = not failures
    failures.append("packet_path_unproven")
    return {"accepted": False, "failures": failures,
            "configuration_and_report_checks_passed": checks_passed,
            "evidence_class": "configuration_and_report_only", "target": target}
