import json
from pathlib import Path

import pytest

from scripts.tier3_audit_acceptance import CLOSED_RESOURCE_COUNTS, evaluate_tier3_audit


def evidence():
    report = dict(
        running=False, source_stream_shutdown_confirmed=True,
        candidate_scheduler_active_ticket_count=0,
        source_stream_admission_commit_pending_count=0,
        source_stream_validation_submit_pending_count=0,
        source_stream_time_to_pool_revision_3_ms=9000,
        source_stream_time_to_pool_target_ms=51000,
        source_stream_alive_at_20s=15, source_stream_committed_alive_count=25,
        source_stream_full_validation_passed_count=26,
        source_stream_admission_commit_committed_count=25,
        source_stream_admission_commit_skipped_count=1,
        source_stream_runtime_identity_match=True,
        source_stream_performance_sample_valid=True,
        source_supply_worker_alive=False,
        source_stream_process_hard_stall=False,
        source_stream_tcp_start_acked_count=100,
        source_stream_tcp_prestart_started_total=100,
        candidate_scheduler_selected_total=100,
        source_stream_candidate_scan_total_granted=100,
    )
    report.update({key: 0 for key in CLOSED_RESOURCE_COUNTS})
    isolation = {key: True for key in (
        "physical_interface_bound", "negative_tcp_control_rejected",
        "owned_dns_control_passed", "capture_disabled", "external_network_unchanged",
        "outbound_socket_guard_installed", "auxiliary_proxy_disabled",
        "non_owned_loopback_blocked",
        "all_owned_processes_guarded",
    )}
    return report, isolation


def check(report, isolation, **kwargs):
    return evaluate_tier3_audit(report, isolation, clean_exit=True,
                               source_unchanged=True, **kwargs)


def test_normal_exit_with_eight_commits_is_not_performance_acceptance():
    report, isolation = evidence()
    report.update(source_stream_committed_alive_count=8,
                  source_stream_time_to_pool_target_ms=None,
                  source_stream_alive_at_20s=6,
                  source_stream_full_validation_passed_count=8,
                  source_stream_admission_commit_committed_count=8,
                  source_stream_admission_commit_skipped_count=0)
    result = check(report, isolation)
    assert not result["accepted"]
    assert "target_not_reached" in result["failures"]
    assert "n20_below_15" in result["failures"]


def test_actual_field_eight_fixture_cannot_be_passed_by_fast_start_or_clean_stop():
    path = Path(__file__).parent / "fixtures/tier3_field_eight_regression_v87.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    _, isolation = evidence()
    result = check(fixture["report"], isolation)
    assert all(failure in result["failures"] for failure in [
        "milestone_failed:source_stream_time_to_pool_target_ms",
        "n20_below_15", "target_not_reached",
    ])
    assert fixture["individual_transport_trace"] == "unavailable"
    assert "://" not in path.read_text(encoding="utf-8")


def test_tun_backed_success_does_not_prove_independent_performance():
    report, _ = evidence()
    result = check(report, {"capture_disabled": True})
    assert not result["accepted"]
    assert "isolation_unproven:physical_interface_bound" in result["failures"]


@pytest.mark.parametrize("missing", ["outbound_socket_guard_installed",
                                     "auxiliary_proxy_disabled", "non_owned_loopback_blocked",
                                     "all_owned_processes_guarded"])
def test_physical_mihomo_alone_does_not_prove_auxiliary_tun_bypass(missing):
    report, isolation = evidence()
    isolation.pop(missing)
    result = check(report, isolation)
    assert not result["accepted"]
    assert f"isolation_unproven:{missing}" in result["failures"]


def test_missing_or_stale_closed_snapshot_cannot_pass():
    report, isolation = evidence()
    for key in ("source_stream_shutdown_confirmed", "candidate_scheduler_active_ticket_count"):
        invalid = dict(report)
        invalid.pop(key)
        assert not check(invalid, isolation)["accepted"]
    report["source_stream_admission_commit_pending_count"] = 7
    assert not check(report, isolation)["accepted"]


def test_complete_synthetic_evidence_passes_only_with_conservation():
    report, isolation = evidence()
    result = check(report, isolation)
    assert result["configuration_and_report_checks_passed"]
    assert not result["accepted"]
    assert result["evidence_class"] == "configuration_and_report_only"
    assert "packet_path_unproven" in result["failures"]
    report["source_stream_full_validation_passed_count"] += 1
    assert not check(report, isolation)["configuration_and_report_checks_passed"]


def test_later_refill_cannot_replace_failed_cold_milestones():
    report, isolation = evidence()
    report.update(runtime_generation=1, source_stream_time_to_pool_revision_3_ms=0,
                  source_stream_alive_at_20s=25, source_stream_time_to_pool_target_ms=1000)
    report["completed_session_summaries"] = [dict(
        runtime_generation=1, refresh_epoch=0, initial_healthy=0, final_healthy=17,
        time_to_3_ms=43141, time_to_target_ms=None, alive_at_20s=1,
    )]
    result = check(report, isolation)
    assert not result["accepted"]
    assert "n20_below_15" in result["failures"]
    assert "target_not_reached" in result["failures"]


def test_already_healthy_zero_millisecond_start_is_not_a_cold_result():
    report, isolation = evidence()
    report["source_stream_time_to_pool_revision_3_ms"] = 0
    assert not check(report, isolation)["accepted"]


@pytest.mark.parametrize("key", CLOSED_RESOURCE_COUNTS)
@pytest.mark.parametrize("value", [None, False, "0", 7])
def test_every_closed_owner_must_prove_zero_resources(key, value):
    report, isolation = evidence()
    report[key] = value
    assert f"active_resource:{key}" in check(report, isolation)["failures"]


@pytest.mark.parametrize("key", [
    "source_stream_runtime_identity_match", "source_stream_performance_sample_valid",
])
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_fast_target_with_unverified_build_is_not_accepted(key, value):
    report, isolation = evidence()
    report[key] = value
    assert "build_identity_unproven" in check(report, isolation)["failures"]


def test_source_worker_or_observed_hard_stall_prevents_acceptance():
    report, isolation = evidence()
    report["source_supply_worker_alive"] = True
    report["source_stream_process_hard_stall"] = True
    assert {"source_worker_exit_unconfirmed", "validation_liveness_unproven"}.issubset(
        check(report, isolation)["failures"])


def test_source_lease_acceptance_uses_supervisor_owner_not_optional_alias():
    report, isolation = evidence()
    report.pop("source_supply_active_lease_count", None)
    assert check(report, isolation)["configuration_and_report_checks_passed"]
    report["source_supply_active_lease_count"] = 0
    report["source_supply_supervisor_active_leases"] = 1
    assert "active_resource:source_supply_supervisor_active_leases" in check(
        report, isolation)["failures"]
    report.pop("source_supply_supervisor_active_leases")
    assert "active_resource:source_supply_supervisor_active_leases" in check(
        report, isolation)["failures"]


@pytest.mark.parametrize("key", [
    "source_stream_tcp_start_acked_count", "source_stream_tcp_prestart_started_total",
    "candidate_scheduler_selected_total", "source_stream_candidate_scan_total_granted",
])
def test_stale_tcp_totals_cannot_pass_closed_report_acceptance(key):
    report, isolation = evidence()
    report[key] = 96
    assert "tcp_start_conservation_unproven" in check(report, isolation)["failures"]


def test_self_reported_packet_flag_is_not_capture_evidence():
    report, isolation = evidence()
    isolation.update(packet_path_verified=True, tun_bypassed=True,
                     evidence_class='independent_network')
    result = check(report, isolation)
    assert not result['accepted']
    assert result['evidence_class'] == 'configuration_and_report_only'
    assert 'packet_path_unproven' in result['failures']
