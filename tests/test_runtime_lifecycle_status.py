import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bridge_protocol import validate_service_state
from bridge_runtime_access import RuntimeAccess, service_state_data
from desktop_runtime import DesktopRuntime
from core_runner import MihomoCoreRunner
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected external access: {name}")


class Runner:
    def lightweight_fact(self):
        return {"running": True, "pid": 42, "process_generation": 3,
                "cleanup_pending": False}


def minimal_runtime():
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime._runtime_intent_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.Lock()
    runtime._tier3_egress_activation_lock = threading.Lock()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._tier3_pool_sync_lock = threading.Lock()
    runtime._tier3_core_gate_lock = threading.Lock()
    runtime._exit_quality_static_refresh_lock = threading.Lock()
    runtime._runtime_failure_monitor_lock = threading.Lock()
    runtime._tier3_egress_activation_thread = None
    runtime._tier3_egress_activation_worker_running = False
    runtime._tier3_pool_sync_thread = None
    runtime._tier3_pool_sync_worker_running = False
    runtime._tier3_core_gate_thread = None
    runtime._tier3_core_gate_worker_running = False
    runtime._exit_quality_static_refresh_thread = None
    runtime._runtime_failure_monitor_thread = None
    runtime._runtime_failure_recovery_threads = set()
    runtime.monitor_thread = None
    runtime.last_error = None
    runtime._tier3_required_for_start = False
    runtime._tier3_startup_terminal_failure = False
    runtime._start_deferred_is_live = False
    runtime._lifecycle_revision = 4
    runtime._runtime_generation = 2
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime._last_runtime_stop = {}
    runtime._core_generation = 7
    runtime.core_runner = Runner()
    runtime._runtime_route_epoch = 9
    runtime._runtime_network_context_epoch = 5
    runtime._runtime_route_snapshot = SimpleNamespace(
        process_pid=42, process_generation=3, core_generation=7)
    runtime._runtime_egress_readiness_publication_revision = 8
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY, validation_active=False,
        lifecycle_revision=4, route_epoch=9, network_context_epoch=5)
    runtime.manager = Forbidden()
    runtime.api_client = Forbidden()
    return runtime


def test_light_sample_is_immutable_and_valid(monkeypatch):
    import winreg
    monkeypatch.setattr(winreg, "OpenKey", lambda *a, **k: pytest.fail("registry read"))
    runtime = minimal_runtime()
    sample = runtime.lifecycle_status()
    assert not sample["freshness"]["stale"]
    assert sample["runtime"]["startup_phase"] == "running"
    assert validate_service_state(service_state_data(sample)) == []
    with pytest.raises(TypeError):
        sample["readiness"]["state"] = "fail_closed"
    assert runtime._runtime_egress_readiness.state is RuntimeEgressReadinessState.READY


def test_new_core_with_old_readiness_is_stale():
    runtime = minimal_runtime()
    runtime._core_generation = 8
    sample = runtime.lifecycle_status()
    assert sample["freshness"]["stale"]
    assert sample["runtime"]["startup_phase"] != "running"
    assert validate_service_state(service_state_data(sample)) == []


def test_lifecycle_revision_mismatch_is_stale():
    runtime = minimal_runtime()
    runtime._lifecycle_revision += 1
    assert runtime.lifecycle_status()["freshness"]["stale"]


def test_same_route_reread_after_ready_is_not_stale():
    # 11-01: every re-read of the same Core-loaded route (or an append-only
    # provider commit) stamps a newer loaded_at than the READY publication.
    # Owner and epochs are unchanged, so the sample stays fresh.
    runtime = minimal_runtime()
    runtime._runtime_egress_readiness = replace(
        runtime._runtime_egress_readiness, updated_at_monotonic=1.0)
    runtime._runtime_route_snapshot = SimpleNamespace(
        process_pid=42, process_generation=3, core_generation=7,
        loaded_at_monotonic=2.0)
    sample = runtime.lifecycle_status()
    assert not sample["freshness"]["stale"]
    assert sample["runtime"]["startup_phase"] == "running"


def test_ready_without_a_loaded_route_is_stale():
    runtime = minimal_runtime()
    runtime._runtime_route_snapshot = None
    assert runtime.lifecycle_status()["freshness"]["stale"]


def test_network_context_change_after_ready_is_stale():
    runtime = minimal_runtime()
    runtime._runtime_network_context_epoch += 1
    assert runtime.lifecycle_status()["freshness"]["stale"]


def test_route_epoch_advance_without_new_readiness_is_stale():
    # The predicate itself never relaxes: a selection move after READY
    # (native failover, provisional move, lease release) stays stale until
    # readiness is re-published.  Decision D (11-04) re-publishes it through
    # the READY carry (_maybe_carry_runtime_ready, tests/test_runtime_ready_carry.py),
    # never by loosening this rule.
    runtime = minimal_runtime()
    runtime._runtime_route_epoch += 1
    assert runtime.lifecycle_status()["freshness"]["stale"]


def test_core_runner_lightweight_fact_owns_no_process_reference():
    runner = MihomoCoreRunner.__new__(MihomoCoreRunner)
    runner._lock = threading.RLock()
    runner.process = SimpleNamespace(pid=42, poll=lambda: None)
    runner._reader_thread = None
    runner._process_generation = 3
    fact = runner.lightweight_fact()
    assert dict(fact) == {"running": True, "pid": 42,
                          "process_generation": 3, "cleanup_pending": False}
    with pytest.raises(TypeError):
        fact["pid"] = 8


def assert_wire_valid(runtime):
    sample = runtime.lifecycle_status()
    assert validate_service_state(service_state_data(sample)) == []
    return sample


def test_completed_stop_ignores_latched_cancel_event_and_adapts():
    runtime = minimal_runtime()
    runtime.running = False
    runtime._desired_running = False
    runtime.stop_event.set()
    runtime._last_runtime_stop = {"result": "stopped"}
    runtime.core_runner.lightweight_fact = lambda: {
        "running": False, "pid": None, "process_generation": 3,
        "cleanup_pending": False}
    sample = assert_wire_valid(runtime)
    assert sample["runtime"]["startup_phase"] == "stopped"
    assert not sample["intent"]["stop_requested"]
    runtime.stop = lambda *, stop_intent: {"shutdown_exit_code": 0}
    from bridge_runtime_access import StopRequest
    result = RuntimeAccess(runtime).stop(stop_request=StopRequest(None, {}))
    assert result["startup_phase"] == "stopped"
    assert result["cleanup_pending"] is False


@pytest.mark.parametrize("owner", ["monitor_thread", "_tier3_egress_activation_thread",
                                   "_runtime_failure_recovery_threads",
                                   "_exit_quality_static_refresh_thread"])
def test_failed_stop_with_live_cleanup_owner(owner):
    runtime = minimal_runtime()
    runtime.manager = None
    runtime.running = False
    runtime._desired_running = False
    runtime.stop_event.set()
    runtime._last_runtime_stop = {"result": "failed", "error_code": "timeout"}
    runtime.core_runner.lightweight_fact = lambda: {
        "running": False, "pid": None, "process_generation": 3,
        "cleanup_pending": False}
    worker = SimpleNamespace(is_alive=lambda: True)
    setattr(runtime, owner, [worker] if owner.endswith("threads") else worker)
    sample = assert_wire_valid(runtime)
    assert sample["runtime"]["startup_phase"] == "failed"
    assert sample["runtime"]["shutdown_cleanup_pending"]
    assert sample["runtime"]["failure_code"] == "timeout"
    runtime.stop = lambda *, stop_intent: {
        "shutdown_exit_code": 0, "shutdown_cleanup_pending": True}
    from bridge_runtime_access import StopRequest
    adapted = RuntimeAccess(runtime).stop(stop_request=StopRequest(None, {}))
    assert adapted["cleanup_pending"]
    assert adapted["startup_phase"] == "failed"


def test_terminal_start_failure_and_live_deferred_priority():
    runtime = minimal_runtime()
    runtime.core_runner.lightweight_fact = lambda: {
        "running": False, "pid": None, "process_generation": 3,
        "cleanup_pending": False}
    runtime._tier3_required_for_start = True
    runtime._tier3_startup_terminal_failure = True
    runtime.last_error = "source_supply_no_egress: exhausted"
    failed = assert_wire_valid(runtime)
    assert failed["runtime"]["startup_phase"] == "failed"
    assert failed["runtime"]["failure_code"] == "source_supply_no_egress"
    runtime.start = lambda: {"startup_phase": "failed", "last_error": runtime.last_error}
    assert RuntimeAccess(runtime).start() == {
        "startup_phase": "failed", "failure_code": "source_supply_no_egress"}
    runtime._tier3_startup_terminal_failure = False
    runtime._start_deferred_is_live = True
    assert assert_wire_valid(runtime)["runtime"]["startup_phase"] == "collecting"


def test_failed_stop_with_validation_activity_flag():
    runtime = minimal_runtime()
    runtime.manager = None
    runtime.running = False
    runtime._desired_running = False
    runtime._last_runtime_stop = {"result": "failed"}
    runtime.core_runner.lightweight_fact = lambda: {
        "running": False, "pid": None, "process_generation": 3,
        "cleanup_pending": False}
    runtime._tier3_egress_activation_worker_running = True
    sample = assert_wire_valid(runtime)
    assert sample["runtime"]["startup_phase"] == "failed"
    assert sample["runtime"]["shutdown_cleanup_pending"]
    assert sample["runtime"]["failure_code"] == "internal"


def test_version_switch_during_core_sample_is_stale():
    runtime = minimal_runtime()
    original = runtime.core_fact
    def switched():
        runtime._lifecycle_revision += 1
        runtime._core_generation += 1
        return original()
    runtime.core_fact = switched
    sample = assert_wire_valid(runtime)
    assert sample["freshness"]["stale"]
    assert sample["runtime"]["startup_phase"] != "running"
