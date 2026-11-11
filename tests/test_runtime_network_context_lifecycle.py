from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

import runtime_network_context as context


def snapshot(generation: str) -> context.RuntimeNetworkContextSnapshot:
    return context.RuntimeNetworkContextSnapshot(
        captured_at="", captured_monotonic_ns=time.monotonic_ns(),
        runtime_generation=generation, state="indeterminate", related_processes=(),
        adapter_summary=(), default_route_summary=(), fake_ip_route_summary=(),
        dns_summary=(), system_proxy_enabled=None, system_proxy_digest="", errors=(),
    )


def test_stop_does_not_launch_a_final_diagnostic_capture():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def capture(generation):
        calls.append(generation)
        entered.set()
        assert release.wait(2)
        return snapshot(generation)

    observer = context.RuntimeNetworkContextObserver("stopped", capture=capture)
    observer.start()
    try:
        assert entered.wait(1)
        observer.stop(timeout_seconds=0)
    finally:
        release.set()
        observer.stop(timeout_seconds=1)
    assert calls == ["stopped"]
    assert observer.history() == ()
    assert not observer._thread.is_alive()


def test_stopped_observer_cannot_launch_a_late_sampler():
    calls = []
    observer = context.RuntimeNetworkContextObserver(
        "never-started", capture=lambda generation: calls.append(generation) or snapshot(generation)
    )
    observer.stop()
    observer.start()
    observer.stop()
    assert calls == []


def test_default_sampling_is_single_flight_and_keeps_capture_timestamp():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def command(argv, timeout, **kwargs):
        calls.append(argv)
        entered.set()
        assert release.wait(2)
        return ""

    observers = [context.RuntimeNetworkContextObserver(str(index)) for index in range(8)]
    with patch.object(context, "_DEFAULT_SAMPLER", context._DefaultNetworkContextSampler()), patch.object(context, "_run_readonly_command", side_effect=command), patch.object(
        context, "_read_system_proxy", return_value=(False, "", None)
    ):
        observers[0].start()
        try:
            assert entered.wait(1)
            for observer in observers[1:]:
                observer._sample()
            assert len(calls) == 1
            release.set()
            deadline = time.monotonic() + 1
            while observers[0].latest() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            captured = observers[0].latest()
            assert captured is not None
            for observer in observers[1:]:
                observer._sample()
                assert observer.latest().runtime_generation == observer.runtime_generation
                assert observer.latest().captured_monotonic_ns == captured.captured_monotonic_ns
            assert len(calls) == 3
        finally:
            release.set()
            for observer in observers:
                observer.stop(timeout_seconds=1)


def test_stop_during_capture_does_not_launch_remaining_commands():
    stopped = threading.Event()
    calls = []

    def command(argv, _timeout):
        calls.append(argv)
        stopped.set()
        return ""

    with patch.object(context, "_read_system_proxy", return_value=(False, "", None)):
        context.capture_runtime_network_context(
            "cancelled", command_runner=command, cancel_event=stopped
        )
    assert calls == [("tasklist.exe", "/fo", "csv", "/nh")]


@pytest.mark.parametrize("cancelled", [False, True])
def test_command_timeout_or_cancel_has_bounded_cleanup_without_pipe_readers(cancelled):
    stopped = threading.Event()
    waits = []

    class Child:
        returncode = None
        killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            waits.append(timeout)
            if self.killed:
                self.returncode = -1
                return -1
            if cancelled:
                stopped.set()
            raise context.subprocess.TimeoutExpired("diagnostic", timeout)

        def kill(self):
            self.killed = True

    child = Child()

    def popen(argv, **kwargs):
        assert kwargs["stdout"] != context.subprocess.PIPE
        assert kwargs["stderr"] != context.subprocess.PIPE
        assert kwargs["stdin"] == context.subprocess.DEVNULL
        return child

    ticks = iter(index * 0.05 for index in range(100))
    with patch.object(context.subprocess, "Popen", side_effect=popen), patch.object(
        context.time, "monotonic", side_effect=lambda: next(ticks)
    ), pytest.raises((RuntimeError, context.subprocess.TimeoutExpired)):
        context._run_readonly_command(("route.exe", "print", "-4"), 0.1, cancel_event=stopped)
    assert child.killed
    assert waits and max(waits) <= 0.25
