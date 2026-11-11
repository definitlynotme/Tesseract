from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus
from node_diagnostic import diagnostic_node_identity, node_config_fingerprint
from runtime_reload_gate import RuntimeReloadReason


def _node(name: str, *, tier: str) -> Node:
    return Node(
        name=name,
        protocol="ss",
        address=f"{name}.example",
        port=8388,
        tier=tier,
        method="aes-256-gcm",
        password=f"{name}-password",
        status=NodeStatus.ALIVE,
        latency_ms=100.0,
    )


def _settings_runtime(*, core_running: bool) -> tuple[DesktopRuntime, Any]:
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger(f"latency-policy-settings-{id(runtime)}")
    runtime.core_runner = SimpleNamespace(
        is_running=MagicMock(return_value=core_running)
    )
    manager = SimpleNamespace(
        check_exit=MagicMock(),
        check_tier1=MagicMock(),
        check_tier2=MagicMock(),
        check_tier3=MagicMock(),
        evaluate_state=MagicMock(),
    )
    runtime.manager = manager
    runtime.update_local_config = MagicMock()
    runtime.reload_core_config = MagicMock(return_value={"reloaded": True})
    runtime.restart_core = MagicMock()
    return runtime, manager


def test_saving_latency_policy_with_core_stopped_only_persists_once() -> None:
    runtime, manager = _settings_runtime(core_running=False)
    persisted: list[dict[str, Any]] = []

    def update_once(updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        payload = {"testing": {"slow_threshold_ms": 2000}}
        updated = updater(payload)
        persisted.append(copy.deepcopy(updated))
        return {"written": True}

    runtime.update_local_config.side_effect = update_once

    result = runtime.apply_latency_policy_settings(1200)

    assert runtime.update_local_config.call_count == 1
    assert persisted == [
        {
            "testing": {
                "slow_threshold_ms": 2000,
                "primary_gateway_max_latency_ms": 1200,
            }
        }
    ]
    assert result["saved"] is True
    assert result["core_running"] is False
    assert result["recheck_attempted"] is False
    assert result["recheck_order"] == []
    assert result["tier3_skipped"] is True
    assert result["checked"] == 0
    assert result["errors"] == []
    manager.check_exit.assert_not_called()
    manager.check_tier1.assert_not_called()
    manager.check_tier2.assert_not_called()
    manager.check_tier3.assert_not_called()
    manager.evaluate_state.assert_not_called()
    runtime.reload_core_config.assert_not_called()
    runtime.restart_core.assert_not_called()


def test_saving_latency_policy_with_core_running_rechecks_in_order_and_reloads() -> None:
    runtime, manager = _settings_runtime(core_running=True)
    call_order: list[str] = []

    runtime.update_local_config.side_effect = lambda updater: updater({"testing": {}})
    manager.check_exit.side_effect = lambda **_kwargs: (
        call_order.append("exit") or [_node("gateway", tier="exit")]
    )

    slow = _node("entry", tier="tier1")
    slow.status = NodeStatus.SLOW
    manager.check_tier1.side_effect = lambda: (
        call_order.append("tier1") or [slow]
    )

    dead = _node("sentinel", tier="tier2")
    dead.status = NodeStatus.DEAD
    manager.check_tier2.side_effect = lambda **_kwargs: (
        call_order.append("tier2") or [dead]
    )
    manager.evaluate_state.side_effect = lambda: call_order.append("evaluate")
    runtime.reload_core_config.side_effect = lambda **_kwargs: (
        call_order.append("reload") or {"reloaded": True}
    )

    result = runtime.apply_latency_policy_settings(800)

    assert call_order == ["exit", "tier1", "tier2", "evaluate", "reload"]
    manager.check_exit.assert_called_once_with(force_remote_refresh=False)
    manager.check_tier1.assert_called_once_with()
    manager.check_tier2.assert_called_once_with(force_remote_refresh=False)
    manager.check_tier3.assert_not_called()
    manager.evaluate_state.assert_called_once_with()
    reload_call = runtime.reload_core_config.call_args
    assert reload_call.kwargs["mode"] == "incremental_export"
    assert reload_call.kwargs["reason"] is RuntimeReloadReason.USER_GRAPH_CONFIG
    assert str(reload_call.kwargs["user_commit_id"]).startswith("latency-policy:800:")
    runtime.restart_core.assert_not_called()
    assert result["core_running"] is True
    assert result["recheck_attempted"] is True
    assert result["recheck_order"] == ["exit", "tier1", "tier2"]
    assert result["tier3_skipped"] is True
    assert result["status_counts"] == {"alive": 1, "slow": 1, "dead": 1}
    assert result["checked"] == 3
    assert result["errors"] == []


class _DiagnosticService:
    def __init__(
        self,
        *,
        state: str = "success",
        latency_ms: float | None = 801.0,
        reason: str = "ok",
        on_diagnose: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.latency_ms = latency_ms
        self.reason = reason
        self.on_diagnose = on_diagnose

    def diagnose(self, node: Node, **kwargs: Any) -> dict[str, Any]:
        if self.on_diagnose is not None:
            self.on_diagnose()
        return {
            "request_id": str(kwargs["request_id"]),
            "node_identity": str(kwargs["node_identity"]),
            "config_fingerprint": node_config_fingerprint(node),
            "mode": str(kwargs["mode"]),
            "started_at": "2026-08-04T00:00:00+00:00",
            "completed_at": "2026-08-04T00:00:01+00:00",
            "state": self.state,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
        }


def _diagnostic_runtime(
    tmp_path: Path,
    target: Node,
    service: _DiagnosticService,
) -> tuple[DesktopRuntime, Any]:
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.project_root = tmp_path
    runtime.config = {
        "client": {
            "mihomo_binary_path": "bin/mihomo.exe",
            "mihomo_runtime_config": "data/runtime.yaml",
            "mihomo_work_dir": "data/core",
        },
        "testing": {
            "probe_url": "https://probe.example/generate_204",
            "request_timeout": 5,
            "primary_gateway_max_latency_ms": 800,
            "slow_threshold_ms": 2000,
        },
    }
    manager = SimpleNamespace(
        lock=threading.RLock(),
        operation_lock=threading.Lock(),
        runtime_nodes={
            tier: [target] if tier == target.tier else []
            for tier in ("tier1", "tier2", "tier3", "exit")
        },
        _replace_tested_runtime_node=MagicMock(),
        evaluate_state=MagicMock(),
    )
    runtime.manager = manager
    runtime.node_diagnostic_service = service
    runtime._node_diagnostic_excluded_ports = MagicMock(return_value=set())
    return runtime, manager


@pytest.mark.parametrize(
    ("tier", "expected_status", "expected_limit", "expected_reason"),
    [
        ("tier1", NodeStatus.DEAD, 800, "latency_limit_exceeded"),
        ("tier3", NodeStatus.ALIVE, None, ""),
    ],
)
def test_successful_manual_diagnostic_uses_shared_latency_policy_and_commits(
    tmp_path: Path,
    tier: str,
    expected_status: NodeStatus,
    expected_limit: int | None,
    expected_reason: str,
) -> None:
    target = _node("manual-target", tier=tier)
    runtime, manager = _diagnostic_runtime(
        tmp_path,
        target,
        _DiagnosticService(latency_ms=801.0),
    )
    identity = diagnostic_node_identity(target)

    result = runtime.diagnose_node(identity, request_id=f"manual-{tier}")

    assert result["state"] == "success"
    assert result["status"] == expected_status.value
    assert result["status_applied"] is True
    assert result["quality_reason"] == expected_reason
    assert result["latency_limit_ms"] == expected_limit
    manager._replace_tested_runtime_node.assert_called_once()
    tier_arg, identity_arg, tested_node = manager._replace_tested_runtime_node.call_args.args
    assert tier_arg == tier
    assert identity_arg == identity
    assert tested_node.status is expected_status
    assert tested_node.latency_ms == 801.0
    if tier == "tier1":
        assert tested_node.extra == {
            "latency_policy_state": "over_limit",
            "latency_policy_limit_ms": 800,
            "latency_policy_observed_ms": 801.0,
            "latency_policy_scope": "manual_direct",
        }
    else:
        assert tested_node.extra == {}
    assert manager._replace_tested_runtime_node.call_args.kwargs == {
        "mark_layer_check": True
    }
    manager.evaluate_state.assert_called_once_with()


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled"])
def test_unsuccessful_manual_diagnostic_does_not_commit(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    target = _node("manual-target", tier="tier1")
    runtime, manager = _diagnostic_runtime(
        tmp_path,
        target,
        _DiagnosticService(
            state=terminal_state,
            latency_ms=None,
            reason="timeout" if terminal_state == "failed" else "cancelled",
        ),
    )

    result = runtime.diagnose_node(
        diagnostic_node_identity(target),
        request_id=f"manual-{terminal_state}",
    )

    assert result["state"] == terminal_state
    manager._replace_tested_runtime_node.assert_not_called()
    manager.evaluate_state.assert_not_called()


def test_manual_diagnostic_node_changed_result_does_not_commit(tmp_path: Path) -> None:
    target = _node("manual-target", tier="tier1")
    service = _DiagnosticService()
    runtime, manager = _diagnostic_runtime(tmp_path, target, service)
    service.on_diagnose = lambda: manager.runtime_nodes["tier1"].clear()

    result = runtime.diagnose_node(
        diagnostic_node_identity(target),
        request_id="manual-node-changed",
    )

    assert result["state"] == "failed"
    assert result["reason"] == "node_changed"
    assert result["latency_ms"] is None
    manager._replace_tested_runtime_node.assert_not_called()
    manager.evaluate_state.assert_not_called()
