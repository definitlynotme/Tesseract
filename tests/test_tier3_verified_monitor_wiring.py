"""Monitor-loop wiring of the verified Tier3B lane (review LSC-3, 11-03).

With the online provider cohort frozen, a tick retries a parked provider
write, or else re-runs the write that kept a kicked pick (F5); every tick also
runs the Gateway binding tick, which saves an old name-only Gateway selection
as the identity of the node the loaded Core serves for it (F22, T-GW-ID).  One
real ``_monitor_loop`` cycle per case, everything around it stubbed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.test_runtime_serving_fallback_regressions import _harness_runtime


class _OneCycleStopEvent:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, _timeout: float = 0.0) -> bool:
        self.stopped = True
        return True


def _one_cycle(tmp_path: Path, *, frozen: bool, parked_retried: bool):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _harness, runtime, _manager, _runner = _harness_runtime(tmp_path)
    runtime.stop_event = _OneCycleStopEvent()
    runtime.manager = MagicMock()
    for name in (
        "_write_runtime_lifecycle_heartbeat_if_due",
        "_sync_effective_egress_state_from_core",
        "_maybe_probe_runtime_failure_guard",
        "_wake_parked_tier3_pool_sync_if_causal_key_changed",
        "_queue_tier3_pool_sync_after_commit",
        "_maybe_retry_tier3_standby_activation",
        "_maybe_revalidate_tier3_runtime_qualification",
        "_monitor_core_health",
        "_clear_last_error_if_runtime_healthy",
        "_retry_kicked_tier3_pick",
        "_persist_gateway_selection_binding",
    ):
        setattr(runtime, name, MagicMock(return_value=False))
    runtime._remote_refresh_egress_proxy_config = MagicMock(return_value=None)
    runtime._tier3_online_provider_cohort_frozen = MagicMock(return_value=frozen)
    runtime._retry_parked_tier3_online_growth = MagicMock(return_value=parked_retried)

    runtime._monitor_loop()

    runtime.manager.run_cycle.assert_called_once()
    return runtime


def test_a_frozen_tick_re_runs_a_kicked_pick_when_no_parked_write_was_retried(tmp_path):
    runtime = _one_cycle(tmp_path / "idle", frozen=True, parked_retried=False)
    runtime._retry_parked_tier3_online_growth.assert_called_once()
    runtime._retry_kicked_tier3_pick.assert_called_once()
    runtime._queue_tier3_pool_sync_after_commit.assert_not_called()

    runtime = _one_cycle(tmp_path / "parked", frozen=True, parked_retried=True)
    runtime._retry_parked_tier3_online_growth.assert_called_once()
    runtime._retry_kicked_tier3_pick.assert_not_called()


def test_an_unfrozen_tick_queues_the_keyed_sync_instead(tmp_path):
    runtime = _one_cycle(tmp_path, frozen=False, parked_retried=False)
    runtime._queue_tier3_pool_sync_after_commit.assert_called_once()
    runtime._retry_parked_tier3_online_growth.assert_not_called()
    runtime._retry_kicked_tier3_pick.assert_not_called()


@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "unfrozen"])
def test_every_tick_runs_the_gateway_binding_tick(tmp_path, frozen):
    """The binding tick (_persist_gateway_selection_binding, which replaced
    _persist_resolved_gateway_selection) runs once per tick, frozen or not."""
    runtime = _one_cycle(tmp_path, frozen=frozen, parked_retried=False)
    runtime._persist_gateway_selection_binding.assert_called_once()
    assert not hasattr(runtime, "_persist_resolved_gateway_selection")
