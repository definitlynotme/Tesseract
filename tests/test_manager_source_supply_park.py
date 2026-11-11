from __future__ import annotations

import threading
import time

from manager import ProxyTierManager


def bare_manager() -> ProxyTierManager:
    manager = ProxyTierManager.__new__(ProxyTierManager)
    manager._tier3_source_supply_wake_event = threading.Event()
    manager._tier3_source_supply_signal_lock = threading.Lock()
    manager._tier3_source_supply_route_revision = 0
    manager._tier3_source_supply_wake_reason = ""
    manager._tier3_source_supply_parked_until = 0.0
    manager._tier3_source_supply_parked_reason = ""
    return manager


def test_park_is_bounded_and_wakes_the_loop_so_it_can_apply_the_park():
    manager = bare_manager()
    assert manager._tier3_source_supply_park_remaining() == 0.0
    manager.park_tier3_source_supply(300.0, "no_egress")
    remaining = manager._tier3_source_supply_park_remaining()
    assert 299.0 < remaining <= 300.0
    assert manager._tier3_source_supply_parked_reason == "no_egress"
    assert manager._tier3_source_supply_wake_event.is_set()


def test_resume_lifts_the_park_and_dispatches_due_roots_immediately():
    manager = bare_manager()
    manager.park_tier3_source_supply(60.0, "no_egress")
    manager._tier3_source_supply_wake_event.clear()
    manager.resume_tier3_source_supply("no_egress_retry")
    assert manager._tier3_source_supply_park_remaining() == 0.0
    assert manager._tier3_source_supply_parked_reason == ""
    assert manager._tier3_source_supply_route_revision == 1
    assert manager._tier3_source_supply_wake_reason == "no_egress_retry"
    assert manager._tier3_source_supply_wake_event.is_set()
    # Resuming an idle supply is a no-op: no spurious wake, no revision bump.
    manager._tier3_source_supply_wake_event.clear()
    manager.resume_tier3_source_supply("again")
    assert manager._tier3_source_supply_route_revision == 1
    assert not manager._tier3_source_supply_wake_event.is_set()


def test_park_expires_on_its_own():
    manager = bare_manager()
    manager.park_tier3_source_supply(0.05, "no_egress")
    time.sleep(0.08)
    assert manager._tier3_source_supply_park_remaining() == 0.0
