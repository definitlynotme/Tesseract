"""Post-dial observer wiring (node-judgement design v3 P1-1 tap, P1-3 thread).

The pure classifier has its own suite (test_runtime_post_dial.py).  These
tests pin the integration: what the stdout reader thread may do, when the
``runtime-post-dial-observer`` thread starts and stops, that an idle observer
makes no controller call, and that a replayed zombie reaches a shadow
``would_trigger`` line through the real thread, with nothing but tokens and
hashes in the log.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import logging
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import Mock

import pytest
import yaml

import runtime_post_dial
import runtime_post_dial_observer
from clash_formatter import build_full_clash_config
from core_runner import CoreOutputEvent
from desktop_runtime import (
    DesktopRuntime,
    _NodeJudgementRuntimeState,
    _RuntimeMonitoringLease,
    _RuntimeRecoveryArm,
)
from runtime_egress_recovery import (
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    runtime_process_subkey,
)
from runtime_post_dial import (
    DEFAULT_POST_DIAL_CONSTANTS,
    OriginRules,
    PostDialObserverCore,
    PostDialTokenizer,
    parse_rfc3339,
    system_rule_log_form,
)
from runtime_post_dial_observer import (
    OBSERVER_THREAD_NAME,
    PostDialObserverRunner,
    PostDialRouteContext,
    PostDialTap,
    format_observer_record,
)


def _load_replay():
    path = Path(__file__).resolve().parent / "fixtures" / "post_dial" / "replay.py"
    spec = importlib.util.spec_from_file_location("post_dial_replay", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay()

PID = 4312
PROCESS_GENERATION = 7
MEMBERS = ("n1", "n2", "n3")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_TIME_FIELD = re.compile(r'time="([^"]+)"')


# ---------------------------------------------------------------------------
# helpers


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, prefix: str = "Post-dial observer") -> list[str]:
        return [
            record.getMessage()
            for record in self.records
            if record.getMessage().startswith(prefix)
        ]


class _FakeController:
    """``MihomoControllerKeepAlive`` stand-in: counts calls and their threads."""

    def __init__(self, snapshot: Callable[[float], dict[str, Any]] | None = None, *, cost: float = 0.0) -> None:
        self._snapshot = snapshot or (lambda _t: {"connections": []})
        self._cost = float(cost)
        self.calls = 0
        self.threads: list[str] = []
        self.closed = 0

    def connections(self, *, timeout_seconds: float) -> dict[str, Any]:
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        if self._cost:
            time.sleep(self._cost)
        return self._snapshot(time.time())

    def close(self) -> None:
        self.closed += 1


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _observer_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == OBSERVER_THREAD_NAME and thread.is_alive()
    ]


@pytest.fixture(autouse=True)
def _no_observer_thread_leak():
    before = set(_observer_threads())
    yield
    leaked = [thread for thread in _observer_threads() if thread not in before]
    for thread in leaked:
        thread.join(timeout=2.0)
    assert not [thread.name for thread in leaked if thread.is_alive()]


def _snapshot(members=MEMBERS, *, core_generation: int = 3) -> CoreLoadedRouteSnapshot:
    return CoreLoadedRouteSnapshot(
        process_pid=PID,
        process_generation=PROCESS_GENERATION,
        core_generation=core_generation,
        semantic_digest="post-dial-observer-test",
        route_epoch=1,
        group_states=(
            CoreLoadedGroupState(
                name="TIER3-AUTO",
                type_name="Fallback",
                members=(*members, "REJECT"),
                now=members[0],
                test_url="https://www.google.com/generate_204",
                expected_status="204",
            ),
        ),
    )


# The node-judgement keys flipped on by default on 10-28, pinned to their
# legacy (pre-ship) values: these tests were written against shadow mode and
# the ladder / rearm / lease keys off.  Any value a test sets wins.
_LEGACY_JUDGEMENT_DEFAULTS = {
    "post_dial_judge": "shadow",
    "rearm_on_stale_route": False,
    "tier_ladder_judge": False,
    "overshoot_repair": False,
}


def _with_legacy_judgement_defaults(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config if config is not None else {}
    recovery = config.setdefault("client", {}).setdefault("egress_recovery", {})
    block = recovery.setdefault("node_judgement", {})
    for key, value in _LEGACY_JUDGEMENT_DEFAULTS.items():
        block.setdefault(key, value)
    recovery.setdefault("route_lease", {}).setdefault("enabled", False)
    return config


def _narrow_runtime(
    config: dict[str, Any] | None = None,
    *,
    controller: Any = None,
    members=MEMBERS,
    install_observer: bool = True,
):
    """A monitoring-active runtime without ``DesktopRuntime.__init__``."""

    runtime = object.__new__(DesktopRuntime)
    runtime.config = _with_legacy_judgement_defaults(config)
    runtime.stop_event = threading.Event()
    logger = logging.getLogger(f"test.post-dial-observer.{id(runtime)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _ListHandler()
    logger.handlers[:] = [handler]
    runtime.logger = logger
    runtime._runtime_generation = 4
    runtime._core_generation = 3
    runtime._lifecycle_revision = 9
    runtime._runtime_initial_activation_generation = 3
    runtime._runtime_selector_intent_revision = 2
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    lease = _RuntimeMonitoringLease(4, 3, PID, PROCESS_GENERATION, 9, time.monotonic() - 1)
    runtime._runtime_monitoring_lease = lease
    runtime.core_runner = SimpleNamespace(
        is_running=lambda: True,
        status=lambda: {"pid": PID, "process_generation": PROCESS_GENERATION},
    )
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_route_snapshot = _snapshot(members)
    runtime._runtime_route_snapshot_payload = {}
    runtime._runtime_local_listener_fault_from_core_output = Mock(return_value=None)
    runtime._runtime_network_context_epoch = 0
    if install_observer:
        runtime._runtime_post_dial_observer_lock = threading.Lock()
        runtime._runtime_post_dial_observer = None
        runtime._runtime_post_dial_observer_lease = None
        runtime._runtime_post_dial_tap = None
    if controller is not None:
        runtime._runtime_post_dial_controller = lambda: controller
    return runtime, lease, handler


def _open_line(sport: int, *, host: str = "zombie.test", leaf: str = "n1", rule: str | None = "Match",
               outer: str = "GENERAL", t: float | None = None) -> str:
    return replay.open_line(time.time() if t is None else t, sport=sport, host=host, leaf=leaf, rule=rule, outer=outer)


def _event(raw: str, *, pid: int = PID, process_generation: int = PROCESS_GENERATION) -> CoreOutputEvent:
    return CoreOutputEvent(
        raw_line=raw,
        pid=pid,
        process_generation=process_generation,
        received_at_monotonic=time.monotonic(),
    )


def _dial_error_line() -> str:
    return (
        'level=warning msg="[TCP] dial FINAL-EXIT (match Match/) '
        '127.0.0.1:55001 --> example.invalid:443 error: context deadline exceeded"'
    )


def _dormant(runner: PostDialObserverRunner) -> bool:
    tap = runner.tap
    return bool(tap._wake_armed and not runner._active)


# ---------------------------------------------------------------------------
# P1-1: the reader-thread tap


def test_reader_appends_only_raw_info_open_lines_and_parses_nothing(monkeypatch) -> None:
    runtime, _lease, _handler = _narrow_runtime()
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    runtime._runtime_post_dial_tap = tap
    parsed: list[str] = []

    def forbidden(*_args, **_kwargs):
        parsed.append(threading.current_thread().name)
        raise AssertionError("the reader thread must not parse")

    monkeypatch.setattr(runtime_post_dial, "parse_open_line", forbidden)
    monkeypatch.setattr(runtime_post_dial_observer, "parse_open_line", forbidden)
    user_open = _open_line(50001)
    udp_open = user_open.replace("[TCP]", "[UDP]")
    debug_open = user_open.replace("level=info", "level=debug")
    lines = [user_open, udp_open, debug_open, _dial_error_line(), 'level=info msg="[DNS] cached"']
    events = [_event(raw) for raw in lines]
    for event in events:
        runtime._on_core_output_event(event)
    assert parsed == []
    # Exactly the one INFO [TCP] open, byte for byte, with its receive time.
    assert tap.drain() == [(events[0].received_at_monotonic, user_open)]
    # The dial-error line still takes today's path to the coordinator.
    ingress = runtime._runtime_egress_coordinator.next_ingress()
    assert ingress is not None and ingress.event is events[3]
    # Another process's lines never enter the tap.
    runtime._on_core_output_event(_event(user_open, pid=PID + 1))
    runtime._on_core_output_event(_event(user_open, process_generation=PROCESS_GENERATION + 1))
    assert tap.drain() == []


def test_reader_path_with_the_tap_sustains_20k_lines_per_second() -> None:
    runtime, _lease, _handler = _narrow_runtime()
    # Isolate the reader callback from today's per-line gates (measured
    # elsewhere): only the P1-1 filter and the append remain variable.
    runtime._runtime_monitoring_is_active = lambda: True
    runtime._runtime_local_listener_fault_from_core_output = lambda *_a, **_k: None
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    runtime._runtime_post_dial_tap = tap
    events = [_event(_open_line(40000 + index % 20000)) for index in range(2000)]
    trials = []
    for _trial in range(3):
        # This thread's CPU time, best of three: other processes on a loaded
        # host inflate wall time, not the reader's own cost per line.
        started = time.thread_time()
        for _round in range(10):
            for event in events:
                runtime._on_core_output_event(event)
        trials.append(time.thread_time() - started)
    assert tap.offered == 60_000 and len(tap.drain()) == 512
    assert tap.overflow == 60_000 - 512
    assert min(trials) < 1.0, f"20k lines took {min(trials):.3f} s of CPU at best"


def test_tap_is_bounded_to_512_lines_and_counts_what_it_dropped() -> None:
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    assert tap.maxlen == DEFAULT_POST_DIAL_CONSTANTS.tap_maxlen == 512
    for index in range(600):
        assert tap.offer(SimpleNamespace(pid=PID, process_generation=PROCESS_GENERATION,
                                         received_at_monotonic=float(index), raw_line=f"line-{index}"))
    drained = tap.drain()
    assert len(drained) == 512
    assert drained[0] == (88.0, "line-88") and drained[-1] == (599.0, "line-599")
    assert tap.overflow == 88 and tap.offered == 600


class _CountingEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.sets = 0

    def set(self) -> None:
        self.sets += 1
        super().set()


def test_tap_sets_its_event_only_on_the_dormant_to_active_transition() -> None:
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    tap._event = _CountingEvent()
    line = SimpleNamespace(pid=PID, process_generation=PROCESS_GENERATION, received_at_monotonic=1.0, raw_line="x")
    for _ in range(3):
        tap.offer(line)
    assert tap.event.sets == 0  # active observer: no Event traffic per line
    # Lines already queued: arming reports them instead of sleeping.
    assert tap.arm_wake() is True
    tap.drain()
    assert tap.arm_wake() is False
    for _ in range(5):
        tap.offer(line)
    assert tap.event.sets == 1 and tap.event.is_set()
    tap.close()
    assert tap.offer(line) is False and tap.drain() == []


def test_no_line_is_appended_once_the_observer_has_stopped() -> None:
    controller = _FakeController()
    runtime, lease, _handler = _narrow_runtime(controller=controller)
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    tap = runtime._runtime_post_dial_tap
    assert tap is runner.tap and tap.alive
    assert runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert runner.join(2.0)
    assert runtime._runtime_post_dial_tap is None
    offered = tap.offered
    runtime._on_core_output_event(_event(_open_line(50002)))
    assert tap.offered == offered and tap.drain() == []
    assert not tap.alive
    assert controller.calls == 0


# ---------------------------------------------------------------------------
# P1-3: lifecycle


def _arm_for_commit(runtime, lease) -> None:
    snapshot = runtime._runtime_route_snapshot
    runtime._runtime_monitoring_lease = None
    runtime._runtime_initial_activation_complete = Mock(return_value=True)
    runtime._runtime_recovery_is_armed = Mock(return_value=True)
    runtime._runtime_worker_token = lambda: (4, 1, 3, PID, PROCESS_GENERATION, 0)
    runtime._runtime_recovery_arm = _RuntimeRecoveryArm(
        worker_token=runtime._runtime_worker_token(), snapshot_key=snapshot.snapshot_key,
        binding_key=("tier3", "tier3b", "n1-id"), route_epoch=snapshot.route_epoch,
        facility_epoch=0, selector_intent_revision=2, lifecycle_revision=9,
        armed_at_monotonic=time.monotonic(),
    )


def test_observer_starts_when_the_monitoring_lease_commits_and_only_once() -> None:
    controller = _FakeController()
    runtime, lease, handler = _narrow_runtime(controller=controller)
    _arm_for_commit(runtime, lease)
    before = set(_observer_threads())
    assert runtime._commit_runtime_monitoring_lease()
    runner = runtime._runtime_post_dial_observer
    committed = runtime._runtime_monitoring_lease
    assert runner is not None and runtime._runtime_post_dial_observer_lease is committed
    assert runtime._runtime_post_dial_tap is runner.tap
    assert _wait_for(lambda: _dormant(runner))
    # A re-arm of the same lease keeps the same observer and thread.
    assert runtime._commit_runtime_monitoring_lease()
    assert runtime._runtime_post_dial_observer is runner
    new_threads = [thread for thread in _observer_threads() if thread not in before]
    assert new_threads == [runner.thread]
    assert "Post-dial observer observer_started (mode=shadow)" in handler.messages()
    runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert not runner.thread.is_alive()
    assert controller.calls == 0


@pytest.mark.parametrize(
    "config",
    [
        {"client": {"egress_recovery": {"node_judgement": {"enabled": False}}}},
        {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": "off"}}}},
        {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": False}}}},
    ],
    ids=["enabled-false", "judge-off", "judge-bare-off"],
)
def test_no_observer_and_unchanged_reader_path_when_the_flag_is_off(config) -> None:
    controller = _FakeController()
    runtime, lease, _handler = _narrow_runtime(config, controller=controller)
    _arm_for_commit(runtime, lease)
    before = set(_observer_threads())
    assert runtime._commit_runtime_monitoring_lease()
    assert runtime._runtime_post_dial_observer is None
    assert runtime._runtime_post_dial_tap is None
    assert [thread for thread in _observer_threads() if thread not in before] == []
    # The reader path is today's: opens are dropped, dial errors are enqueued.
    runtime._on_core_output_event(_event(_open_line(50003)))
    assert runtime._runtime_egress_coordinator.next_ingress() is None
    error = _event(_dial_error_line())
    runtime._on_core_output_event(error)
    assert runtime._runtime_egress_coordinator.next_ingress().event is error
    assert controller.calls == 0


def test_narrow_embedders_without_the_observer_slot_never_start_one() -> None:
    runtime, lease, _handler = _narrow_runtime(install_observer=False)
    _arm_for_commit(runtime, lease)
    assert runtime._commit_runtime_monitoring_lease()
    assert "_runtime_post_dial_observer" not in runtime.__dict__
    runtime._on_core_output_event(_event(_open_line(50004)))
    assert not runtime._stop_runtime_post_dial_observer("test")


def test_a_new_lease_replaces_the_observer_of_the_old_one() -> None:
    controller = _FakeController()
    runtime, lease, _handler = _narrow_runtime(controller=controller)
    assert runtime._ensure_runtime_post_dial_observer(lease)
    first = runtime._runtime_post_dial_observer
    first_tap = first.tap
    # A new Core generation commits a new lease.
    runtime._core_generation = 4
    runtime._runtime_initial_activation_generation = 4
    new_lease = _RuntimeMonitoringLease(4, 4, PID + 1, 1, 9, time.monotonic())
    runtime._runtime_monitoring_lease = new_lease
    runtime.core_runner = SimpleNamespace(
        is_running=lambda: True, status=lambda: {"pid": PID + 1, "process_generation": 1},
    )
    assert runtime._ensure_runtime_post_dial_observer(new_lease)
    second = runtime._runtime_post_dial_observer
    assert second is not first
    assert first.join(2.0) and first.stop_reason == "lease_replaced"
    assert not first_tap.alive
    assert runtime._runtime_post_dial_tap is second.tap
    # Only the new process's lines reach the new tap.
    runtime._on_core_output_event(_event(_open_line(50005), pid=PID + 1, process_generation=1))
    assert second.tap.offered == 1
    runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert second.join(2.0)


def test_a_late_ensure_for_an_older_lease_leaves_the_current_observer_alone() -> None:
    # Commits run on several worker threads and call _ensure after they drop
    # the monitor lock: a late call for a superseded lease must not replace
    # the committed lease's observer.
    controller = _FakeController()
    runtime, old_lease, _handler = _narrow_runtime(controller=controller)
    runtime._lifecycle_revision = 10
    lease = _RuntimeMonitoringLease(4, 3, PID, PROCESS_GENERATION, 10, time.monotonic())
    runtime._runtime_monitoring_lease = lease
    before = set(_observer_threads())
    assert runtime._ensure_runtime_post_dial_observer(lease)
    current = runtime._runtime_post_dial_observer
    assert _wait_for(lambda: _dormant(current))
    assert not runtime._ensure_runtime_post_dial_observer(old_lease)
    assert runtime._runtime_post_dial_observer is current
    assert runtime._runtime_post_dial_observer_lease is lease
    assert runtime._runtime_post_dial_tap is current.tap
    assert current.is_alive() and current.stop_reason == ""
    assert [thread for thread in _observer_threads() if thread not in before] == [current.thread]
    # An uncommitted lease never gets an observer either.
    runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert current.join(2.0)
    runtime._runtime_monitoring_lease = None
    assert not runtime._ensure_runtime_post_dial_observer(lease)
    assert runtime._runtime_post_dial_observer is None
    assert controller.calls == 0


def test_observer_exits_when_its_lease_is_revoked() -> None:
    controller = _FakeController()
    runtime, lease, handler = _narrow_runtime(controller=controller)
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    assert _wait_for(lambda: _dormant(runner))
    runtime._core_generation = 99  # the Core was replaced
    runner.tap.offer(_event(_open_line(50006)))  # wakes the dormant observer
    assert runner.join(2.0)
    assert runner.stop_reason == "lease_revoked"
    assert controller.calls == 0
    assert any("observer_stopped (reason=lease_revoked" in message for message in handler.messages())
    runtime._stop_runtime_post_dial_observer("cleanup", join_timeout=1.0)


def test_origin_rules_are_built_on_the_observer_thread_not_on_the_ready_path() -> None:
    runtime, lease, _handler = _narrow_runtime(controller=_FakeController())
    built: list[str] = []
    real = runtime._runtime_post_dial_origin_rules

    def recording():
        built.append(threading.current_thread().name)
        return real()

    runtime._runtime_post_dial_origin_rules = recording
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    assert _wait_for(lambda: _dormant(runner))
    assert built == [OBSERVER_THREAD_NAME]
    runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert runner.join(2.0)

    failing, lease2, handler = _narrow_runtime(controller=_FakeController())
    failing._runtime_post_dial_origin_rules = Mock(side_effect=RuntimeError("config"))
    assert failing._ensure_runtime_post_dial_observer(lease2)
    broken = failing._runtime_post_dial_observer
    assert broken.join(2.0) and broken.stop_reason == "origin_rules_unavailable"
    failing._on_core_output_event(_event(_open_line(50009)))
    assert broken.tap.offered == 0
    failing._stop_runtime_post_dial_observer("cleanup")


def test_an_observer_without_a_controller_is_not_restarted_for_the_same_lease() -> None:
    runtime, lease, handler = _narrow_runtime()
    runtime.api_client = None  # external controller disabled
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    runtime._on_core_output_event(_event(_open_line(50007)))  # a user open wants a poll
    assert runner.join(2.0)
    assert runner.stop_reason == "controller_unavailable"
    # Re-arm ticks commit the same lease again: no new thread per tick.
    assert not runtime._ensure_runtime_post_dial_observer(lease)
    assert runtime._runtime_post_dial_observer is runner
    assert any("observer_stopped (reason=controller_unavailable" in m for m in handler.messages())
    runtime._stop_runtime_post_dial_observer("cleanup")


def test_an_observer_disabled_by_a_config_reload_restarts_when_re_enabled() -> None:
    controller = _FakeController()
    runtime, lease, _handler = _narrow_runtime(controller=controller)
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    assert _wait_for(lambda: _dormant(runner))
    runtime.config = {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": "off"}}}}
    runner.tap.offer(_event(_open_line(50008)))  # the next wake sees the reload
    assert runner.join(2.0) and runner.stop_reason == "disabled"
    runtime.config = {}
    assert runtime._ensure_runtime_post_dial_observer(lease)
    second = runtime._runtime_post_dial_observer
    assert second is not runner and second.is_alive()
    runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert second.join(2.0)
    assert controller.calls == 0


def _real_runtime(temp_path: Path) -> DesktopRuntime:
    config = {
        "client": {"runtime_state_path": str(temp_path / "runtime-state.json")},
        "alerts": {"log_file": str(temp_path / "monitor.log"), "desktop": {"enabled": False}},
    }
    runtime = DesktopRuntime(config, project_root=temp_path)
    for handler in list(runtime.logger.handlers):
        handler.close()
        runtime.logger.removeHandler(handler)
    runtime.logger = logging.getLogger("test.post-dial-observer.real")
    runtime.logger.handlers[:] = [logging.NullHandler()]
    return runtime


def _start_real_observer(runtime: DesktopRuntime, controller: _FakeController) -> PostDialObserverRunner:
    lease = _RuntimeMonitoringLease(
        int(runtime._runtime_generation), int(runtime._core_generation), PID,
        PROCESS_GENERATION, int(runtime._lifecycle_revision), time.monotonic(),
    )
    runtime._runtime_monitoring_lease = lease
    runtime._runtime_post_dial_controller = lambda: controller
    # A live Core (``__init__`` leaves the phase at ``stopped``); set directly
    # so no route-snapshot refresh is queued.
    runtime._core_runtime_phase = "formal_capture"
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    assert runner is not None and runner.is_alive()
    return runner


def test_monitor_reset_stops_and_joins_the_observer() -> None:
    controller = _FakeController()
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime = _real_runtime(Path(temp_dir))
        runner = _start_real_observer(runtime, controller)
        lease = runtime._runtime_post_dial_observer_lease
        before = set(_observer_threads())
        real_stop = runtime._stop_runtime_post_dial_observer
        raced: list[bool] = []

        def stop_then_race(reason, **kwargs):
            stopped = real_stop(reason, **kwargs)
            if reason == "monitor_reset":
                # A commit of this lease on another thread reaches _ensure
                # right behind the reset's stop.
                raced.append(runtime._ensure_runtime_post_dial_observer(lease))
            return stopped

        runtime._stop_runtime_post_dial_observer = stop_then_race
        runtime._reset_runtime_failure_monitor_state()
        assert raced == [False]
        assert not runner.thread.is_alive()
        assert runner.stop_reason == "monitor_reset"
        assert runtime._runtime_post_dial_observer is None
        assert runtime._runtime_post_dial_tap is None
        assert not runtime._ensure_runtime_post_dial_observer(lease)
        assert [thread for thread in _observer_threads() if thread not in before] == []
    assert controller.calls == 0


@pytest.mark.parametrize("phase", ["stopping", "stopped"])
def test_core_stop_and_restart_phases_stop_the_observer(phase) -> None:
    controller = _FakeController()
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime = _real_runtime(Path(temp_dir))
        runner = _start_real_observer(runtime, controller)
        lease = runtime._runtime_post_dial_observer_lease
        runtime._set_core_runtime_phase(phase)
        assert runner.join(2.0)
        assert runner.stop_reason == f"core_{phase}"
        assert runtime._runtime_post_dial_tap is None
        # The lease is still the committed one while the Core stops, but a
        # commit that passed its armed check before the disarm and reaches
        # _ensure now must not start an observer behind the stop signal.
        assert runtime._runtime_post_dial_observer_is_current(lease)
        before = set(_observer_threads())
        assert not runtime._ensure_runtime_post_dial_observer(lease)
        assert runtime._runtime_post_dial_observer is None
        assert runtime._runtime_post_dial_tap is None
        assert [thread for thread in _observer_threads() if thread not in before] == []
    assert controller.calls == 0


def test_real_start_core_ready_starts_the_observer_and_stop_core_ends_it() -> None:
    # Imported here so pytest does not collect the TestCase a second time.
    from system_proxy import ProxySettings
    from tests.test_desktop_runtime_recovery import (
        DesktopRuntimeRecoveryTests,
        FakeLifecycleCoreRunner,
        FakeTrafficMonitor,
        MemoryProxyBackend,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
        runtime = DesktopRuntimeRecoveryTests._runtime(None, temp_path, backend)
        DesktopRuntimeRecoveryTests._install_injected_runtime_authority(runtime)
        runtime.manager = object()
        runtime.running = True
        runtime._desired_running = True
        runtime.system_proxy.enabled = False
        runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
        runtime.core_runner = FakeLifecycleCoreRunner()
        runtime.traffic_monitor = FakeTrafficMonitor()
        controller = _FakeController()
        runtime._runtime_post_dial_controller = lambda: controller
        before = set(_observer_threads())
        runtime.start_core()
        assert runtime._wait_for_tier3_egress_activation(timeout=1.0)
        runner = runtime._runtime_post_dial_observer
        assert runner is not None and runner.is_alive()
        assert runtime._runtime_post_dial_observer_lease is runtime._runtime_monitoring_lease
        assert [thread for thread in _observer_threads() if thread not in before] == [runner.thread]
        try:
            runtime.stop_core(timeout=2.0)
            assert runner.join(2.0)
            assert runner.stop_reason == "core_stopping"
            assert runtime._runtime_post_dial_observer is None
        finally:
            # This fixture leaves runtime workers spinning after stop_core
            # (with or without the observer); end them so later tests in the
            # same process are not slowed down.
            runtime.stop_event.set()
    assert controller.calls == 0


# ---------------------------------------------------------------------------
# P1-3: dormancy and polling


def test_app_origin_and_non_tier3_opens_schedule_no_controller_call() -> None:
    controller = _FakeController()
    runtime, lease, _handler = _narrow_runtime(controller=controller)
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    lines = [
        # Private source listener: no ``match`` (Conf-5).
        _open_line(51001, host="203.0.113.7", rule=None, outer="FINAL-EXIT"),
        # A system rule, as Mihomo prints it (Conf-3).
        _open_line(51002, host="www.gstatic.com", rule="Domain(www.gstatic.com)", outer="FINAL-EXIT"),
        # DIRECT and REJECT are never judged.
        _open_line(51003, host="lan.test", leaf=None, outer="DIRECT"),
        _open_line(51004, host="blocked.test", leaf="REJECT", outer="GENERAL"),
        # A user open on a leaf outside TIER3-AUTO.
        _open_line(51005, host="site.test", leaf="tier1-leaf"),
    ]
    for raw in lines:
        runtime._on_core_output_event(_event(raw))
    assert _wait_for(lambda: runner.metrics["lines_drained"] == len(lines) and _dormant(runner))
    time.sleep(0.3)
    assert controller.calls == 0
    assert runner.metrics["lines_rejected"] == 0
    runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
    assert runner.join(2.0)


def _zombie_mihomo(*, sports=(52001, 52002), leaf: str = "n1", t0: float | None = None,
                   life: float = 0.05, host: str = "zombie.test"):
    """Two tunnels that send a ClientHello and die with 0 bytes down."""

    mihomo = replay.FakeMihomo()
    start = time.time() + 0.05 if t0 is None else t0
    lines = []
    for index, sport in enumerate(sports):
        t_open = start + 0.02 * index
        mihomo.add(
            replay.FakeConn(
                conn_id=f"z{sport}", t_open=t_open, sport=sport, host=host, port=443, leaf=leaf,
                t_close=t_open + life, up=replay.step(t_open + 0.01, 517),
            )
        )
        lines.append((t_open, replay.open_line(t_open, sport=sport, host=host, leaf=leaf)))
    return mihomo, lines


def _feed(runtime, lines, *, pid: int = PID, process_generation: int = PROCESS_GENERATION) -> None:
    for t_line, raw in sorted(lines):
        delay = t_line - time.time()
        if delay > 0:
            time.sleep(delay)
        runtime._on_core_output_event(_event(raw, pid=pid, process_generation=process_generation))


def _run_zombie(config=None, *, epoch_change: bool = False, attempts: int = 3):
    """Drive a two-tunnel zombie through the real thread; retry host-load misses."""

    last = None
    for _attempt in range(attempts):
        runtime, lease, handler = _narrow_runtime(config)
        mihomo, lines = _zombie_mihomo(t0=time.time() + 0.3)
        controller = _FakeController(mihomo.snapshot)
        runtime._runtime_post_dial_controller = lambda controller=controller: controller
        assert runtime._ensure_runtime_post_dial_observer(lease)
        runner = runtime._runtime_post_dial_observer
        assert _wait_for(lambda: _dormant(runner))
        if epoch_change:
            runtime._runtime_network_context_epoch_changed_wall = time.time()
            runtime._runtime_network_context_epoch = 1
        _feed(runtime, lines)
        _wait_for(
            lambda: runner.metrics["lines_drained"] == len(lines) and _dormant(runner),
            timeout=4.0,
        )
        runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
        assert runner.join(2.0)
        last = (runtime, runner, controller, handler)
        if epoch_change or any("rule=FAST" in message for message in handler.messages()):
            return last
    return last


def test_shadow_mode_logs_would_trigger_and_the_act_hook_takes_no_action() -> None:
    runtime, runner, controller, handler = _run_zombie()
    messages = handler.messages()
    fast = [message for message in messages if "would_trigger" in message and "rule=FAST " in message]
    assert len(fast) == 1, messages
    tokenizer = runtime._node_judgement_state().tokenizer
    assert f"leaf={tokenizer.leaf('n1')} " in fast[0]
    assert "mode=shadow" in fast[0]
    assert not any(message.startswith("Post-dial observer trigger ") for message in messages)
    assert set(controller.threads) == {OBSERVER_THREAD_NAME}
    assert runner.metrics["poll_errors"] == 0


def test_act_mode_trigger_reaches_the_judge_which_gates_an_unbound_route() -> None:
    # Re-pinned in stage 3: the seam no longer logs ``judge=not_wired``; the
    # act trigger now reaches the judge's gates.  This narrow fixture
    # publishes no FINAL-EXIT route, so the judge stops at its binding gate
    # (logged once) and never probes (the P2 paths are in
    # test_runtime_post_dial_judge.py).
    # Keys off, opt in: exhausted_hold (default True since 11-01) would gate this
    # route-less fixture first.
    config = {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": "act", "exhausted_hold": False}}}}
    runtime, runner, controller, handler = _run_zombie(config)
    messages = handler.messages()
    trigger = [message for message in messages if message.startswith("Post-dial observer trigger ")]
    assert len(trigger) == 1, messages
    assert "rule=FAST" in trigger[0] and "mode=act" in trigger[0]
    assert "judge=not_wired" not in trigger[0]
    gated = [message for message in messages if message.startswith("Post-dial observer gated ")]
    assert len(gated) == 1 and "reason=route_unbound" in gated[0], messages
    assert not any(message.startswith("Post-dial observer probe_") for message in messages)
    assert runtime._node_judgement_state().judge.metrics["judgements"] == 0
    # Only /connections reads; the fake has no other method to call.
    assert controller.calls == runner.metrics["controller_calls"] > 0


class _ClockController:
    """``connections()`` from a FakeMihomo at the fake clock's time."""

    def __init__(self, mihomo, clock: Callable[[], float]) -> None:
        self._mihomo = mihomo
        self._clock = clock
        self.calls = 0

    def connections(self, *, timeout_seconds: float) -> dict[str, Any]:
        self.calls += 1
        return self._mihomo.snapshot(self._clock())

    def close(self) -> None:
        pass


def _drive_dies_mid_browsing(config=None):
    """The pure suite's ``_dies_mid_browsing`` (three answers on n1, then six
    failing retries) through the runtime's own observer runner and trigger
    seam, driven synchronously on a fake clock (no thread)."""

    t0 = 1_790_000_000.0
    runtime, lease, handler = _narrow_runtime(config)
    clock = {"t": t0}
    mihomo, feed = replay.FakeMihomo(), []
    sport = 60000

    def conn(t: float, host: str, life: float, down: int = 0) -> None:
        nonlocal sport
        sport += 1
        mihomo.add(
            replay.FakeConn(
                conn_id=f"m{sport}", t_open=t, sport=sport, host=host, port=443, leaf="n1",
                t_close=t + life, up=replay.step(t + 0.001, 800 if down else 517),
                down=replay.step(t + 0.2, down) if down else (lambda _t: 0),
            )
        )
        feed.append((t + 0.001, replay.open_line(t, sport=sport, host=host, leaf="n1")))

    for index in range(3):
        conn(t0 + 2.0 + index * 0.5, f"ok{index}.example", 1.5, down=4200)
    for index in range(6):
        conn(t0 + 8.0 + index * 0.15, "retry.example", 0.01)
    controller = _ClockController(mihomo, lambda: clock["t"])
    runtime._runtime_post_dial_controller = lambda: controller
    runner = runtime._build_runtime_post_dial_observer(lease)
    runner._wall = runner._perf = runner._monotonic = lambda: clock["t"]
    handed: list[Any] = []
    seam = runner._on_trigger

    def on_trigger(trigger, mode):
        handed.append(trigger)
        seam(trigger, mode)

    runner._on_trigger = on_trigger
    assert runner._read_mode()
    mode = runner._mode_value[0]
    assert runner._load_origin_rules()
    core = runner.core
    index, t, t_end = 0, t0, t0 + 12.0
    while t <= t_end:
        clock["t"] = t
        runner._refresh_context(t)
        batch = []
        while index < len(feed) and feed[index][0] <= t:
            batch.append(feed[index])
            index += 1
        if batch:
            runner._ingest_lines(batch, t, t)
        due = core.next_poll_time(t, t)
        if due is None or due > t:
            next_line = feed[index][0] if index < len(feed) else float("inf")
            target = min(float("inf") if due is None else due, next_line)
            if target > t_end:
                break
            t = target
            continue
        runner._poll(mode)
        t += 0.003
    assert controller.calls > 0
    return runtime, runner, handler, handed, mode


@pytest.mark.parametrize("judge", ["shadow", "act"])
def test_a_variant_only_record_is_logged_but_never_reaches_the_trigger_seam(judge) -> None:
    # The guard is off by default (10-01).  The node that dies mid-browsing
    # triggers at the rules' own time; the later match where the 0.5 guard
    # would first have passed is logged as a variant-only would_trigger.
    # That record is never handed to the trigger seam, so it is never judged
    # and never gets a would_judge: one would_judge (shadow) / one trigger
    # line (act) per episode, exactly as before the variants were logged.
    # Keys off, opt in: exhausted_hold (default True since 11-01) would gate first.
    config = {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": judge, "exhausted_hold": False}}}}
    runtime, runner, handler, handed, mode = _drive_dies_mid_browsing(config)
    assert mode == judge
    assert runner.core.constants.trigger_min_failed_share == 0.0
    messages = handler.messages()
    first_line = "Post-dial observer trigger " if judge == "act" else "Post-dial observer would_trigger "
    fast = [m for m in messages if m.startswith(first_line) and " rule=FAST " in m and "variant_only" not in m]
    marker = [m for m in messages if " rule=FAST " in m and "variant_only=true" in m]
    assert len(fast) == 1, messages
    assert "opens=rules,onset_0_5 " in fast[0] and "share_ok=true " in fast[0]
    assert len(marker) == 1, messages
    assert marker[0].startswith("Post-dial observer would_trigger ")
    assert "opens=share_0_5 " in marker[0] and f"mode={judge})" in marker[0]
    episode = re.search(r" episode=(\d+) ", fast[0]).group(1)
    assert f" episode={episode} " in marker[0]
    would_judge = [m for m in messages if m.startswith("Post-dial observer would_judge ")]
    if judge == "shadow":
        assert len(would_judge) == 1, messages
        assert " rule=FAST " in would_judge[0] and f" episode={episode} " in would_judge[0]
    else:
        assert would_judge == []
        gated = [m for m in messages if m.startswith("Post-dial observer gated ")]
        assert len(gated) == 1 and "reason=route_unbound" in gated[0], messages
        assert runtime._node_judgement_state().judge.metrics["judgements"] == 0
    # Only Triggers reach the seam: never the variant-only ShadowTrigger.
    assert handed and all(isinstance(t, runtime_post_dial.Trigger) for t in handed)
    assert all(t.kind == "FAST" for t in handed)
    assert len([t for t in handed if t.new_episode]) == 1
    marker_t = float(re.search(r"\(t=([0-9.]+) ", marker[0]).group(1))
    assert any(abs(t.t - marker_t) < 1e-3 and not t.new_episode for t in handed), (
        "the guard is off: the marked match itself is an ordinary (non-first) Trigger"
    )
    assert runner.core.summary(marker_t + 1.0)["variant_triggers"] == 1


def test_shadow_mode_never_arms_the_not_reproduced_backoff() -> None:
    # 10-28 ship2: the 420 s NOT_REPRODUCED back-off is act bookkeeping only;
    # shadow never finishes a judgement, so its would_judge lines are as before.
    config = {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": "shadow"}}}}
    runtime, runner, handler, handed, mode = _drive_dies_mid_browsing(config)
    assert mode == "shadow"
    judge = runtime._node_judgement_state().judge
    assert judge.constants.not_reproduced_backoff_s == 420.0
    assert judge.constants.leaf_backoff_s == 10.0
    assert judge.gates._backoff == {} and judge.metrics["judgements"] == 0
    messages = handler.messages()
    would_judge = [m for m in messages if m.startswith("Post-dial observer would_judge ")]
    assert len(would_judge) == 1, messages
    assert "gate=leaf_backoff" not in would_judge[0] and "mode=shadow" in would_judge[0]
    assert not any(m.startswith("Post-dial observer verdict ") for m in messages)


def test_network_epoch_change_discards_evidence_for_the_quiet_window() -> None:
    runtime, runner, controller, handler = _run_zombie(epoch_change=True)
    messages = handler.messages()
    assert not any("rule=FAST" in message for message in messages), messages
    # Both opens were drained, but inside the quiet window they are not
    # evidence, so they never scheduled a poll.
    assert runner.metrics["lines_drained"] == 2
    assert controller.calls == 0
    assert any("Post-dial observer epoch" in message for message in messages)


def test_the_epoch_change_time_is_stored_before_the_epoch_advances() -> None:
    # The observer reads (epoch, time) without the route-snapshot lock, epoch
    # first: the writer must store the time first, or a read between the two
    # stores pairs the new epoch with the previous change's time.
    order: list[str] = []
    watched = {"_runtime_network_context_epoch", "_runtime_network_context_epoch_changed_wall"}

    class _RecordingRuntime(DesktopRuntime):
        def __setattr__(self, name, value):
            if name in watched:
                order.append(name)
            super().__setattr__(name, value)

    with tempfile.TemporaryDirectory() as temp_dir:
        runtime = _real_runtime(Path(temp_dir))
        runtime.__class__ = _RecordingRuntime
        for name in (
            "_disarm_runtime_recovery",
            "_enter_runtime_facility_hold",
            "_queue_runtime_route_snapshot_refresh",
            "_wake_parked_tier3_pool_sync_if_causal_key_changed",
        ):
            setattr(runtime, name, Mock())
        epoch = int(runtime._runtime_network_context_epoch)
        before = time.time()
        assert runtime._advance_runtime_network_context_epoch("test") == epoch + 1
        assert order == [
            "_runtime_network_context_epoch_changed_wall",
            "_runtime_network_context_epoch",
        ]
        read_epoch, changed_at = runtime._runtime_post_dial_network_epoch()
        assert read_epoch == epoch + 1 and changed_at is not None and changed_at >= before


def test_an_epoch_change_without_a_newer_time_starts_the_quiet_window_now() -> None:
    reading: dict[str, tuple[int, float | None]] = {"value": (0, 100.0)}
    core = PostDialObserverCore(tokenizer=PostDialTokenizer(bytes(range(32))), tier3_members=MEMBERS)
    noted: list[float] = []
    core.note_network_epoch = noted.append
    runner = PostDialObserverRunner(
        tap=PostDialTap(pid=PID, process_generation=PROCESS_GENERATION),
        core=core,
        controller_factory=lambda: None,
        is_current=lambda: True,
        mode=lambda: ("shadow", frozenset()),
        route_snapshot=lambda: None,
        route_context_for=lambda _snapshot: None,
        network_epoch=lambda: reading["value"],
        on_trigger=lambda *_args: None,
        emit=lambda *_args: None,
    )
    runner._refresh_context(1000.0)  # first sight: the last change's own time
    assert noted == [100.0]
    reading["value"] = (1, 100.0)  # a torn read: the new epoch, the old time
    runner._refresh_context(1001.0)
    assert noted == [100.0, 1001.0]
    reading["value"] = (1, 1000.5)  # its time lands later: same epoch, nothing new
    runner._refresh_context(1002.0)
    assert noted == [100.0, 1001.0]
    reading["value"] = (2, None)  # an advance that recorded no time
    runner._refresh_context(1003.0)
    assert noted[-1] == 1003.0
    reading["value"] = (3, 1010.0)  # a well-formed change keeps its own time
    runner._refresh_context(1011.0)
    assert noted[-1] == 1010.0


def test_summary_is_logged_while_active_and_carries_cost_and_call_counts() -> None:
    records: list[tuple[str, dict[str, Any]]] = []
    tokenizer = PostDialTokenizer(bytes(range(32)))
    core = PostDialObserverCore(tokenizer=tokenizer, tier3_members=MEMBERS)
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    t_open = time.time()
    mihomo = replay.FakeMihomo()
    # A user download on n1 whose bytes keep changing: the 1 Hz linger stays on.
    mihomo.add(replay.FakeConn(conn_id="dl", t_open=t_open, sport=53001, host="video.test", port=443,
                               leaf="n1", up=replay.step(t_open, 900),
                               down=replay.ramp(t_open + 0.05, t_open + 30.0, 30_000_000)))
    controller = _FakeController(mihomo.snapshot, cost=0.04)
    runner = PostDialObserverRunner(
        tap=tap, core=core, origin_rules=OriginRules(),
        controller_factory=lambda: controller, is_current=lambda: True,
        mode=lambda: ("shadow", frozenset({"FAST", "AGED"})),
        route_snapshot=lambda: None, route_context_for=lambda _s: None,
        network_epoch=lambda: (0, None), on_trigger=lambda *_a: None,
        emit=lambda level, record: records.append((level, dict(record))),
        summary_interval_s=1.0,
    )
    runner.start()
    try:
        tap.offer(_event(replay.open_line(t_open, sport=53001, host="video.test", leaf="n1")))
        assert _wait_for(lambda: any(r["ev"] == "summary" for _l, r in records), timeout=4.0)
    finally:
        runner.stop("test")
        assert runner.join(2.0)
    summary = next(record for _level, record in records if record["ev"] == "summary")
    assert summary["total_controller_calls"] >= 2 and summary["polls"] >= 2
    assert summary["poll_cost_p50_ms"] >= 35.0
    assert summary["poll_total_p50_ms"] >= summary["poll_cost_p50_ms"]
    assert summary["total_tap_offered"] == 1 and summary["total_tap_overflow"] == 0
    assert all(level == "info" for level, record in records if record["ev"] == "summary")


def test_cost_adaptation_counts_the_ingest_pass_not_only_the_request() -> None:
    # A cheap request whose ingest pass is slow (a large /connections body):
    # the fast period must stretch on the whole poll, request plus ingest.
    tokenizer = PostDialTokenizer(bytes(range(32)))
    core = PostDialObserverCore(tokenizer=tokenizer, tier3_members=MEMBERS)
    real_ingest = core.ingest_snapshot

    def slow_ingest(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.08)
        return real_ingest(*args, **kwargs)

    core.ingest_snapshot = slow_ingest  # type: ignore[method-assign]
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    t_open = time.time()
    mihomo = replay.FakeMihomo()
    # An unanswered user open on n1: fast polls for its whole fast window.
    mihomo.add(replay.FakeConn(conn_id="slow", t_open=t_open, sport=53101, host="slow.test", port=443,
                               leaf="n1", up=replay.step(t_open, 517)))
    controller = _FakeController(mihomo.snapshot)
    stamps: list[float] = []
    real_connections = controller.connections

    def connections(*, timeout_seconds: float) -> dict[str, Any]:
        stamps.append(time.perf_counter())
        return real_connections(timeout_seconds=timeout_seconds)

    controller.connections = connections  # type: ignore[method-assign]
    runner = PostDialObserverRunner(
        tap=tap, core=core, origin_rules=OriginRules(),
        controller_factory=lambda: controller, is_current=lambda: True,
        mode=lambda: ("shadow", frozenset({"FAST", "AGED"})),
        route_snapshot=lambda: None, route_context_for=lambda _s: None,
        network_epoch=lambda: (0, None), on_trigger=lambda *_a: None,
        emit=lambda _level, _record: None,
    )
    runner.start()
    try:
        tap.offer(_event(replay.open_line(t_open, sport=53101, host="slow.test", leaf="n1")))
        assert _wait_for(lambda: len(stamps) >= 4, timeout=4.0)
    finally:
        runner.stop("test")
        assert runner.join(2.0)
    gaps = [b - a for a, b in zip(stamps[:4], stamps[1:4])]
    # 3 x the ~80 ms whole-poll cost; the request alone (~0 ms) would keep 100 ms.
    assert min(gaps) >= 0.2, gaps


class _FakeRawController(_FakeController):
    """A controller that also offers ``connections_raw`` (Go-compact bytes)."""

    def __init__(self, snapshot=None, *, bodies: list[bytes] | None = None) -> None:
        super().__init__(snapshot)
        self.bodies = list(bodies or [])
        self.raw_calls = 0

    def connections_raw(self, *, timeout_seconds: float) -> bytes:
        self.raw_calls += 1
        self.threads.append(threading.current_thread().name)
        if self.bodies:
            return self.bodies.pop(0)
        payload = self._snapshot(time.time())
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def test_raw_bodies_feed_the_core_and_a_body_that_is_not_json_is_a_poll_error() -> None:
    tokenizer = PostDialTokenizer(bytes(range(32)))
    core = PostDialObserverCore(tokenizer=tokenizer, tier3_members=MEMBERS)
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    t_open = time.time()
    mihomo = replay.FakeMihomo()
    mihomo.add(replay.FakeConn(conn_id="r1", t_open=t_open, sport=53201, host="raw.test", port=443,
                               leaf="n1", up=replay.step(t_open, 517)))
    controller = _FakeRawController(mihomo.snapshot, bodies=[b"not json", b'["a"]'])
    records: list[dict[str, Any]] = []
    runner = PostDialObserverRunner(
        tap=tap, core=core, origin_rules=OriginRules(),
        controller_factory=lambda: controller, is_current=lambda: True,
        mode=lambda: ("shadow", frozenset({"FAST", "AGED"})),
        route_snapshot=lambda: None, route_context_for=lambda _s: None,
        network_epoch=lambda: (0, None), on_trigger=lambda *_a: None,
        emit=lambda _level, record: records.append(dict(record)),
    )
    runner.start()
    try:
        tap.offer(_event(replay.open_line(t_open, sport=53201, host="raw.test", leaf="n1")))
        assert _wait_for(lambda: controller.raw_calls >= 5, timeout=4.0)
    finally:
        runner.stop("test")
        assert runner.join(2.0)
    assert controller.calls == 0  # connections() is never used when the raw call exists
    assert set(controller.threads) == {OBSERVER_THREAD_NAME}
    assert runner.metrics["poll_errors"] == 2
    assert runner.metrics["raw_polls"] == runner.metrics["controller_calls"]
    summary = next(record for record in records if record["ev"] == "summary")  # the one at stop
    assert summary["snapshots_skipped_controller_error"] == 2
    assert summary["polls"] >= 3
    assert summary["total_raw_polls"] == runner.metrics["raw_polls"]
    assert summary["total_raw_fast_polls"] == core.raw_counts["fast_polls"] >= 3


def test_every_poll_follows_a_fresh_policy_reading_and_off_stops_before_the_next_poll() -> None:
    tokenizer = PostDialTokenizer(bytes(range(32)))
    core = PostDialObserverCore(tokenizer=tokenizer, tier3_members=MEMBERS)
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    t_open = time.time()
    mihomo = replay.FakeMihomo()
    # An unanswered user open on n1: fast polls for its whole fast window.
    mihomo.add(replay.FakeConn(conn_id="m1", t_open=t_open, sport=53301, host="mode.test", port=443,
                               leaf="n1", up=replay.step(t_open, 517)))
    controller = _FakeController(mihomo.snapshot)
    policy = {"value": ("act", frozenset({"FAST"}))}
    events: list[str] = []

    def mode() -> tuple[str, frozenset[str]]:
        events.append("mode")
        return policy["value"]

    real_connections = controller.connections

    def connections(*, timeout_seconds: float) -> dict[str, Any]:
        events.append("poll")
        if events.count("poll") == 3:
            policy["value"] = ("off", frozenset())  # a config reload during the poll
        return real_connections(timeout_seconds=timeout_seconds)

    controller.connections = connections  # type: ignore[method-assign]
    runner = PostDialObserverRunner(
        tap=tap, core=core, origin_rules=OriginRules(),
        controller_factory=lambda: controller, is_current=lambda: True,
        mode=mode,
        route_snapshot=lambda: None, route_context_for=lambda _s: None,
        network_epoch=lambda: (0, None), on_trigger=lambda *_a: None,
        emit=lambda _level, _record: None,
    )
    runner.start()
    try:
        tap.offer(_event(replay.open_line(t_open, sport=53301, host="mode.test", leaf="n1")))
        assert _wait_for(lambda: runner.thread is not None and not runner.thread.is_alive(), timeout=4.0)
    finally:
        runner.stop("test")
        assert runner.join(2.0)
    assert runner.stop_reason == "disabled"
    polls = [index for index, event in enumerate(events) if event == "poll"]
    assert len(polls) == 3, events
    for start, end in zip([-1, *polls], polls):
        assert "mode" in events[start + 1 : end], events
    assert events[-1] == "mode"


# ---------------------------------------------------------------------------
# CPU budget (V4 follow-up) through the real thread


class _FakeCpu:
    """The observer thread's CPU clock: ``share`` of one core of perf time
    (work a stretch cannot cut) plus ``per_poll`` seconds for every
    controller call (charged by ``_StampedController``).  ``fail`` makes the
    next reading raise."""

    def __init__(self, share: float = 0.0, *, per_poll: float = 0.0) -> None:
        self.share = float(share)
        self.per_poll = float(per_poll)
        self.fail = False
        self.calls = 0
        self.threads: set[str] = set()
        self._cpu = 0.0
        self._last: float | None = None

    def charge_poll(self) -> None:
        self._cpu += self.per_poll

    def __call__(self) -> float:
        if self.fail:
            raise OSError("clock gone")
        now = time.perf_counter()
        if self._last is not None:
            self._cpu += (now - self._last) * self.share
        self._last = now
        self.calls += 1
        self.threads.add(threading.current_thread().name)
        return self._cpu


class _StampedController(_FakeController):
    def __init__(self, snapshot: Callable[[float], dict[str, Any]], cpu: _FakeCpu | None = None) -> None:
        super().__init__(snapshot)
        self.stamps: list[float] = []
        self.cpu = cpu

    def connections(self, *, timeout_seconds: float) -> dict[str, Any]:
        self.stamps.append(time.perf_counter())
        if self.cpu is not None:
            self.cpu.charge_poll()
        return super().connections(timeout_seconds=timeout_seconds)


def _budget_runner(cpu: Any, controller: Any, records: list, **changes: Any):
    # A 1 s window, a 0.5 s settle time and a 1.5 s hold keep these runs short.
    changes = {"cpu_window_s": 1.0, "cpu_settle_s": 0.5, "cpu_hold_s": 1.5, **changes}
    constants = runtime_post_dial.constants_with(**changes)
    core = PostDialObserverCore(
        tokenizer=PostDialTokenizer(bytes(range(32))), constants=constants, tier3_members=MEMBERS,
    )
    tap = PostDialTap(pid=PID, process_generation=PROCESS_GENERATION)
    runner = PostDialObserverRunner(
        tap=tap, core=core, origin_rules=OriginRules(),
        controller_factory=lambda: controller, is_current=lambda: True,
        mode=lambda: ("shadow", frozenset({"FAST", "AGED"})),
        route_snapshot=lambda: None, route_context_for=lambda _s: None,
        network_epoch=lambda: (0, None), on_trigger=lambda *_a: None,
        emit=lambda level, record: records.append((level, dict(record))),
        cpu_clock=cpu,
    )
    return runner, tap


def _offer_pending_open(
    mihomo: Any,
    tap: PostDialTap,
    sport: int,
    *,
    up: int = 517,
    down_at: float | None = None,
) -> None:
    """One TIER3 user open on n1: its fast window, and its AGED poll unless
    answered ``down_at`` s after the open."""

    t = time.time()
    mihomo.add(replay.FakeConn(conn_id=f"b{sport}", t_open=t, sport=sport, host="budget.test", port=443,
                               leaf="n1", up=replay.step(t, up) if up else (lambda _t: 0),
                               down=replay.step(t + down_at, 4000) if down_at is not None else (lambda _t: 0)))
    tap.offer(_event(replay.open_line(t, sport=sport, host="budget.test", leaf="n1")))


def _gaps(stamps: list[float]) -> list[float]:
    return [b - a for a, b in zip(stamps, stamps[1:])]


def _feeding(mihomo: Any, tap: PostDialTap, first_sport: int) -> tuple[threading.Event, threading.Thread]:
    """A new open answered after 0.5 s every 0.3 s: the fast window stays open."""

    feeding = threading.Event()
    feeding.set()

    def feed() -> None:
        sport = first_sport
        while feeding.is_set():
            _offer_pending_open(mihomo, tap, sport, down_at=0.5)
            sport += 1
            time.sleep(0.3)

    return feeding, threading.Thread(target=feed, daemon=True)


def test_cpu_budget_stretches_the_polls_above_the_budget_and_releases_them_below() -> None:
    records: list[tuple[str, dict[str, Any]]] = []
    mihomo = replay.FakeMihomo()
    cpu = _FakeCpu(per_poll=0.02)  # 20 ms a poll: 20 % of a core at 10 Hz, ten times the budget
    controller = _StampedController(mihomo.snapshot, cpu)
    runner, tap = _budget_runner(cpu, controller, records, cpu_scale_max=4.0)
    feeding, feeder = _feeding(mihomo, tap, 54000)
    runner.start()
    feeder.start()
    try:
        assert _wait_for(lambda: any(r["ev"] == "observer_throttled" for _l, r in records), timeout=4.0)
        engaged = len(controller.stamps)
        assert _wait_for(lambda: len(controller.stamps) >= engaged + 6, timeout=6.0)
        hot = controller.stamps[engaged:]
        cpu.per_poll = 0.0  # the load is gone
        assert _wait_for(lambda: runner._scale == 1.0, timeout=5.0)
        released = len(controller.stamps)
        assert _wait_for(lambda: len(controller.stamps) >= released + 9, timeout=4.0)
        cool = controller.stamps[released + 1:]
    finally:
        feeding.clear()
        feeder.join(2.0)
        runner.stop("test")
        assert runner.join(2.0)

    assert min(_gaps(hot)) >= 0.4 - 0.01  # 4 x the 100 ms fast period
    cool_gaps = sorted(_gaps(cool))
    assert cool_gaps[len(cool_gaps) // 2] <= 0.2  # back to 100 ms
    throttled = [(level, record) for level, record in records if record["ev"] == "observer_throttled"]
    assert len(throttled) == 1  # one record per episode (and per summary interval)
    level, record = throttled[0]
    assert level == "info"
    assert record["budget_pct"] == 2.0 and record["measured_pct"] >= 10.0 and record["poll_pct"] >= 10.0
    assert record["scale"] == 4.0 and record["period_ms"] == 400.0 and record["slow_period_ms"] == 4000.0
    text = format_observer_record(record)
    assert "budget.test" not in text and "n1" not in text
    # 4 x cannot meet 2 % at 20 ms a poll: said once, numbers only.
    unreachable = [record for _level, record in records if record["ev"] == "observer_budget_unreachable"]
    assert len(unreachable) == 1 and unreachable[0]["scale"] == 4.0
    summary = next(record for _level, record in records if record["ev"] == "summary")  # the one at stop
    assert summary["total_throttle_episodes"] == 1 and summary["throttle_episodes"] == 1
    assert summary["throttle_unreachable"] >= 1 and summary["throttle_changes"] >= 2
    assert summary["total_throttled_polls"] >= 6
    assert summary["throttle_scale_peak"] == 4.0 and summary["throttle_scale"] == 1.0
    assert summary["cpu_budget_pct"] == 2.0 and summary["observer_cpu_pct"] > 0.0
    assert 0.0 < summary["observer_poll_cpu_pct"] <= summary["observer_cpu_pct"]
    assert cpu.threads == {OBSERVER_THREAD_NAME}


def test_cpu_the_stretch_cannot_cut_does_not_stretch_the_polls() -> None:
    # 20 % of a core outside the polls (open lines, say) and none in them:
    # stretching would only discard evidence, so the polls keep 100 ms.
    records: list[tuple[str, dict[str, Any]]] = []
    mihomo = replay.FakeMihomo()
    cpu = _FakeCpu(0.20)
    controller = _StampedController(mihomo.snapshot, cpu)
    runner, tap = _budget_runner(cpu, controller, records)
    feeding, feeder = _feeding(mihomo, tap, 54300)
    runner.start()
    feeder.start()
    try:
        assert _wait_for(
            lambda: any(r["ev"] == "observer_budget_unreachable" for _l, r in records), timeout=4.0,
        )
        start = len(controller.stamps)
        assert _wait_for(lambda: len(controller.stamps) >= start + 10, timeout=4.0)
    finally:
        feeding.clear()
        feeder.join(2.0)
        runner.stop("test")
        assert runner.join(2.0)
    assert runner.core.poll_scale == 1.0
    assert not [record for _level, record in records if record["ev"] == "observer_throttled"]
    gaps = sorted(_gaps(controller.stamps[start:]))
    assert gaps[len(gaps) // 2] <= 0.2
    (record,) = [record for _level, record in records if record["ev"] == "observer_budget_unreachable"]
    assert record["other_pct"] >= 10.0 and record["poll_pct"] < 1.0 and record["scale"] == 1.0


def test_a_zero_cpu_budget_never_stretches_the_polls() -> None:
    records: list[tuple[str, dict[str, Any]]] = []
    mihomo = replay.FakeMihomo()
    cpu = _FakeCpu(0.5, per_poll=0.05)
    controller = _StampedController(mihomo.snapshot, cpu)
    runner, tap = _budget_runner(cpu, controller, records, cpu_budget_pct=0.0)
    runner.start()
    try:
        for index in range(6):
            _offer_pending_open(mihomo, tap, 54100 + index)
            time.sleep(0.3)
        assert _wait_for(lambda: len(controller.stamps) >= 15, timeout=4.0)
    finally:
        runner.stop("test")
        assert runner.join(2.0)
    gaps = sorted(_gaps(controller.stamps))
    assert gaps[len(gaps) // 2] <= 0.2
    assert runner._scale == 1.0 and runner.core.poll_scale == 1.0
    assert not [record for _level, record in records if record["ev"].startswith("observer_")
                and record["ev"] not in {"observer_started", "observer_stopped"}]
    summary = next(record for _level, record in records if record["ev"] == "summary")
    assert summary["total_throttle_episodes"] == 0 and summary["total_throttled_polls"] == 0
    assert summary["cpu_budget_pct"] == 0.0
    assert summary["observer_cpu_pct"] >= 30.0  # still measured


def test_a_throttled_observer_still_goes_dormant_and_polls_a_new_open_at_once() -> None:
    records: list[tuple[str, dict[str, Any]]] = []
    mihomo = replay.FakeMihomo()
    cpu = _FakeCpu(per_poll=0.02)
    controller = _StampedController(mihomo.snapshot, cpu)
    runner, tap = _budget_runner(cpu, controller, records, cpu_scale_max=4.0, cpu_hold_s=10.0)
    runner.start()
    try:
        assert _wait_for(lambda: _dormant(runner))
        # No upload: the fast window and the AGED poll, then nothing to linger on.
        _offer_pending_open(mihomo, tap, 54200, up=0)
        assert _wait_for(lambda: runner._scale == 4.0, timeout=3.0)
        engaged = len(controller.stamps)
        assert _wait_for(lambda: controller.calls >= engaged + 2 and _dormant(runner), timeout=6.0)
        calls, readings = controller.calls, cpu.calls
        time.sleep(0.5)
        # Dormant, still stretched: no controller call, no CPU reading, no timer.
        assert (controller.calls, cpu.calls) == (calls, readings)
        assert runner.core.poll_scale == 4.0
        stretched = controller.stamps[engaged - 1:]
        t_open = time.perf_counter()
        _offer_pending_open(mihomo, tap, 54201, up=0)
        assert _wait_for(lambda: controller.calls > calls, timeout=2.0)
        first = controller.stamps[calls]
    finally:
        runner.stop("test")
        assert runner.join(2.0)
    gaps = _gaps(stretched)
    assert min(gaps) >= 0.1 * runtime_post_dial.DEFAULT_POST_DIAL_CONSTANTS.min_poll_scale - 0.01
    assert max(gaps) >= 0.4 - 0.01
    # The stretch never delays the first poll after dormancy.
    assert first - t_open < 0.3
    assert runner.metrics["dormant_waits"] >= 2


@pytest.mark.parametrize("where", ["reading", "poll"])
def test_a_failing_cpu_clock_ends_the_budget_and_undoes_the_stretch(where: str) -> None:
    records: list[tuple[str, dict[str, Any]]] = []
    mihomo = replay.FakeMihomo()
    cpu = _FakeCpu(per_poll=0.02)
    controller = _StampedController(mihomo.snapshot, cpu)
    if where == "poll":
        # The clock fails between the readings around a poll.
        def charge_and_fail() -> None:
            cpu._cpu += cpu.per_poll
            if runner._scale > 1.0:
                cpu.fail = True

        cpu.charge_poll = charge_and_fail  # type: ignore[method-assign]
    runner, tap = _budget_runner(cpu, controller, records, cpu_scale_max=4.0, cpu_hold_s=60.0)
    feeding, feeder = _feeding(mihomo, tap, 54400)
    runner.start()
    feeder.start()
    try:
        assert _wait_for(lambda: runner._scale == 4.0 or cpu.fail, timeout=4.0)
        cpu.fail = True
        assert _wait_for(lambda: runner._scale == 1.0, timeout=2.0)
        readings = cpu.calls
        start = len(controller.stamps)
        assert _wait_for(lambda: len(controller.stamps) >= start + 8, timeout=4.0)
    finally:
        feeding.clear()
        feeder.join(2.0)
        runner.stop("test")
        assert runner.join(2.0)
    assert runner.core.poll_scale == 1.0 and runner._throttle.enabled is False
    assert runner._measure_cpu is False and runner._cpu_clock is None
    assert cpu.calls == readings  # no reading after the failure
    gaps = sorted(_gaps(controller.stamps[start:]))
    assert gaps[len(gaps) // 2] <= 0.2  # 100 ms again


def test_no_reliable_cpu_clock_leaves_the_budget_off(monkeypatch) -> None:
    if sys.platform == "win32":
        # The cycle counter is unavailable: no thread_time fallback (it
        # misreads short bursts), neither now nor on later calls.
        def unavailable() -> Any:
            raise OSError("no cycle counter")

        monkeypatch.setattr(runtime_post_dial_observer, "_CYCLE_QUERY", None)
        monkeypatch.setattr(runtime_post_dial_observer, "_windows_cycle_query", unavailable)
        assert runtime_post_dial_observer.thread_cpu_clock() is None
        assert runtime_post_dial_observer._CYCLE_QUERY is False
        assert runtime_post_dial_observer.thread_cpu_clock() is None
    monkeypatch.setattr(runtime_post_dial_observer, "thread_cpu_clock", lambda: None)
    core = PostDialObserverCore(tokenizer=PostDialTokenizer(bytes(range(32))), tier3_members=MEMBERS)
    runner = PostDialObserverRunner(
        tap=PostDialTap(pid=PID, process_generation=PROCESS_GENERATION),
        core=core, controller_factory=lambda: None, is_current=lambda: True,
        mode=lambda: ("shadow", frozenset()), route_snapshot=lambda: None,
        route_context_for=lambda _s: None, network_epoch=lambda: (0, None),
        on_trigger=lambda *_a: None, emit=lambda *_a: None,
    )
    assert runner._measure_cpu is True and runner._throttle.enabled is True
    runner._note_cpu(0.0, 0.0)
    assert runner._measure_cpu is False and runner._throttle.enabled is False
    assert runner._scale == 1.0 and runner._throttle.summary()["observer_cpu_pct"] is None


@pytest.mark.parametrize(("value", "expected"), [(None, 2.0), (5, 5.0), (0, 0.0), (99, 20.0), ("1%", 1.0)])
def test_the_observer_takes_its_cpu_budget_from_the_node_judgement_policy(value, expected) -> None:
    block = {} if value is None else {"observer_cpu_budget_pct": value}
    config = {"client": {"egress_recovery": {"node_judgement": block}}}
    runtime, lease, _handler = _narrow_runtime(config, controller=_FakeController())
    assert runtime._ensure_runtime_post_dial_observer(lease)
    runner = runtime._runtime_post_dial_observer
    try:
        assert runner.core.constants.cpu_budget_pct == expected
        assert runner._throttle.budget_pct == expected
        assert runner._throttle.enabled is (expected > 0.0)
        assert runner._measure_cpu is True
    finally:
        runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
        assert runner.join(2.0)


def test_a_substituted_perf_clock_without_a_cpu_clock_leaves_the_budget_off() -> None:
    core = PostDialObserverCore(tokenizer=PostDialTokenizer(bytes(range(32))), tier3_members=MEMBERS)
    runner = PostDialObserverRunner(
        tap=PostDialTap(pid=PID, process_generation=PROCESS_GENERATION),
        core=core, controller_factory=lambda: None, is_current=lambda: True,
        mode=lambda: ("shadow", frozenset()), route_snapshot=lambda: None,
        route_context_for=lambda _s: None, network_epoch=lambda: (0, None),
        on_trigger=lambda *_a: None, emit=lambda *_a: None,
        perf=lambda: 0.0,
    )
    assert runner._measure_cpu is False
    runner._note_cpu(0.0, 0.0)
    assert runner._scale == 1.0 and runner._throttle.summary()["observer_cpu_pct"] is None


def test_thread_cpu_clock_counts_the_threads_cpu_and_not_its_sleep() -> None:
    clock = runtime_post_dial_observer.thread_cpu_clock()
    assert clock is not None
    start = clock()
    wall = time.perf_counter()
    while time.perf_counter() - wall < 0.1:
        pass
    busy_wall = time.perf_counter() - wall
    busy = clock() - start
    assert 0.02 <= busy <= busy_wall * 1.1 + 0.002
    before = clock()
    time.sleep(0.1)
    assert 0.0 <= clock() - before < 0.02


# ---------------------------------------------------------------------------
# Replayed fixture through the real thread and the real keep-alive client


class _ConnectionsHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.server.accepted += 1

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.paths.append(self.path)
        self.server.auth.add(self.headers.get("Authorization", ""))
        if self.path != "/connections":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        snapshot = self.server.snapshot(time.time())
        if self.server.go_framing:
            # Mihomo: Go encoding/json compact output plus a newline, one chunk.
            body = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
            return
        body = json.dumps(snapshot).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


class _ConnectionsServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, snapshot: Callable[[float], dict[str, Any]], *, go_framing: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _ConnectionsHandler)
        self.snapshot = snapshot
        self.go_framing = go_framing
        self.paths: list[str] = []
        self.auth: set[str] = set()
        self.accepted = 0


def _shift_rep(rep: dict[str, Any], shift: float) -> dict[str, Any]:
    lines = []
    for receive_ts, raw, schedule, outcome in rep["lines"]:
        stamp = parse_rfc3339(_TIME_FIELD.search(raw).group(1))
        raw = _TIME_FIELD.sub(f'time="{replay.rfc3339(stamp + shift)}"', raw, count=1)
        if schedule is not None:
            schedule = [
                value + shift if index in (0, 1, 2, 4, 5) and value is not None else value
                for index, value in enumerate(schedule)
            ]
        lines.append([receive_ts + shift, raw, schedule, outcome])
    return {**rep, "lines": lines}


def _replay_p1(rep: dict[str, Any], *, go_framing: bool = False):
    t_trigger = rep["proto"]["triggers"][0][0]
    window = (rep["t_fault"] - 1.0, t_trigger + 1.0)
    shift = time.time() + 0.4 - window[0]
    shifted = _shift_rep(rep, shift)
    mihomo, feed = replay.vproto_mihomo(shifted)
    server = _ConnectionsServer(mihomo.snapshot, go_framing=go_framing)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    server_thread.start()
    try:
        runtime, lease, handler = _narrow_runtime(members=tuple(rep["members"]))
        runtime.api_client = SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_address[1]}", secret="observer-secret",
        )
        assert runtime._ensure_runtime_post_dial_observer(lease)
        runner = runtime._runtime_post_dial_observer
        lines = [(ts, raw) for ts, raw in feed if window[0] + shift <= ts <= window[1] + shift]
        assert _wait_for(lambda: _dormant(runner))
        _feed(runtime, lines)
        # Pre-existing streams in the fixture may keep the 1 Hz watch on, so
        # the replay ends at the window end rather than at dormancy.
        _wait_for(lambda: time.time() >= window[1] + shift + 0.3, timeout=6.0)
        runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
        assert runner.join(2.0)
    finally:
        server.shutdown()
        server.server_close()
    return runtime, runner, handler, server, shifted


@pytest.mark.parametrize("go_framing", [False, True], ids=["json-framing", "mihomo-framing"])
def test_replayed_zombie_fixture_logs_a_shadow_fast_trigger_through_the_real_thread(go_framing: bool) -> None:
    rep = replay.load_fixture("vproto_P1.json.gz")["reps"][0]
    for _attempt in range(3):
        runtime, runner, handler, server, shifted = _replay_p1(rep, go_framing=go_framing)
        messages = handler.messages()
        fast = [message for message in messages if "would_trigger" in message and "rule=FAST " in message]
        if fast:
            break
    assert len(fast) == 1, messages
    tokenizer = runtime._node_judgement_state().tokenizer
    assert f"leaf={tokenizer.leaf('n1')} " in fast[0]
    # The keep-alive client: one socket, /connections only, never /proxies.
    assert server.paths and set(server.paths) == {"/connections"}
    assert server.accepted == 1
    assert server.auth == {"Bearer observer-secret"}
    assert runner.metrics["controller_calls"] == len(server.paths)
    assert not any(message.startswith("Post-dial observer trigger ") for message in messages)
    # Every poll took the raw body; Mihomo's framing is decoded entry by
    # entry, any other goes through json.loads.
    assert runner.metrics["raw_polls"] == runner.metrics["controller_calls"] - runner.metrics["poll_errors"]
    counts = runner.core.raw_counts
    if go_framing:
        assert counts["fast_polls"] > 0 and counts["json_polls"] == 0
    else:
        assert counts["fast_polls"] == 0 and counts["json_polls"] > 0

    # Privacy: no raw host, source address or leaf name in any log line.
    hosts = {
        match.group(1).strip("[]")
        for _ts, raw, _schedule, _outcome in shifted["lines"]
        for match in [re.search(r"--> (\S+):\d+ ", raw)]
        if match
    }
    assert hosts
    text = "\n".join(record.getMessage() for record in handler.records)
    for host in hosts:
        assert host not in text
    assert "127.0.0.1" not in text and "observer-secret" not in text
    for leaf in rep["members"]:
        assert f"[{leaf}]" not in text and f"={leaf} " not in text and f"={leaf})" not in text
    for message in messages:
        for value in re.findall(r"\bleaf=(\S+?)[ )]", message):
            assert _HEX12.match(value), message
        for value in re.findall(r"\btoks?=(\S+?)[ )]", message):
            assert all(_HEX12.match(part) for part in value.split(",") if part != "-"), message


# ---------------------------------------------------------------------------
# Tokens, origin rules, record format


def test_tokenizer_key_comes_from_the_per_process_runtime_key() -> None:
    namespace = "node-judgement-tokenizer-v1"
    expected = PostDialTokenizer(runtime_process_subkey(namespace)).leaf("leaf-A")
    assert _NodeJudgementRuntimeState().tokenizer.leaf("leaf-A") == expected
    code = (
        "from runtime_egress_recovery import runtime_process_subkey;"
        "from runtime_post_dial import PostDialTokenizer;"
        f"print(PostDialTokenizer(runtime_process_subkey({namespace!r})).leaf('leaf-A'))"
    )
    root = Path(__file__).resolve().parents[1]
    others = {
        subprocess.run(
            [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True, timeout=60,
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(others) == 2 and expected not in others


def test_origin_rules_match_the_system_rules_the_formatter_renders() -> None:
    config = {
        "testing": {"probe_urls": ["https://probe.example.test/generate_204", "https://198.51.100.9/"]},
        "geo_update": {"geox_url": {"geoip": "https://geo.example.test/geoip.dat"}},
    }
    runtime, _lease, _handler = _narrow_runtime(config)
    rules = runtime._runtime_post_dial_origin_rules()
    urls = runtime._default_probe_urls()
    rendered = yaml.safe_load(
        build_full_clash_config(
            {}, probe_url=urls[0], probe_urls=urls,
            geox_url=config["geo_update"]["geox_url"],
        )
    )
    system = {
        system_rule_log_form(rule)
        for rule in rendered["rules"]
        if rule.split(",")[0] in {"DOMAIN", "IP-CIDR", "IP-CIDR6"} and ",FINAL-EXIT" in rule
    }
    assert system and system == set(rules.system_rule_log_forms)
    assert "Domain(probe.example.test)" in rules.system_rule_log_forms
    assert "IPCIDR(198.51.100.9/32)" in rules.system_rule_log_forms
    assert "Domain(geo.example.test)" in rules.system_rule_log_forms
    assert rules.user_inbound_names == frozenset({"DEFAULT-MIXED"})
    runtime.network_mode = "tun"
    assert runtime._runtime_post_dial_origin_rules().user_inbound_names == frozenset(
        {"DEFAULT-MIXED", "DEFAULT-TUN"}
    )
    # mixed is TUN + system proxy: its TUN inbound is user traffic too
    runtime.network_mode = "mixed"
    assert runtime._runtime_post_dial_origin_rules().user_inbound_names == frozenset(
        {"DEFAULT-MIXED", "DEFAULT-TUN"}
    )


def test_route_context_is_tier3_auto_all_minus_reject() -> None:
    context = DesktopRuntime._runtime_post_dial_route_context(_snapshot(("leaf-alpha", "leaf-beta")))
    assert context == PostDialRouteContext(
        tier3_members=frozenset({"leaf-alpha", "leaf-beta"}), gateway_groups=frozenset({"EXIT-SELECT"}),
    )
    assert "leaf-" not in repr(context)
    empty = CoreLoadedRouteSnapshot(
        process_pid=1, process_generation=1, core_generation=1, semantic_digest="x", route_epoch=1,
    )
    assert DesktopRuntime._runtime_post_dial_route_context(empty).tier3_members == frozenset()


def test_record_format_keeps_only_a_safe_character_set() -> None:
    text = format_observer_record(
        {"ev": "x", "leaf": "abc def\n[evil]", "toks": ["aa", "b b"], "n": 2, "ok": True, "gap": None, "t": 1.23456},
    )
    assert text == "ev=x leaf=abc_def__evil_ toks=aa,b_b n=2 ok=true gap=none t=1.2346"
