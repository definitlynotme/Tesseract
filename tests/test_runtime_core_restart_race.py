"""Core-restart race fix (10-26): health restarts are serialized and generation-bound."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

import desktop_runtime as dr
from desktop_runtime import DesktopRuntime


class FakeRunner:
    def __init__(self, *, running: bool = True, port_ok: bool = False) -> None:
        self.running = running
        self.port_ok = port_ok
        self.stops = 0
        self.stop_result: dict | None = None
        self.inside_start = threading.Event()

    def is_running(self) -> bool:
        return self.running

    def check_proxy_port(self, host="127.0.0.1", port=7890) -> bool:
        return self.port_ok

    def stop(self):
        assert not self.inside_start.is_set(), "stop() entered while another owner is starting the Core"
        self.stops += 1
        self.running = False
        return dict(self.stop_result) if self.stop_result is not None else {
            "stopped": True, "still_running": False, "cleanup_pending": False,
        }

    def status(self):
        return {"running": self.running}


def build(runner: FakeRunner | None = None):
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-core-restart-race")
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime.last_error = None
    runtime.health_failure_count = 0
    runtime._health_failure_generation = -1
    runtime._core_health_defer_logged_at = 0.0
    runtime._core_transition_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.Lock()
    runtime._core_generation = 1
    runtime.core_runner = runner or FakeRunner()
    runtime._get_core_runner = lambda: runtime.core_runner
    runtime.check_proxy_port = lambda: runtime.core_runner.port_ok
    phases: list[str] = []
    runtime._set_core_runtime_phase = lambda phase: phases.append(phase)
    starts: list[int] = []

    def start_core():
        with runtime._core_transition_lock:
            starts.append(runtime._core_generation)
            with runtime._core_lifecycle_lock:
                runtime._core_generation += 1
            runtime.core_runner.running = True
            return {"running": True}

    runtime.start_core = start_core
    revokes: list[str] = []
    runtime._revoke_core_provider_authority = lambda reason="": revokes.append(reason)
    runtime.test = SimpleNamespace(starts=starts, phases=phases, revokes=revokes)
    return runtime


def restart(runtime, **kwargs):
    return runtime._attempt_core_restart_after_health_failure(
        failure_message="Failed to restart mihomo after proxy port health failure",
        **kwargs,
    )


def test_generation_change_between_detection_and_lock_skips_everything(caplog):
    caplog.set_level(logging.INFO)
    runtime = build()
    runtime._core_generation = 2
    assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "skipped"
    assert runtime.core_runner.stops == 0 and runtime.test.starts == []
    assert any("Mihomo health restart skipped (reason=superseded_generation" in r.getMessage() for r in caplog.records)


def test_health_restart_racing_a_user_restart_never_kills_the_new_core():
    runner = FakeRunner(running=True, port_ok=False)
    runtime = build(runner)
    in_lock = threading.Event()
    let_go = threading.Event()

    def user_restart():
        # restart_core: stop + start_core inside the transition lock.
        with runtime._core_transition_lock:
            runner.stop()
            runner.inside_start.set()
            in_lock.set()
            let_go.wait(2.0)
            with runtime._core_lifecycle_lock:
                runtime._core_generation += 1
            runner.running = True
            runner.inside_start.clear()

    observed = runtime._core_generation
    thread = threading.Thread(target=user_restart)
    thread.start()
    assert in_lock.wait(2.0)
    outcome: list[str] = []
    health = threading.Thread(
        target=lambda: outcome.append(restart(runtime, stop_core_first=True, observed_core_generation=observed))
    )
    health.start()
    time.sleep(0.1)
    let_go.set()
    thread.join(2.0)
    health.join(3.0)
    assert outcome == ["skipped"]
    assert runner.stops == 1  # only the user's own stop
    assert runtime._core_generation == observed + 1  # exactly one new generation
    assert runtime.test.starts == []


def test_busy_transition_lock_defers_without_stop_or_start(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(dr, "CORE_HEALTH_RESTART_LOCK_WAIT_S", 0.05)
    runtime = build()
    held = threading.Event()
    done = threading.Event()

    def hold():
        with runtime._core_transition_lock:
            held.set()
            done.wait(2.0)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(1.0)
    try:
        assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "deferred"
        assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "deferred"
    finally:
        done.set()
        thread.join(2.0)
    assert runtime.core_runner.stops == 0 and runtime.test.starts == []
    deferred = [r for r in caplog.records if "Mihomo health restart deferred" in r.getMessage()]
    assert len(deferred) == 1  # rate limited


def test_deferred_port_restart_keeps_the_failure_count_due(monkeypatch):
    runtime = build(FakeRunner(running=True, port_ok=False))
    runtime._core_generation = 1
    runtime._health_failure_generation = 1
    runtime.health_failure_count = 5
    runtime._system_proxy_capture_state = lambda: (True, True)
    runtime._system_proxy_config = lambda: {}
    runtime._set_effective_egress_readiness_state = lambda *a, **k: None
    runtime._set_effective_egress_state = lambda *a, **k: None
    calls: list[dict] = []

    def helper(**kwargs):
        calls.append(kwargs)
        return "deferred"

    runtime._attempt_core_restart_after_health_failure = helper
    runtime.network_mode = "system_proxy"
    runtime.system_proxy = SimpleNamespace(active=True)
    monkeypatch.setattr(dr, "tun_mode_core_health_monitor_active", lambda **_k: False)
    runtime._monitor_core_health()
    assert calls and calls[0]["observed_core_generation"] == 1 and calls[0]["stop_core_first"] is True
    assert runtime.health_failure_count == 6


def test_failure_count_resets_when_the_generation_changes(monkeypatch):
    runtime = build(FakeRunner(running=True, port_ok=False))
    runtime._health_failure_generation = 1
    runtime.health_failure_count = 5
    runtime._core_generation = 2
    runtime._system_proxy_capture_state = lambda: (True, True)
    runtime._system_proxy_config = lambda: {}
    runtime._set_effective_egress_readiness_state = lambda *a, **k: None
    runtime._set_effective_egress_state = lambda *a, **k: None
    runtime._attempt_core_restart_after_health_failure = lambda **_k: pytest.fail("no restart on a fresh generation")
    runtime.network_mode = "system_proxy"
    runtime.system_proxy = SimpleNamespace(active=True)
    monkeypatch.setattr(dr, "tun_mode_core_health_monitor_active", lambda **_k: False)
    runtime._monitor_core_health()
    assert runtime.health_failure_count == 1
    assert runtime._health_failure_generation == 2


def test_port_healthy_under_the_lock_skips_the_stop():
    runtime = build(FakeRunner(running=True, port_ok=True))
    assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "skipped"
    assert runtime.core_runner.stops == 0 and runtime.test.starts == []


def test_unexpected_exit_branch_never_stops_and_skips_a_running_core():
    runtime = build(FakeRunner(running=True))
    assert restart(runtime, observed_core_generation=1) == "skipped"
    assert runtime.core_runner.stops == 0
    runtime.core_runner.running = False
    assert restart(runtime, observed_core_generation=1) == "restarted"
    assert runtime.core_runner.stops == 0 and runtime.test.starts == [1]


def test_tun_branch_goes_through_the_helper(monkeypatch):
    runtime = build(FakeRunner(running=False))
    runtime._health_failure_generation = 1
    runtime.health_failure_count = 5
    runtime._system_proxy_config = lambda: {}
    runtime._set_effective_egress_state = lambda *a, **k: None
    runtime.network_mode = "tun"
    runtime.system_proxy = SimpleNamespace(active=False)
    monkeypatch.setattr(dr, "tun_mode_core_health_monitor_active", lambda **_k: True)
    calls: list[dict] = []
    runtime._attempt_core_restart_after_health_failure = lambda **kw: calls.append(kw) or "deferred"
    runtime._monitor_core_health()
    assert calls[0]["observed_core_generation"] == 1
    assert calls[0].get("stop_core_first", False) is False
    assert runtime.health_failure_count == 6  # deferred keeps the count
    runtime._attempt_core_restart_after_health_failure = lambda **kw: "restarted"
    runtime._monitor_core_health()
    assert runtime.health_failure_count == 0


def test_stop_event_returns_before_and_inside_the_lock():
    runtime = build()
    runtime.stop_event.set()
    assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "stopping"
    assert runtime.core_runner.stops == 0
    runtime.stop_event.clear()
    original = runtime._core_lifecycle_lock

    class SetStopOnEnter:
        def __enter__(self):
            runtime.stop_event.set()
            return original.__enter__()

        def __exit__(self, *exc):
            return original.__exit__(*exc)

    runtime._core_lifecycle_lock = SetStopOnEnter()
    assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "stopping"
    assert runtime.core_runner.stops == 0 and runtime.test.starts == []


def test_incomplete_stop_aborts_without_start_and_sets_last_error(caplog):
    caplog.set_level(logging.INFO)
    runner = FakeRunner(running=True, port_ok=False)
    runner.stop_result = {"stopped": False, "still_running": True}
    runtime = build(runner)
    assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "failed"
    assert runtime.test.starts == []
    assert runtime.last_error
    assert any("Mihomo health restart aborted (reason=stop_incomplete" in r.getMessage() for r in caplog.records)


def test_port_unhealthy_restart_stops_then_starts_once_and_never_revokes():
    runtime = build(FakeRunner(running=True, port_ok=False))
    assert restart(runtime, stop_core_first=True, observed_core_generation=1) == "restarted"
    assert runtime.core_runner.stops == 1
    assert runtime.test.phases == ["stopped"]
    assert runtime.test.starts == [1] and runtime._core_generation == 2
    assert runtime.test.revokes == []


PENDING = "mihomo proxy port is unhealthy; core restart is pending"


def test_egress_is_demoted_only_when_the_restart_proceeds():
    runtime = build(FakeRunner(running=True, port_ok=True))
    states: list[tuple] = []
    runtime._set_effective_egress_state = lambda ok, reason="": states.append((ok, reason))
    # The port recovered before the lock: no restart, and no stale "pending".
    assert restart(runtime, stop_core_first=True, observed_core_generation=1, pending_egress_reason=PENDING) == "skipped"
    assert states == []
    runtime.core_runner.port_ok = False
    seen_at_stop: list[list] = []
    stop = runtime.core_runner.stop
    runtime.core_runner.stop = lambda: (seen_at_stop.append(list(states)), stop())[1]
    assert restart(runtime, stop_core_first=True, observed_core_generation=1, pending_egress_reason=PENDING) == "restarted"
    assert states == [(False, PENDING)]
    assert seen_at_stop == [[(False, PENDING)]]  # demoted before the stop


def test_monitor_leaves_the_port_failure_demotion_to_the_helper(monkeypatch):
    runtime = build(FakeRunner(running=True, port_ok=False))
    runtime._health_failure_generation = 1
    runtime.health_failure_count = 5
    runtime._system_proxy_capture_state = lambda: (True, True)
    runtime._system_proxy_config = lambda: {}
    runtime._set_effective_egress_readiness_state = lambda *a, **k: None
    states: list[tuple] = []
    runtime._set_effective_egress_state = lambda ok, reason="": states.append((ok, reason))
    calls: list[dict] = []
    runtime._attempt_core_restart_after_health_failure = lambda **kw: calls.append(kw) or "skipped"
    runtime.network_mode = "system_proxy"
    runtime.system_proxy = SimpleNamespace(active=True)
    monkeypatch.setattr(dr, "tun_mode_core_health_monitor_active", lambda **_k: False)
    runtime._monitor_core_health()
    assert calls and calls[0]["pending_egress_reason"] == PENDING
    assert states == []
    assert runtime.health_failure_count == 0
