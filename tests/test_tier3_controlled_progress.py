from test_support.tier3_local_pipeline_probe import progress_summary


def test_missing_readiness_is_not_successful_liveness():
    assert progress_summary([{"event": "started", "elapsed": 1}], 3)["ready"] is False


def test_facility_chatter_and_shutdown_results_cannot_hide_internal_stall():
    trace = [
        {"event": "helper_ready", "elapsed": 1},
        {"event": "started", "elapsed": 2},
        {"event": "validation_completion", "elapsed": 4, "transport": False},
        {"event": "helper_ready", "elapsed": 6},
        {"event": "validation_completion", "elapsed": 9, "transport": True},
    ]
    summary = progress_summary(trace, 8)
    assert summary["events"] == 1
    assert summary["max_gap_seconds"] == 6


def test_actual_transport_results_continue_progress_after_tcp_inventory_ends():
    trace = [
        {"event": "started", "elapsed": .1},
        {"event": "helper_ready", "elapsed": 1},
        {"event": "started", "elapsed": 2},
        {"event": "validation_completion", "elapsed": 5, "transport": True},
        {"event": "validation_completion", "elapsed": 9, "transport": True},
    ]
    summary = progress_summary(trace, 10)
    assert summary["events"] == 3
    assert summary["max_gap_seconds"] == 4
