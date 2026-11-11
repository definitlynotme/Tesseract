import multiprocessing
import threading

from tier3_validator_supervisor import Tier3ValidatorSupervisor, _close_stopped_process
from test_support.tier3_deterministic_helper import deterministic_helper_bootstrap


def _held_starter(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture", probe_urls=["http://fixture/probe"],
        expected_statuses={204}, probe_timeout_seconds=1,
        slow_threshold_ms=3000, capacity_sink=lambda _: None,
        attempt_sink=lambda _: None, min_workers=1, max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )

    def held_spawn(*_):
        entered.set()
        release.wait(5)
        raise RuntimeError("controlled spawn cancellation")

    monkeypatch.setattr(supervisor._starter, "_spawn", held_spawn)
    supervisor.start()
    assert entered.wait(2)
    return supervisor, release


def _finish(supervisor, release):
    release.set()
    for thread in supervisor._starter._threads:
        thread.join(timeout=2)
    supervisor._drain_spawn_results(discard=True)
    supervisor.stop(timeout_seconds=2)


def test_stop_waits_for_inflight_starter_within_shared_deadline(monkeypatch):
    supervisor, release = _held_starter(monkeypatch)
    timer = threading.Timer(0.7, release.set)
    timer.start()
    try:
        assert supervisor.stop(timeout_seconds=2) == "STOPPED"
        assert not any(t.is_alive() for t in supervisor._starter._threads)
        assert supervisor._starter.pending_count() == 0
    finally:
        timer.cancel()
        _finish(supervisor, release)


def test_stop_does_not_publish_closed_while_starter_can_create_process(monkeypatch):
    supervisor, release = _held_starter(monkeypatch)
    try:
        assert supervisor.stop(timeout_seconds=0.7) == "STOP_FAILED_QUARANTINED"
    finally:
        _finish(supervisor, release)


def test_unstarted_process_is_not_an_unreaped_child():
    process = multiprocessing.get_context("spawn").Process()
    assert _close_stopped_process(process)
    assert _close_stopped_process(process)
