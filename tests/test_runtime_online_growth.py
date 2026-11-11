"""Online Tier3B growth across the online cohort freeze (11-01).

``tiers.tier3.online_core_growth`` (default on) lets ordinary pool growth reach
a serving Core.  Every other online pool sync stays parked by the freeze;
growth is admitted while the published TIER3-AUTO keeps fewer alive members
than the background target, and the pool CAS of a growth transaction accepts
admissions that only append to the export order.  The real-Core half is
``test_runtime_online_growth_real_core.py``.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from desktop_runtime import (
    DeferredProviderSyncKey,
    DesktopRuntime,
    ProviderSyncDisposition,
)


def _runtime(**tier3) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {"tiers": {"tier3": dict(tier3)}}
    runtime.stop_event = threading.Event()
    runtime.logger = MagicMock()
    return runtime


def _view(**overrides) -> dict:
    view = {
        "group": "TIER3-AUTO",
        "now": "n1",
        "fixed": "",
        "members": ("n1", "n2", "n3", "n4", "n5", "REJECT"),
        "alive": 5,
        "loaded": 5,
        "stale_fixed": False,
        "exhausted": False,
    }
    view.update(overrides)
    return view


def _online(view: dict | None = None, **tier3) -> DesktopRuntime:
    runtime = _runtime(**tier3)
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)
    runtime.running = True
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._runtime_initial_activation_was_completed = lambda: True
    runtime._tier3_initial_activation_transaction_active = lambda: False
    runtime._runtime_recovery_transaction_active = lambda: False
    runtime._node_judgement_state = lambda: SimpleNamespace(judge=None)
    runtime._published_tier3_route_view = lambda: view
    runtime.manager = SimpleNamespace(_tier3_background_target_alive_nodes=lambda: 25)
    return runtime


def test_pool_marker_extends_only_by_appending() -> None:
    extends = DesktopRuntime._tier3_pool_marker_extends
    bound = (("n1", "a"), ("n2", "b"))

    assert extends(bound, bound)
    assert extends((*bound, ("n3", "c")), bound)
    assert not extends((("n2", "b"), ("n1", "a"), ("n3", "c")), bound)
    assert not extends((("n1", "a"),), bound)
    assert not extends((("n1", "a"), ("n2", "rewritten"), ("n3", "c")), bound)
    assert not extends(None, bound)


def test_pool_binding_is_exact_unless_growth_tolerant() -> None:
    runtime = _runtime()
    bound = (("n1", "a"),)
    current = [bound]
    runtime._tier3_core_sync_marker = lambda: current[0]
    exact = {"tier3_pool_revision": 7, "tier3_pool_marker": bound}
    tolerant = {**exact, "pool_growth_tolerant": True}

    assert runtime._tier3_pool_binding_holds(exact, 7)
    assert not runtime._tier3_pool_binding_holds(exact, 8)

    current[0] = (*bound, ("n2", "b"))
    assert not runtime._tier3_pool_binding_holds(exact, 8)
    assert runtime._tier3_pool_binding_holds(tolerant, 8)

    current[0] = (("n2", "b"),)
    assert not runtime._tier3_pool_binding_holds(tolerant, 9)


@pytest.mark.parametrize(
    ("tier3", "enabled"),
    [
        ({}, True),
        ({"online_core_growth": True}, True),
        ({"online_core_growth": False}, False),
        ({"online_core_growth": "off"}, False),
        ({"online_core_growth": "yes"}, True),
    ],
)
def test_online_growth_switch_defaults_on(tier3: dict, enabled: bool) -> None:
    assert _runtime(**tier3)._tier3_online_growth_enabled() is enabled


def test_growth_state_names_what_keeps_the_cohort_frozen() -> None:
    assert _online(_view())._tier3_online_growth_state() == (True, "eligible")
    assert _online(_view(), online_core_growth=False)._tier3_online_growth_state() == (
        False,
        "disabled",
    )

    bootstrap = _online(_view())
    bootstrap._current_core_runtime_phase = lambda: "bootstrap"
    assert bootstrap._tier3_online_growth_state() == (False, "not_online")

    standby = _online(_view())
    standby._runtime_initial_activation_was_completed = lambda: False
    assert standby._tier3_online_growth_state() == (False, "activation_pending")

    recovering = _online(_view())
    recovering._runtime_recovery_transaction_active = lambda: True
    assert recovering._tier3_online_growth_state() == (False, "recovery_active")

    judging = _online(_view())
    judge = SimpleNamespace(judging=lambda: True, ladder_in_flight=lambda: False)
    judging._node_judgement_state = lambda: SimpleNamespace(judge=judge)
    assert judging._tier3_online_growth_state() == (False, "judging")

    assert _online(None)._tier3_online_growth_state() == (False, "no_route_view")
    assert _online(_view(fixed="n2"))._tier3_online_growth_state() == (
        False,
        "fixed_selection",
    )
    assert _online(_view(alive=25, loaded=25))._tier3_online_growth_state() == (
        False,
        "target_alive_reached",
    )


def _frozen_sync_runtime(provider_result: dict, *, eligible: bool) -> DesktopRuntime:
    runtime = _online(_view())
    runtime._tier3_online_provider_cohort_frozen = lambda: True
    runtime._tier3_online_growth_state = lambda: (
        (True, "eligible") if eligible else (False, "disabled")
    )
    runtime._tier3_core_sync_marker = lambda: ("pool", 9)
    runtime._update_running_tier3_core_provider = MagicMock(return_value=provider_result)
    runtime._mark_tier3_provider_sync_complete = MagicMock()
    runtime._record_tier3_core_sync_ledger = MagicMock()
    runtime._tier3_online_growth_waited_at = 0.0
    runtime._tier3_online_growth_waits = 0
    return runtime


def test_frozen_online_sync_still_parks_when_growth_is_off() -> None:
    runtime = _frozen_sync_runtime({"updated": True}, eligible=False)

    outcome = runtime._sync_core_config_after_pool_update()

    assert outcome.disposition is ProviderSyncDisposition.PARKED
    assert outcome.reason == "Core-loaded provider cohort is frozen while traffic is online"
    runtime._update_running_tier3_core_provider.assert_not_called()


def test_eligible_growth_runs_the_provider_transaction_as_growth() -> None:
    runtime = _frozen_sync_runtime(
        {"updated": True, "semantic_digest": "grown", "member_count": 9},
        eligible=True,
    )
    runtime._tier3_online_growth_waited_at = time.monotonic()
    runtime._tier3_online_growth_waits = 2

    outcome = runtime._sync_core_config_after_pool_update()

    assert outcome.disposition is ProviderSyncDisposition.COMMITTED
    runtime._update_running_tier3_core_provider.assert_called_once_with(
        ("pool", 9),
        online_growth=True,
    )
    runtime._mark_tier3_provider_sync_complete.assert_called_once_with(("pool", 9))
    assert runtime._tier3_online_growth_waits == 0
    assert runtime._tier3_online_growth_waited_at == 0.0
    message, members, target = runtime.logger.info.call_args.args
    assert message.startswith("Bastion Layer online growth committed")
    assert (members, target) == (9, 25)


def test_growth_wait_parks_and_backs_off() -> None:
    reason = "online growth waits until TIER3-AUTO selects its first member"
    runtime = _frozen_sync_runtime(
        {"updated": False, "deferred": True, "growth_wait": True, "reason": reason},
        eligible=True,
    )

    outcome = runtime._sync_core_config_after_pool_update()

    assert outcome.disposition is ProviderSyncDisposition.PARKED
    assert outcome.reason == reason
    assert runtime._tier3_online_growth_waits == 1
    assert not runtime._tier3_online_growth_retry_due()
    runtime._tier3_online_growth_waited_at = time.monotonic() - 16.0
    assert runtime._tier3_online_growth_retry_due()
    runtime._tier3_online_growth_waits = 2
    assert not runtime._tier3_online_growth_retry_due()
    runtime._tier3_online_growth_waited_at = time.monotonic() - 31.0
    assert runtime._tier3_online_growth_retry_due()


def _parked_runtime() -> DesktopRuntime:
    runtime = _runtime()
    runtime._tier3_pool_sync_lock = threading.Lock()
    runtime._tier3_pool_sync_pending = False
    runtime._tier3_pool_sync_worker_running = False
    runtime._tier3_pool_sync_parked_key = "parked-key"
    runtime._tier3_pool_sync_parked_event = "append_only"
    runtime._tier3_pool_sync_last_attempted_key = "parked-key"
    runtime._tier3_online_growth_waited_at = time.monotonic()
    runtime._tier3_online_growth_waits = 1
    runtime._tier3_online_growth_state = lambda: (True, "eligible")
    runtime._tier3_online_growth_surplus = lambda: 3
    runtime._queue_tier3_pool_sync_after_commit = MagicMock(return_value=True)
    return runtime


def test_parked_growth_is_retried_only_after_its_backoff() -> None:
    runtime = _parked_runtime()
    queue = runtime._queue_tier3_pool_sync_after_commit

    assert runtime._retry_parked_tier3_online_growth() is False
    queue.assert_not_called()

    runtime._tier3_online_growth_waited_at = time.monotonic() - 16.0
    assert runtime._retry_parked_tier3_online_growth() is True
    queue.assert_called_once_with(immediate=True, sync_event="append_only")
    assert runtime._tier3_pool_sync_parked_key is None
    assert runtime._tier3_pool_sync_last_attempted_key is None


def test_parked_growth_is_not_retried_without_surplus_or_eligibility() -> None:
    no_surplus = _parked_runtime()
    no_surplus._tier3_online_growth_waited_at = 0.0
    no_surplus._tier3_online_growth_surplus = lambda: 0
    assert no_surplus._retry_parked_tier3_online_growth() is False

    frozen = _parked_runtime()
    frozen._tier3_online_growth_waited_at = 0.0
    frozen._tier3_online_growth_state = lambda: (False, "target_alive_reached")
    assert frozen._retry_parked_tier3_online_growth() is False

    busy = _parked_runtime()
    busy._tier3_online_growth_waited_at = 0.0
    busy._tier3_pool_sync_pending = True
    assert busy._retry_parked_tier3_online_growth() is False

    for runtime in (no_surplus, frozen, busy):
        runtime._queue_tier3_pool_sync_after_commit.assert_not_called()
        assert runtime._tier3_pool_sync_parked_key == "parked-key"


def _queue_runtime(surplus: int) -> tuple[DesktopRuntime, list]:
    runtime = _runtime()
    runtime.manager = None
    runtime._tier3_pool_sync_lock = threading.Lock()
    runtime._tier3_pool_sync_pending = False
    runtime._tier3_pool_sync_worker_running = False
    runtime._tier3_pool_sync_thread = None
    runtime._tier3_pool_sync_last_requested_at = 0.0
    runtime._tier3_pool_sync_oldest_requested_at = 0.0
    runtime._tier3_pool_sync_pending_commit_count = 0
    runtime._tier3_pool_sync_pending_immediate = False
    runtime._tier3_pool_sync_pending_event = "append_only"
    runtime._tier3_pool_sync_pending_key = None
    runtime._tier3_pool_sync_active_key = None
    runtime._tier3_pool_sync_parked_key = None
    runtime._tier3_pool_sync_parked_event = "append_only"
    runtime._tier3_pool_sync_last_attempted_key = None
    runtime._tier3_pool_sync_batch_size = 8
    runtime._tier3_pool_sync_max_wait_seconds = 30.0
    runtime._tier3_pool_sync_stable_hard_limit_seconds = 30.0
    runtime._tier3_pool_sync_cooldown_until = 0.0
    runtime._tier3_pool_sync_debounce_seconds = 1.0
    runtime._tier3_pool_sync_slow_threshold_seconds = 5.0
    runtime._tier3_pool_sync_slow_cooldown_seconds = 2.0
    runtime._tier3_runtime_qualification_stable = lambda: False
    runtime._tier3_pool_sync_formal_baseline_loaded = lambda: False
    runtime._mark_tier3_pool_sync_formal_baseline_loaded = MagicMock()
    runtime._record_tier3_core_sync_ledger = MagicMock()
    runtime._record_tier3_core_sync_event = MagicMock()
    runtime._tier3_pool_target_reached = lambda: False
    runtime._tier3_online_provider_cohort_frozen = lambda: True
    runtime._tier3_online_growth_state = lambda: (True, "eligible")
    runtime._tier3_online_growth_waited_at = 0.0
    runtime._tier3_online_growth_waits = 0
    runtime._tier3_online_growth_surplus = lambda: surplus
    key = DeferredProviderSyncKey(
        runtime_generation=1,
        core_generation=1,
        pool_revision=12,
        provider_digest="grown",
        loaded_digest="loaded",
        network_epoch=1,
        facility_epoch=1,
        serving=True,
    )
    runtime._tier3_provider_sync_key = lambda: key
    attempts: list = []

    def sync(*, expected_key):
        attempts.append(expected_key)
        return ProviderSyncDisposition.NOOP

    runtime._sync_core_config_after_pool_update = sync
    return runtime, attempts


def test_a_whole_growth_batch_is_pushed_at_once() -> None:
    runtime, attempts = _queue_runtime(surplus=4)

    assert runtime._queue_tier3_pool_sync_after_commit() is True

    deadline = time.monotonic() + 2.0
    while not attempts and time.monotonic() < deadline:
        time.sleep(0.01)
    runtime.stop_event.set()
    assert len(attempts) == 1


def test_a_smaller_surplus_follows_the_ordinary_batching() -> None:
    runtime, attempts = _queue_runtime(surplus=3)

    assert runtime._queue_tier3_pool_sync_after_commit() is True

    time.sleep(0.4)
    runtime.stop_event.set()
    assert attempts == []
    assert runtime._tier3_pool_sync_pending_immediate is False
