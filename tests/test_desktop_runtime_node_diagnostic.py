from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

from collector import save_snapshot
from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus, PoolSnapshot
from node_diagnostic import diagnostic_node_identity, node_config_fingerprint


TIER2_URL = "https://subscriptions.example/runtime-tier2"
EXIT_URL = "https://subscriptions.example/runtime-exit"
TIER3_URL = "https://subscriptions.example/runtime-tier3"


def _node(
    name: str,
    *,
    tier: str,
    source: str = "",
    address: str | None = None,
    port: int = 8388,
) -> Node:
    return Node(
        name=name,
        protocol="ss",
        address=address or f"{name}.example",
        port=port,
        source=source,
        tier=tier,
        method="aes-256-gcm",
        password=f"{name}-password",
        status=NodeStatus.ALIVE,
        latency_ms=123.0,
        last_checked_at="2026-08-02T00:00:00+00:00",
    )


def _write_cache(path: Path, *, tier: str, url: str, nodes: list[Node]) -> None:
    assert save_snapshot(
        PoolSnapshot(
            generated_at="2026-08-02T00:00:00+00:00",
            metadata={"tier": tier, "source_urls": [url]},
            nodes=nodes,
        ),
        path,
    )


class _DiagnosticService:
    def __init__(
        self,
        *,
        state: str = "success",
        reason: str = "ok",
        latency_ms: float | None = 86.0,
        on_diagnose: Callable[[Node, dict[str, Any]], None] | None = None,
    ) -> None:
        self.state = state
        self.reason = reason
        self.latency_ms = latency_ms
        self.on_diagnose = on_diagnose
        self.calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []
        self.cancel_all_calls = 0
        self.close_calls: list[float] = []

    def diagnose(self, node: Node, **kwargs: Any) -> dict[str, Any]:
        call = {
            "node": node,
            **kwargs,
            "upstream_nodes": list(kwargs.get("upstream_nodes") or []),
        }
        self.calls.append(call)
        captured_fingerprint = node_config_fingerprint(node)
        if self.on_diagnose is not None:
            self.on_diagnose(node, call)
        return {
            "request_id": str(kwargs["request_id"]),
            "node_identity": str(kwargs["node_identity"]),
            "config_fingerprint": captured_fingerprint,
            "mode": str(kwargs["mode"]),
            "started_at": "2026-08-02T00:00:01+00:00",
            "completed_at": "2026-08-02T00:00:02+00:00",
            "state": self.state,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
        }

    def cancel(self, request_id: str) -> bool:
        self.cancel_calls.append(request_id)
        return request_id == "active-request"

    def cancel_all(self) -> int:
        self.cancel_all_calls += 1
        return 1

    def close(self, *, timeout: float) -> bool:
        self.close_calls.append(timeout)
        return True


class _Manager:
    def __init__(self, inventory: dict[str, list[Node]]) -> None:
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.runtime_nodes = {
            tier: list(inventory.get(tier, []))
            for tier in ("tier1", "tier2", "tier3", "exit")
        }
        self.last_check_at = {
            "tier1": "before-tier1",
            "tier2": "before-tier2",
            "tier3": "before-tier3",
            "exit": "before-exit",
        }
        self.background_actions = {"formal": {"running": False}}
        self.selected_exit = "gateway-before"
        self.route_revision = 17
        self.replacement_calls: list[dict[str, Any]] = []
        self.evaluate_calls = 0

    def get_nodes_for_subscription(self, tier_id: str, limit: int | None = None) -> list[Node]:
        nodes = list(self.runtime_nodes.get(tier_id, []))
        return nodes[:limit] if limit else nodes

    def _replace_tested_runtime_node(
        self,
        tier_id: str,
        identity: str,
        tested_node: Node,
        *,
        mark_layer_check: bool,
    ) -> None:
        self.replacement_calls.append(
            {
                "tier_id": tier_id,
                "identity": identity,
                "tested_node": tested_node.clone(),
                "mark_layer_check": mark_layer_check,
            }
        )
        for index, current in enumerate(self.runtime_nodes.get(tier_id, [])):
            if diagnostic_node_identity(current) == identity:
                self.runtime_nodes[tier_id][index] = tested_node.clone()
                if mark_layer_check:
                    self.last_check_at[tier_id] = tested_node.last_checked_at
                return
        raise AssertionError(f"diagnostic target disappeared before commit: {identity}")

    def evaluate_state(self) -> None:
        self.evaluate_calls += 1


def _manager_state(manager: _Manager) -> dict[str, Any]:
    return copy.deepcopy(
        {
            "runtime_nodes": {
                tier: [node.to_dict() for node in nodes]
                for tier, nodes in manager.runtime_nodes.items()
            },
            "last_check_at": manager.last_check_at,
            "background_actions": manager.background_actions,
            "selected_exit": manager.selected_exit,
            "route_revision": manager.route_revision,
        }
    )


def _config(root: Path) -> dict[str, Any]:
    return {
        "client": {
            "mihomo_binary_path": "bin/formal-mihomo.exe",
            "mihomo_runtime_config": "data/formal-runtime.yaml",
            "mihomo_work_dir": "data/formal-core",
            "ipv6": False,
        },
        "testing": {
            "probe_url": "https://probe.example/generate_204",
            "request_timeout": 5,
        },
        "paths": {
            "tier2_cache_file": str(root / "tier2-cache.json"),
            "exit_cache_file": str(root / "exit-cache.json"),
        },
        "tiers": {
            "tier1": {"nodes": []},
            "tier2": {"nodes": [], "subscription_urls": []},
            "tier3": {
                "subscription_urls": [TIER3_URL],
                "cache_file": str(root / "tier3-cache.json"),
            },
            "exit": {"nodes": [], "subscription_urls": []},
        },
    }


def _runtime(
    root: Path,
    service: _DiagnosticService,
    *,
    manager: _Manager | None,
    config: dict[str, Any] | None = None,
) -> DesktopRuntime:
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.project_root = root
    runtime.config = config or _config(root)
    runtime.manager = manager
    runtime.node_diagnostic_service = service
    runtime.logger = logging.getLogger(f"runtime-node-diagnostic-{id(runtime)}")
    runtime.core_runner = MagicMock(name="formal_core_runner")
    runtime.api_client = MagicMock(name="formal_api_client")
    runtime.write_mihomo_runtime_config = MagicMock(name="write_formal_core_config")
    runtime.reload_mihomo_config = MagicMock(name="reload_formal_core")
    runtime.test_proxy_delay = MagicMock(name="formal_proxy_delay")
    runtime.audit_runtime_egress = MagicMock(name="formal_route_audit")
    runtime._tier3_nodes_for_core_export = MagicMock(
        side_effect=lambda _limit: list(manager.runtime_nodes["tier3"]) if manager else []
    )
    return runtime


def _formal_calls(runtime: DesktopRuntime) -> dict[str, Any]:
    return {
        "core_runner": list(runtime.core_runner.mock_calls),
        "api_client": list(runtime.api_client.mock_calls),
        "write_config": list(runtime.write_mihomo_runtime_config.mock_calls),
        "reload": list(runtime.reload_mihomo_config.mock_calls),
        "proxy_delay": list(runtime.test_proxy_delay.mock_calls),
        "route_audit": list(runtime.audit_runtime_egress.mock_calls),
    }


def test_core_off_and_manager_absent_resolves_tier2_and_exit_from_persisted_cache(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["tiers"]["tier2"]["subscription_urls"] = [TIER2_URL]
    config["tiers"]["exit"]["subscription_urls"] = [EXIT_URL]
    tier2 = _node("cached-tier2", tier="tier2", source=TIER2_URL)
    gateway = _node("cached-gateway", tier="exit", source=EXIT_URL, port=9443)
    _write_cache(
        tmp_path / "tier2-cache.json",
        tier="tier2",
        url=TIER2_URL,
        nodes=[tier2],
    )
    _write_cache(
        tmp_path / "exit-cache.json",
        tier="exit",
        url=EXIT_URL,
        nodes=[gateway],
    )
    cache_before = {
        path: path.read_bytes()
        for path in (tmp_path / "tier2-cache.json", tmp_path / "exit-cache.json")
    }
    service = _DiagnosticService()
    runtime = _runtime(tmp_path, service, manager=None, config=config)
    runtime.core_runner = None
    runtime.api_client = None

    tier2_result = runtime.diagnose_node(
        diagnostic_node_identity(tier2),
        request_id="tier2-cache-request",
        probe_mode="all",
    )
    exit_result = runtime.diagnose_node(
        diagnostic_node_identity(gateway),
        request_id="exit-cache-request",
        probe_mode="direct",
    )

    assert tier2_result["state"] == "success"
    assert exit_result["state"] == "success"
    assert [call["node"].tier for call in service.calls] == ["tier2", "exit"]
    assert [call["request_id"] for call in service.calls] == [
        "tier2-cache-request",
        "exit-cache-request",
    ]
    assert all(call["mode"] == "direct" for call in service.calls)
    assert all(call["upstream_nodes"] == [] for call in service.calls)
    assert {path: path.read_bytes() for path in cache_before} == cache_before


def test_stopped_chain_uses_persisted_tier1_then_tier2_and_never_tier3_cache(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["tiers"]["tier1"]["nodes"] = [
        {
            "name": "configured-entry",
            "protocol": "ss",
            "address": "entry.example",
            "port": 8388,
            "enabled": True,
        }
    ]
    config["tiers"]["tier2"]["subscription_urls"] = [TIER2_URL]
    config["tiers"]["exit"]["subscription_urls"] = [EXIT_URL]
    tier2 = _node("cached-tier2", tier="tier2", source=TIER2_URL)
    gateway = _node("cached-gateway", tier="exit", source=EXIT_URL)
    tier3 = _node("unverified-tier3", tier="tier3", source=TIER3_URL)
    _write_cache(tmp_path / "tier2-cache.json", tier="tier2", url=TIER2_URL, nodes=[tier2])
    _write_cache(tmp_path / "exit-cache.json", tier="exit", url=EXIT_URL, nodes=[gateway])
    _write_cache(tmp_path / "tier3-cache.json", tier="tier3", url=TIER3_URL, nodes=[tier3])
    service = _DiagnosticService()
    runtime = _runtime(tmp_path, service, manager=None, config=config)
    runtime.core_runner = None
    runtime.api_client = None

    result = runtime.diagnose_node(
        diagnostic_node_identity(gateway),
        request_id="stopped-chain",
        probe_mode="routed",
    )

    assert result["state"] == "success"
    call = service.calls[0]
    assert call["mode"] == "chain"
    assert [node.tier for node in call["upstream_nodes"]] == ["tier1", "tier2"]
    assert call["upstream_nodes"][0].name.startswith("configured-entry")
    assert call["upstream_nodes"][1].name == "cached-tier2"
    assert all(node.name != "unverified-tier3" for node in call["upstream_nodes"])


def test_running_manager_chain_is_deep_copied_and_commits_only_target_health(
    tmp_path: Path,
) -> None:
    entry = _node("entry", tier="tier1")
    sentinel = _node("sentinel", tier="tier2")
    bastion = _node("bastion", tier="tier3")
    gateway = _node("gateway", tier="exit")
    manager = _Manager(
        {
            "tier1": [entry],
            "tier2": [sentinel],
            "tier3": [bastion],
            "exit": [gateway],
        }
    )

    def mutate_captured_snapshots(node: Node, call: dict[str, Any]) -> None:
        node.address = "mutated-target.example"
        node.extra["diagnostic_only"] = True
        for upstream in call["upstream_nodes"]:
            upstream.address = "mutated-upstream.example"

    service = _DiagnosticService(on_diagnose=mutate_captured_snapshots)
    runtime = _runtime(tmp_path, service, manager=manager)
    manager_before = _manager_state(manager)
    formal_before = _formal_calls(runtime)
    original_gateway_fingerprint = node_config_fingerprint(gateway)

    result = runtime.diagnose_node(
        diagnostic_node_identity(gateway),
        timeout_ms=1600,
        request_id="running-chain",
        probe_mode="chain",
    )

    assert result["state"] == "success"
    assert result["config_fingerprint"] == original_gateway_fingerprint
    call = service.calls[0]
    assert [(node.tier, node.name) for node in call["upstream_nodes"]] == [
        ("tier1", "entry"),
        ("tier2", "sentinel"),
        ("tier3", "bastion"),
    ]
    assert call["timeout_ms"] == 1600
    assert call["excluded_ports"] == {7890, 9090}
    assert call["node"] is not manager.runtime_nodes["exit"][0]
    assert all(
        captured is not manager.runtime_nodes[captured.tier][0]
        for captured in call["upstream_nodes"]
    )
    assert _manager_state(manager)["runtime_nodes"]["tier1"] == manager_before["runtime_nodes"]["tier1"]
    assert _manager_state(manager)["runtime_nodes"]["tier2"] == manager_before["runtime_nodes"]["tier2"]
    assert _manager_state(manager)["runtime_nodes"]["tier3"] == manager_before["runtime_nodes"]["tier3"]
    committed_gateway = manager.runtime_nodes["exit"][0]
    assert committed_gateway.address == "gateway.example"
    assert committed_gateway.extra == {}
    assert committed_gateway.status == NodeStatus.ALIVE
    assert committed_gateway.latency_ms == 86.0
    assert committed_gateway.last_checked_at == "2026-08-02T00:00:02+00:00"
    assert manager.last_check_at["exit"] == committed_gateway.last_checked_at
    assert len(manager.replacement_calls) == 1
    assert manager.replacement_calls[0]["mark_layer_check"] is True
    assert manager.evaluate_calls == 1
    assert _formal_calls(runtime) == formal_before
    runtime._tier3_nodes_for_core_export.assert_not_called()


def test_running_inventory_is_a_pure_runtime_nodes_copy_and_never_prunes_route_overlay(
    tmp_path: Path,
) -> None:
    bastion = _node("bastion", tier="tier3")
    manager = _Manager({"tier3": [bastion]})
    manager.get_nodes_for_subscription = MagicMock(
        side_effect=AssertionError("manual diagnostics must not call Manager getters")
    )
    runtime = _runtime(tmp_path, _DiagnosticService(), manager=manager)
    runtime._tier3_nodes_for_core_export.side_effect = AssertionError(
        "manual diagnostics must not call Core export helpers"
    )
    runtime._runtime_audit_export_overlay_lock = threading.RLock()
    runtime._runtime_audit_export_excluded_identities = {
        "formal-route": {
            "expires_at_monotonic": 0.0,
            "release_on_healthy": True,
        }
    }
    overlay_before = copy.deepcopy(runtime._runtime_audit_export_excluded_identities)

    inventory = runtime._node_diagnostic_inventory_snapshot()

    assert [node.name for node in inventory["tier3"]] == ["bastion"]
    assert inventory["tier3"][0] is not bastion
    assert runtime._runtime_audit_export_excluded_identities == overlay_before
    manager.get_nodes_for_subscription.assert_not_called()
    runtime._tier3_nodes_for_core_export.assert_not_called()


def test_runtime_passes_custom_formal_core_ports_to_isolated_diagnostic(
    tmp_path: Path,
) -> None:
    target = _node("target", tier="tier2")
    manager = _Manager({"tier2": [target]})
    service = _DiagnosticService()
    config = _config(tmp_path)
    config["client"]["mihomo_mixed_port"] = 51890
    config["client"]["mihomo_controller"] = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 51990,
    }
    runtime = _runtime(tmp_path, service, manager=manager, config=config)

    result = runtime.diagnose_node(
        diagnostic_node_identity(target),
        request_id="custom-formal-ports",
    )

    assert result["state"] == "success"
    assert service.calls[0]["excluded_ports"] == {51890, 51990}


def test_success_is_discarded_when_target_configuration_changes_during_request(
    tmp_path: Path,
) -> None:
    gateway = _node("gateway", tier="exit")
    manager = _Manager({"exit": [gateway]})

    def edit_formal_inventory(_node: Node, _call: dict[str, Any]) -> None:
        with manager.lock:
            manager.runtime_nodes["exit"][0].address = "edited.example"

    service = _DiagnosticService(on_diagnose=edit_formal_inventory)
    runtime = _runtime(tmp_path, service, manager=manager)

    result = runtime.diagnose_node(
        diagnostic_node_identity(gateway),
        request_id="fingerprint-change",
    )

    assert result["state"] == "failed"
    assert result["reason"] == "node_changed"
    assert result["latency_ms"] is None
    assert result["request_id"] == "fingerprint-change"
    assert manager.replacement_calls == []
    assert manager.evaluate_calls == 0


def test_health_only_formal_change_does_not_invalidate_later_manual_commit(
    tmp_path: Path,
) -> None:
    gateway = _node("gateway", tier="exit")
    manager = _Manager({"exit": [gateway]})

    def commit_health_only_change(_node: Node, _call: dict[str, Any]) -> None:
        with manager.lock:
            checked = manager.runtime_nodes["exit"][0].clone()
            checked.status = NodeStatus.DEAD
            checked.latency_ms = None
            checked.last_checked_at = "2026-08-02T01:00:00+00:00"
            checked.suspicious_reason = "formal-timeout"
            manager.runtime_nodes["exit"][0] = checked
            manager.last_check_at["exit"] = checked.last_checked_at

    service = _DiagnosticService(on_diagnose=commit_health_only_change)
    runtime = _runtime(tmp_path, service, manager=manager)

    result = runtime.diagnose_node(
        diagnostic_node_identity(gateway),
        request_id="health-only-change",
    )

    assert result["state"] == "success"
    assert result["latency_ms"] == 86.0
    assert result["status"] == "alive"
    assert result["status_applied"] is True
    assert manager.runtime_nodes["exit"][0].status == NodeStatus.ALIVE
    assert manager.runtime_nodes["exit"][0].latency_ms == 86.0
    assert manager.last_check_at["exit"] == "2026-08-02T00:00:02+00:00"
    assert len(manager.replacement_calls) == 1
    assert manager.evaluate_calls == 1


def test_runtime_cancel_and_close_delegate_only_to_ephemeral_service(tmp_path: Path) -> None:
    service = _DiagnosticService()
    runtime = _runtime(tmp_path, service, manager=None)
    formal_before = _formal_calls(runtime)

    assert runtime.cancel_node_diagnostic("active-request") is True
    assert runtime.cancel_node_diagnostic("unknown-request") is False
    assert runtime.cancel_all_node_diagnostics() == 1
    assert runtime.close_node_diagnostics(timeout=1.75) is True

    assert service.cancel_calls == ["active-request", "unknown-request"]
    assert service.cancel_all_calls == 1
    assert service.close_calls == [1.75]
    assert _formal_calls(runtime) == formal_before


def test_only_success_terminal_result_updates_manager_health_not_core_or_files(
    tmp_path: Path,
) -> None:
    gateway = _node("gateway", tier="exit")
    scenarios = (
        ("success", "ok", 72.0),
        ("failed", "timeout", None),
        ("failed", "probe_failed", None),
        ("cancelled", "cancelled", None),
    )
    for state, reason, latency_ms in scenarios:
        manager = _Manager({"exit": [gateway.clone()]})
        service = _DiagnosticService(
            state=state,
            reason=reason,
            latency_ms=latency_ms,
        )
        runtime = _runtime(tmp_path, service, manager=manager)
        cache_path = tmp_path / f"formal-cache-{reason}.json"
        runtime_path = tmp_path / f"formal-runtime-{reason}.yaml"
        cache_path.write_bytes(b'{"formal":"cache"}')
        runtime_path.write_bytes(b"proxies: []\n")
        files_before = {
            cache_path: cache_path.read_bytes(),
            runtime_path: runtime_path.read_bytes(),
        }
        manager_before = _manager_state(manager)
        formal_before = _formal_calls(runtime)

        result = runtime.diagnose_node(
            diagnostic_node_identity(gateway),
            request_id=f"terminal-{reason}",
        )

        assert result["state"] == state
        assert result["reason"] == reason
        assert result["latency_ms"] == latency_ms
        if state == "success":
            committed_gateway = manager.runtime_nodes["exit"][0]
            assert committed_gateway.status == NodeStatus.ALIVE
            assert committed_gateway.latency_ms == 72.0
            assert committed_gateway.last_checked_at == "2026-08-02T00:00:02+00:00"
            assert len(manager.replacement_calls) == 1
            assert manager.evaluate_calls == 1
        else:
            assert _manager_state(manager) == manager_before
            assert manager.replacement_calls == []
            assert manager.evaluate_calls == 0
        assert _formal_calls(runtime) == formal_before
        assert {path: path.read_bytes() for path in files_before} == files_before


def test_manual_probe_runs_before_operation_lock_but_health_commit_waits(tmp_path: Path) -> None:
    gateway = _node("gateway", tier="exit")
    manager = _Manager({"exit": [gateway]})
    probe_finished = threading.Event()
    service = _DiagnosticService(
        on_diagnose=lambda _node, _call: probe_finished.set()
    )
    runtime = _runtime(tmp_path, service, manager=manager)
    formal_lock_entered = threading.Event()
    release_formal_lock = threading.Event()
    diagnostic_finished = threading.Event()
    failures: list[BaseException] = []

    def hold_formal_lock() -> None:
        with manager.operation_lock:
            formal_lock_entered.set()
            release_formal_lock.wait(timeout=2.0)

    def run_diagnostic() -> None:
        try:
            runtime.diagnose_node(
                diagnostic_node_identity(gateway),
                request_id="outside-formal-lock",
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            diagnostic_finished.set()

    formal_worker = threading.Thread(target=hold_formal_lock)
    diagnostic_worker = threading.Thread(target=run_diagnostic)
    formal_worker.start()
    assert formal_lock_entered.wait(timeout=1.0)
    diagnostic_worker.start()
    try:
        assert probe_finished.wait(timeout=1.0)
        assert not diagnostic_finished.wait(timeout=0.1)
    finally:
        release_formal_lock.set()
        formal_worker.join(timeout=1.0)
        diagnostic_worker.join(timeout=1.0)

    assert failures == []
    assert diagnostic_finished.is_set()
    assert len(service.calls) == 1
    assert len(manager.replacement_calls) == 1
    assert manager.evaluate_calls == 1
