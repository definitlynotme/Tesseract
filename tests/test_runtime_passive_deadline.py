from __future__ import annotations

import pytest

import desktop_runtime
from desktop_runtime import DesktopRuntime, _RuntimeProbeBudgetUnavailable
from tests.test_runtime_passive_failback import request, runtime


def test_expired_absolute_verifier_deadline_does_not_start_another_probe(monkeypatch):
    service = object.__new__(DesktopRuntime)
    monkeypatch.setattr(desktop_runtime.time, "monotonic", lambda: 106.0)
    service._default_probe_urls = lambda: ["https://neutral.test/204"]
    calls = []
    service._probe_mihomo_mixed_port_once = lambda **kwargs: calls.append(kwargs) or {"status": 204}

    with pytest.raises((TimeoutError, _RuntimeProbeBudgetUnavailable)):
        service._verify_runtime_final_exit(
            require_controller_ready=False,
            timeout_seconds=0.1,
            recovery_deadline_monotonic=106.0,
        )

    assert calls == []


@pytest.mark.parametrize("remaining", [0.0, 0.05])
def test_absolute_budget_below_socket_minimum_never_enters_broker(monkeypatch, remaining):
    service = object.__new__(DesktopRuntime)
    monkeypatch.setattr(desktop_runtime.time, "monotonic", lambda: 100.0)
    calls = []
    service._run_brokered_runtime_probe = lambda *_args, **kwargs: calls.append(kwargs)

    with pytest.raises((TimeoutError, _RuntimeProbeBudgetUnavailable)):
        service._probe_mihomo_mixed_port_once(
            timeout_seconds=1.5,
            recovery_deadline_monotonic=100.0 + remaining,
        )

    assert calls == []


def test_absolute_deadline_reaches_broker_and_probe_without_incident_ticket(monkeypatch):
    service = object.__new__(DesktopRuntime)
    monkeypatch.setattr(desktop_runtime.time, "monotonic", lambda: 100.0)
    broker_calls, probe_calls = [], []

    def broker(callback, **kwargs):
        broker_calls.append(kwargs)
        return callback(None)

    service._run_brokered_runtime_probe = broker
    service._probe_mihomo_mixed_port_once_leased = lambda **kwargs: probe_calls.append(kwargs) or {"status": 204}
    result = service._probe_mihomo_mixed_port_once(
        timeout_seconds=1.5,
        recovery_deadline_monotonic=100.3,
    )

    assert result["status"] == 204
    assert broker_calls[0]["execution_deadline_monotonic"] == pytest.approx(100.3)
    assert probe_calls[0]["timeout_seconds"] == pytest.approx(0.3)
    assert probe_calls[0]["recovery_deadline_monotonic"] == 100.3
    assert probe_calls[0].get("recovery_ticket") is None


def test_nested_failback_cannot_open_a_new_204_after_rollback_spends_six_seconds(runtime):
    clock = [100.0]
    runtime._runtime_recovery_now = lambda: clock[0]
    runtime._runtime_passive_failback_target = lambda *_args: (runtime.test.a, "nested-group")
    verifications = []
    rollbacks = []

    def release(*_args, **_kwargs):
        runtime.test.state.update(leaf="a", fixed="")
        clock[0] = 103.7
        return {"commit_state": "committed"}

    def restore(**kwargs):
        # The rollback itself completes inside the six-second transaction, but
        # leaves less than the minimum socket budget for any further request.
        rollbacks.append(kwargs["deadline_monotonic"])
        clock[0] = kwargs["deadline_monotonic"] - 0.01
        runtime.test.state.update(leaf="b", fixed="b")
        return runtime.test.snapshot, runtime.test.payload, None

    def verify(**kwargs):
        verifications.append((clock[0], kwargs))
        if len(verifications) == 1:
            raise RuntimeError("promotion proof failed")
        pytest.fail("rollback started a fresh 204 after exhausting its socket budget")

    runtime._release_runtime_automatic_fixed_selection = release
    runtime._restore_runtime_automatic_route = restore
    runtime._verify_runtime_final_exit = verify

    assert runtime._attempt_runtime_passive_failback(request(runtime)) == "pending"
    assert rollbacks == [106.0]
    assert len(verifications) == 1
    assert verifications[0][1]["recovery_deadline_monotonic"] == 104.0
    assert "source-proof" not in runtime.test.calls
    assert ("egress", True) not in runtime.test.calls
