from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from desktop_runtime import (
    DesktopRuntime,
    RuntimeControllerReadbackError,
    RuntimeStartCancelled,
)
from runtime_egress_recovery import RuntimeEgressReadinessState
from tests.test_runtime_ready_arm_regressions import _initial_activation_fixture


def _system_proxy_admission():
    runtime, runner, events = _initial_activation_fixture(capture_wanted=True)
    # The shared fixture's old phase stub inferred capture from user intent,
    # masking the production no-ApplyConfig path's missing phase commit.
    del runtime._current_core_runtime_phase
    runtime._core_runtime_phase = "formal_no_capture"
    runtime.network_mode = "system_proxy"
    runtime.system_proxy = SimpleNamespace(active=False)
    manager = SimpleNamespace(
        lock=threading.RLock(),
        tier3_traffic_capture_active=False,
        effective_egress_ok=True,
    )
    runtime.manager = manager
    runtime._tier3_is_primary_export = lambda: True
    runtime._tier3_standby_activation_key = lambda: ("capture-fixture",)
    runtime._record_tier3_initial_activation_stage = lambda *_args, **_kwargs: {}
    runtime._set_tier3_source_control_qualification = lambda *_args: None
    runtime._runtime_recovery_is_armed = lambda: True
    runtime._coalesce_pre_ready_activation_successor = lambda _lease: None
    diagnostics = []
    runtime._record_revalidation_lifecycle_diagnostic = (
        lambda reason, **kwargs: diagnostics.append((reason, kwargs))
    )

    def mark_capture(active):
        manager.tier3_traffic_capture_active = bool(active)
        events.append(("capture-marker", bool(active)))

    manager.mark_tier3_traffic_capture_active = mark_capture

    def apply_capture(_runner):
        runtime.system_proxy.active = True
        events.append(("capture-published",))

    def restore_capture():
        runtime.system_proxy.active = False
        events.append(("capture-restored",))

    def verify_host(**_kwargs):
        assert runtime._current_core_runtime_phase() == "formal_no_capture"
        assert manager.tier3_traffic_capture_active is False
        assert runtime.system_proxy.active is True
        events.append(("host-verified",))
        return {"status": 204, "verified": True}

    runtime._apply_system_proxy_capture_after_core_start = apply_capture
    runtime.restore_system_proxy = restore_capture
    runtime._verify_system_proxy_host_capture = verify_host
    return runtime, runner, events, diagnostics


def _admit(runtime, runner):
    return runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )


def test_system_proxy_host_proof_commits_capture_phase_without_reload():
    runtime, runner, events, diagnostics = _system_proxy_admission()

    assert _admit(runtime, runner)["status"] == 204

    assert runtime._current_core_runtime_phase() == "formal_capture"
    assert runtime.manager.tier3_traffic_capture_active is True
    assert runtime._runtime_initial_activation_complete() is True
    assert runtime._revalidation_mode_for_runtime_state(
        "incremental", source="queue"
    ) == "incremental"
    assert diagnostics == []
    assert ("reload", True) not in events
    assert events.index(("host-verified",)) < events.index(("capture-marker", True))


def test_failed_host_proof_does_not_publish_capture_phase():
    runtime, runner, events, _diagnostics = _system_proxy_admission()

    def fail_host(**_kwargs):
        raise TimeoutError("host path did not pass")

    runtime._verify_system_proxy_host_capture = fail_host
    with pytest.raises(TimeoutError, match="host path did not pass"):
        _admit(runtime, runner)

    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert runtime.manager.tier3_traffic_capture_active is False
    assert runtime.system_proxy.active is False
    assert ("capture-marker", True) not in events
    assert ("readiness", RuntimeEgressReadinessState.READY) not in events


def test_failed_ready_preparation_restores_no_capture_phase_without_reload():
    runtime, runner, events, _diagnostics = _system_proxy_admission()
    runtime._try_arm_runtime_recovery = lambda **_kwargs: False

    with pytest.raises(RuntimeControllerReadbackError, match="could not prepare"):
        _admit(runtime, runner)

    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert runtime.manager.tier3_traffic_capture_active is False
    assert runtime.system_proxy.active is False
    assert ("capture-marker", True) in events
    assert ("capture-marker", False) in events
    assert ("reload", True) not in events
    assert ("rollback",) not in events


@pytest.mark.parametrize("invalidation", [
    "stop", "core_generation", "process_generation", "runtime_generation",
    "lifecycle_revision", "selector_intent",
])
def test_capture_publication_rechecks_authority_after_monitor_start(invalidation):
    runtime, runner, events, _diagnostics = _system_proxy_admission()

    def start_monitor():
        events.append(("traffic-start",))
        if invalidation == "stop":
            runtime.stop_event.set()
        elif invalidation == "process_generation":
            old_status = runner.status()
            runner.status = lambda: {
                **old_status,
                "process_generation": old_status["process_generation"] + 1,
            }
        else:
            attribute = {
                "core_generation": "_core_generation",
                "runtime_generation": "_runtime_generation",
                "lifecycle_revision": "_lifecycle_revision",
                "selector_intent": "_runtime_selector_intent_revision",
            }[invalidation]
            setattr(runtime, attribute, getattr(runtime, attribute) + 1)

    runtime.traffic_monitor.start = start_monitor
    with pytest.raises(RuntimeStartCancelled):
        _admit(runtime, runner)

    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert runtime.manager.tier3_traffic_capture_active is False
    assert runtime.system_proxy.active is False
    assert ("capture-marker", True) not in events
    assert ("traffic-stop",) in events
    assert ("readiness", RuntimeEgressReadinessState.READY) not in events


def test_stop_after_capture_publication_rolls_back_without_ready_or_reload():
    runtime, runner, events, _diagnostics = _system_proxy_admission()

    def stop_during_arm(**_kwargs):
        assert runtime._revalidation_runtime_capture_state() == (
            "formal_capture", True,
        )
        runtime.stop_event.set()
        return True

    runtime._try_arm_runtime_recovery = stop_during_arm
    with pytest.raises(RuntimeStartCancelled):
        _admit(runtime, runner)

    assert runtime._revalidation_runtime_capture_state() == (
        "formal_no_capture", False,
    )
    assert runtime.system_proxy.active is False
    assert ("traffic-stop",) in events
    assert ("capture-marker", True) in events
    assert ("capture-marker", False) in events
    assert ("readiness", RuntimeEgressReadinessState.READY) not in events
    assert ("reload", True) not in events
    assert ("rollback",) not in events


@pytest.mark.parametrize("network_mode", ["tun", "mixed"])
def test_tun_capture_still_reloads_and_verifies_before_ready(network_mode):
    runtime, runner, events = _initial_activation_fixture(capture_wanted=True)
    runtime.network_mode = network_mode

    assert _admit(runtime, runner)["status"] == 204

    capture_probe = next(event for event in events if event[0] == "capture-probe")
    assert events.index(("reload", True)) < events.index(capture_probe)
    assert events.index(capture_probe) < events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    )


def test_provider_revalidation_waits_for_whole_capture_state_commit():
    runtime, runner, _events, diagnostics = _system_proxy_admission()
    publication_entered = threading.Event()
    finish_publication = threading.Event()
    read_started = threading.Event()
    read_finished = threading.Event()
    errors = []
    modes = []
    original_marker = runtime.manager.mark_tier3_traffic_capture_active

    def delayed_marker(active):
        if active:
            publication_entered.set()
            assert finish_publication.wait(2.0)
        original_marker(active)

    def activate():
        try:
            _admit(runtime, runner)
        except Exception as exc:
            errors.append(exc)

    def revalidate():
        read_started.set()
        try:
            modes.append(runtime._revalidation_mode_for_runtime_state(
                "incremental", source="queue"
            ))
        except Exception as exc:
            errors.append(exc)
        finally:
            read_finished.set()

    runtime.manager.mark_tier3_traffic_capture_active = delayed_marker
    activation = threading.Thread(target=activate)
    reader = threading.Thread(target=revalidate)
    activation.start()
    try:
        assert publication_entered.wait(2.0)
        reader.start()
        assert read_started.wait(1.0)
        assert not read_finished.wait(0.05)
    finally:
        finish_publication.set()
        activation.join(2.0)
        if reader.ident is not None:
            reader.join(2.0)

    assert not activation.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert modes == ["incremental"]
    assert diagnostics == []


@pytest.mark.parametrize("phase,active", [
    ("formal_no_capture", True),
    ("formal_capture", False),
])
def test_real_capture_state_mismatch_remains_an_invariant_failure(phase, active):
    runtime, _runner, _events, diagnostics = _system_proxy_admission()
    runtime._core_runtime_phase = phase
    runtime.manager.tier3_traffic_capture_active = active

    assert runtime._revalidation_mode_for_runtime_state(
        "incremental", source="queue"
    ) is None
    runtime._queue_incremental_egress_recheck()

    assert len(diagnostics) == 2
    assert all(item[1]["fail_closed"] is True for item in diagnostics)
