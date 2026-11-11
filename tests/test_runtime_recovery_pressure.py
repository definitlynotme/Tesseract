from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from runtime_recovery_pressure import (
    MAX_QUIESCE_SECONDS,
    RuntimeRecoveryPressureCoordinator,
    RuntimeRecoveryPressurePriority as Priority,
    RuntimeRecoveryPressureQuiesceState as QuiesceState,
    normalise_recovery_pressure_priority,
)


def test_priority_order_is_fixed_and_lower_work_cannot_enter_under_pressure() -> None:
    coordinator = RuntimeRecoveryPressureCoordinator()

    background = coordinator.acquire("background-1", Priority.BACKGROUND)
    hot = coordinator.acquire("hot-1", Priority.HOT_STANDBY)
    manual = coordinator.acquire("manual-1", Priority.MANUAL)
    activation = coordinator.acquire("activation-1", Priority.ACTIVATION)
    recovery = coordinator.acquire("recovery-1", Priority.RUNTIME_RECOVERY)

    assert all((background, hot, manual, activation, recovery))
    assert coordinator.acquire("late-activation", Priority.ACTIVATION) is None
    snapshot = coordinator.snapshot()
    assert snapshot.highest_priority is Priority.RUNTIME_RECOVERY
    assert snapshot.active_priorities == (
        Priority.BACKGROUND,
        Priority.HOT_STANDBY,
        Priority.MANUAL,
        Priority.ACTIVATION,
        Priority.RUNTIME_RECOVERY,
    )

    assert recovery is not None and recovery.release()
    assert coordinator.acquire("late-hot", Priority.HOT_STANDBY) is None
    assert activation is not None and activation.release()
    assert manual is not None and manual.release()
    assert hot is not None and hot.release()
    assert coordinator.acquire("background-2", Priority.BACKGROUND) is not None


def test_same_owner_acquire_and_lease_release_are_idempotent() -> None:
    callbacks: list[str] = []
    coordinator = RuntimeRecoveryPressureCoordinator(
        quiesce_callback=lambda: callbacks.append("quiesce"),
        resume_callback=lambda: callbacks.append("resume"),
    )

    lease = coordinator.acquire("incident-1", "runtime-recovery")
    duplicate = coordinator.acquire("incident-1", Priority.RUNTIME_RECOVERY)

    assert lease is duplicate
    assert callbacks == ["quiesce"]
    assert lease is not None
    assert lease.release()
    assert lease.release()
    assert coordinator.release(lease)
    assert callbacks == ["quiesce", "resume"]
    assert coordinator.snapshot().active_lease_count == 0

    with coordinator.acquire("activation-2", "cold activation") as contextual:
        assert contextual.owner_id == "activation-2"
        assert not contextual.released
    assert contextual.released


def test_owner_cannot_silently_change_priority() -> None:
    coordinator = RuntimeRecoveryPressureCoordinator()
    assert coordinator.acquire("one-owner", Priority.BACKGROUND) is not None

    with pytest.raises(ValueError, match="cannot change"):
        coordinator.acquire("one-owner", Priority.RUNTIME_RECOVERY)


def test_quiesce_waits_for_drain_but_never_more_than_configured_deadline() -> None:
    drain = threading.Event()
    callback_started = threading.Event()

    def quiesce() -> None:
        callback_started.set()
        drain.wait()

    coordinator = RuntimeRecoveryPressureCoordinator(
        quiesce_callback=quiesce,
        quiesce_timeout_seconds=0.04,
    )
    started = time.monotonic()
    lease = coordinator.acquire("activation", Priority.ACTIVATION)
    elapsed = time.monotonic() - started

    assert callback_started.is_set()
    assert lease is not None
    assert 0.02 <= elapsed < 0.25
    snapshot = coordinator.snapshot()
    assert snapshot.quiesce_state is QuiesceState.TIMED_OUT
    assert snapshot.last_callback_error == "quiesce_callback:deadline_expired"

    drain.set()
    assert lease.release()


def test_completed_drain_is_observed_before_acquire_returns() -> None:
    drain = threading.Event()

    def quiesce() -> None:
        assert drain.wait(0.5)

    coordinator = RuntimeRecoveryPressureCoordinator(
        quiesce_callback=quiesce,
    )
    timer = threading.Timer(0.025, drain.set)
    timer.start()
    try:
        started = time.monotonic()
        lease = coordinator.acquire("activation", Priority.ACTIVATION)
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()

    assert lease is not None
    assert 0.015 <= elapsed < MAX_QUIESCE_SECONDS
    assert coordinator.snapshot().quiesce_state is QuiesceState.QUIESCED
    assert lease.release()


def test_high_pressure_is_published_before_quiesce_drain_completes() -> None:
    quiesce_started = threading.Event()
    allow_drain = threading.Event()
    acquired: list[object] = []

    def quiesce() -> None:
        quiesce_started.set()
        assert allow_drain.wait(0.5)

    coordinator = RuntimeRecoveryPressureCoordinator(quiesce_callback=quiesce)

    def acquire_activation() -> None:
        acquired.append(coordinator.acquire("activation", Priority.ACTIVATION))

    worker = threading.Thread(target=acquire_activation)
    worker.start()
    assert quiesce_started.wait(0.5)
    snapshot = coordinator.snapshot()
    assert snapshot.active_owner_ids == ("activation",)
    assert snapshot.quiesce_state is QuiesceState.IN_PROGRESS
    assert coordinator.acquire("new-background", Priority.BACKGROUND) is None

    allow_drain.set()
    worker.join(0.5)
    assert not worker.is_alive()
    assert acquired and acquired[0] is not None


def test_one_quiesce_episode_and_one_resume_cover_all_high_pressure_owners() -> None:
    callbacks: list[str] = []
    coordinator = RuntimeRecoveryPressureCoordinator(
        quiesce_callback=lambda: callbacks.append("quiesce"),
        resume_callback=lambda: callbacks.append("resume"),
    )

    manual = coordinator.acquire("manual", Priority.MANUAL)
    assert callbacks == []
    activation = coordinator.acquire("activation", Priority.ACTIVATION)
    recovery = coordinator.acquire("recovery", Priority.RUNTIME_RECOVERY)
    assert callbacks == ["quiesce"]

    assert activation is not None and activation.release()
    assert callbacks == ["quiesce"]
    assert recovery is not None and recovery.release()
    assert callbacks == ["quiesce", "resume"]
    assert manual is not None and manual.release()
    assert callbacks == ["quiesce", "resume"]


def test_callback_exceptions_are_terminal_state_not_leaked_control_flow() -> None:
    def fail_quiesce() -> None:
        raise RuntimeError("private integration detail")

    def fail_resume() -> None:
        raise ValueError("private integration detail")

    coordinator = RuntimeRecoveryPressureCoordinator(
        quiesce_callback=fail_quiesce,
        resume_callback=fail_resume,
    )

    lease = coordinator.acquire("activation", Priority.ACTIVATION)
    assert lease is not None
    assert coordinator.snapshot().quiesce_state is QuiesceState.FAILED
    assert lease.release()

    snapshot = coordinator.snapshot()
    assert snapshot.quiesce_state is QuiesceState.IDLE
    assert snapshot.active_lease_count == 0
    assert snapshot.resume_callback_count == 1
    assert snapshot.last_callback_error == "resume_callback:ValueError"


def test_lifecycle_reset_invalidates_old_lease_and_stale_generation() -> None:
    callbacks: list[str] = []
    coordinator = RuntimeRecoveryPressureCoordinator(
        lifecycle_generation=7,
        quiesce_callback=lambda: callbacks.append("quiesce"),
        resume_callback=lambda: callbacks.append("resume"),
    )
    old = coordinator.acquire(
        "old-activation",
        Priority.ACTIVATION,
        lifecycle_generation=7,
    )
    assert old is not None

    reset = coordinator.reset_for_lifecycle(8)
    assert reset.lifecycle_generation == 8
    assert reset.active_lease_count == 0
    assert old.released
    assert old.release()
    assert callbacks == ["quiesce", "resume"]
    assert (
        coordinator.acquire(
            "stale",
            Priority.RUNTIME_RECOVERY,
            lifecycle_generation=7,
        )
        is None
    )
    current = coordinator.acquire(
        "current",
        Priority.ACTIVATION,
        lifecycle_generation=8,
    )
    assert current is not None
    assert coordinator.snapshot().active_owner_ids == ("current",)


def test_concurrent_duplicate_clicks_create_one_lease_and_one_quiesce() -> None:
    callback_count = 0
    callback_lock = threading.Lock()

    def quiesce() -> None:
        nonlocal callback_count
        with callback_lock:
            callback_count += 1

    coordinator = RuntimeRecoveryPressureCoordinator(quiesce_callback=quiesce)
    with ThreadPoolExecutor(max_workers=16) as executor:
        leases = list(
            executor.map(
                lambda _: coordinator.acquire("manual-request", Priority.ACTIVATION),
                range(100),
            )
        )

    assert all(lease is leases[0] for lease in leases)
    assert callback_count == 1
    assert coordinator.snapshot().active_lease_count == 1
    assert leases[0] is not None and leases[0].release()


def test_stop_is_bounded_even_when_callbacks_do_not_cooperate() -> None:
    unblock_quiesce = threading.Event()
    unblock_resume = threading.Event()

    coordinator = RuntimeRecoveryPressureCoordinator(
        quiesce_callback=lambda: unblock_quiesce.wait(),
        resume_callback=lambda: unblock_resume.wait(),
        quiesce_timeout_seconds=0.02,
    )
    lease = coordinator.acquire("recovery", Priority.RUNTIME_RECOVERY)
    assert lease is not None

    started = time.monotonic()
    snapshot = coordinator.stop(timeout_seconds=0.04)
    elapsed = time.monotonic() - started

    try:
        assert elapsed < 0.2
        assert snapshot.stopped
        assert snapshot.quiesce_state is QuiesceState.STOPPED
        assert snapshot.active_lease_count == 0
        assert lease.released
        assert lease.release()
        assert coordinator.acquire("late", Priority.RUNTIME_RECOVERY) is None
    finally:
        unblock_quiesce.set()
        unblock_resume.set()


def test_stop_during_release_resume_does_not_dispatch_resume_twice() -> None:
    resume_started = threading.Event()
    unblock_resume = threading.Event()
    callback_count = 0
    callback_lock = threading.Lock()

    def resume() -> None:
        nonlocal callback_count
        with callback_lock:
            callback_count += 1
        resume_started.set()
        unblock_resume.wait()

    coordinator = RuntimeRecoveryPressureCoordinator(resume_callback=resume)
    lease = coordinator.acquire("activation", Priority.ACTIVATION)
    assert lease is not None
    release_thread = threading.Thread(target=lease.release)
    release_thread.start()
    assert resume_started.wait(0.5)

    stopped = coordinator.stop(timeout_seconds=0.02)
    try:
        assert stopped.stopped
        assert callback_count == 1
        assert stopped.resume_callback_count == 1
    finally:
        unblock_resume.set()
        release_thread.join(0.5)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("runtime recovery", Priority.RUNTIME_RECOVERY),
        ("cold-activation", Priority.ACTIVATION),
        ("manual_validation", Priority.MANUAL),
        ("hot standby", Priority.HOT_STANDBY),
        ("background", Priority.BACKGROUND),
    ],
)
def test_priority_normalisation_is_closed_and_integration_friendly(
    value: str,
    expected: Priority,
) -> None:
    assert normalise_recovery_pressure_priority(value) is expected


def test_unknown_priority_fails_closed() -> None:
    coordinator = RuntimeRecoveryPressureCoordinator()
    with pytest.raises(ValueError, match="unknown runtime recovery priority"):
        coordinator.acquire("owner", "supplier-magic")
