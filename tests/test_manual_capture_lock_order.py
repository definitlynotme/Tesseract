from __future__ import annotations

import threading
from types import SimpleNamespace

from desktop_runtime import DesktopRuntime


def test_manual_publication_does_not_block_manager_route_truth_read():
    manager_attempted = threading.Event()
    manager_lock = threading.RLock()
    monitor_lock = threading.RLock()
    main_thread = threading.current_thread()

    class ObservedManagerLock:
        def __enter__(self):
            if threading.current_thread() is not main_thread:
                manager_attempted.set()
            return manager_lock.__enter__()

        def __exit__(self, *args):
            return manager_lock.__exit__(*args)

    runtime = object.__new__(DesktopRuntime)
    runtime.manager = SimpleNamespace(lock=ObservedManagerLock())
    runtime._runtime_failure_monitor_lock = monitor_lock
    runtime._runtime_mutation_binding_is_current = lambda _binding: True
    runtime._runtime_uses_early_single_coordinator = lambda: True
    runtime._current_core_runtime_phase = lambda: 'formal_no_capture'

    def queue():
        # Production queue reads the phase and capture flag together under
        # Manager.lock. Do that real lock operation at the same boundary.
        with runtime.manager.lock:
            pass

    runtime._queue_tier3_egress_activation = queue
    outcomes = []
    errors = []

    def manual():
        try:
            outcomes.append(runtime._promote_manual_egress_after_verified_probe(
                mutation_binding=object(), clean_current_success=True,
            ))
        except Exception as exc:
            errors.append(exc)

    with manager_lock:
        worker = threading.Thread(target=manual)
        worker.start()
        assert manager_attempted.wait(2)
        # The other actor models Manager's route-truth callback: it already
        # owns Manager.lock and now needs monitor. The old order deadlocks.
        could_read_truth = monitor_lock.acquire(timeout=0.25)
        if could_read_truth:
            monitor_lock.release()
    worker.join(2)
    assert not worker.is_alive()
    assert could_read_truth
    assert errors == []
    assert outcomes == [True]
