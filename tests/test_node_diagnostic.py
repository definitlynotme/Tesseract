from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from clash_formatter import build_full_clash_config
from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus
from node_diagnostic import (
    NodeDiagnosticCoreBinding,
    build_node_diagnostic_core_binding,
    diagnose_node_snapshot,
    diagnostic_node_identity,
    node_config_fingerprint,
)


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _CoreRunner:
    def __init__(self, running: bool) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running


class _MihomoClient:
    def __init__(
        self,
        *,
        proxies: dict | None = None,
        delay: dict | None = None,
        proxy_error: Exception | None = None,
        delay_error: Exception | None = None,
        on_call=None,
    ) -> None:
        self.proxy_payload = proxies if proxies is not None else {"proxies": {}}
        self.delay_payload = delay if delay is not None else {"delay": 42}
        self.proxy_error = proxy_error
        self.delay_error = delay_error
        self.on_call = on_call
        self.calls: list[tuple] = []

    def proxies(self) -> dict:
        self.calls.append(("proxies",))
        if self.on_call:
            self.on_call("proxies")
        if self.proxy_error:
            raise self.proxy_error
        return self.proxy_payload

    def proxy_delay(self, name: str, *, url: str, timeout_ms: int = 5000) -> dict:
        self.calls.append(("proxy_delay", name, url, timeout_ms))
        if self.on_call:
            self.on_call("proxy_delay")
        if self.delay_error:
            raise self.delay_error
        return self.delay_payload


class _Manager:
    def __init__(self, nodes: list[Node]) -> None:
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.runtime_nodes = {"tier1": list(nodes), "tier2": [], "tier3": [], "exit": []}
        self.last_check_at = {"tier1": "before", "tier2": None, "tier3": None, "exit": None}
        self.background_actions = {"test_node": {"running": False, "result": None}}
        self.selected_exit = "gateway-before"
        self.observations = {"count": 7}
        self.evaluate_state_calls = 0

    def evaluate_state(self) -> None:
        self.evaluate_state_calls += 1

    def commit_formal_check(self, *, latency_ms: float = 18.0) -> None:
        with self.operation_lock:
            with self.lock:
                checked = copy.deepcopy(self.runtime_nodes["tier1"][0])
                checked.status = NodeStatus.ALIVE
                checked.latency_ms = latency_ms
                checked.last_checked_at = "2026-07-18T01:00:00+00:00"
                self.runtime_nodes["tier1"][0] = checked
                self.last_check_at["tier1"] = checked.last_checked_at
                self.background_actions["formal_check"] = {
                    "running": False,
                    "result": "committed",
                }


def _node(*, name: str = "alpha", address: str = "alpha.example", port: int = 8388) -> Node:
    return Node(
        name=name,
        protocol="ss",
        address=address,
        port=port,
        tier="tier1",
        method="aes-256-gcm",
        password="secret",
        status=NodeStatus.SLOW,
        latency_ms=999,
        last_checked_at="2026-07-18T00:00:00+00:00",
    )


def _binding_for_nodes(
    nodes: list[Node],
    *,
    generation: int = 1,
) -> NodeDiagnosticCoreBinding:
    tiers = {"tier1": [], "tier2": [], "tier3": [], "exit": []}
    for node in nodes:
        tiers.setdefault(node.tier, []).append(copy.deepcopy(node))
    runtime_config = build_full_clash_config(tiers)
    return build_node_diagnostic_core_binding(runtime_config, generation=generation)


def _manager_state(manager: _Manager) -> dict:
    return copy.deepcopy(
        {
            "nodes": [[item.to_dict() for item in values] for values in manager.runtime_nodes.values()],
            "last_check_at": manager.last_check_at,
            "background_actions": manager.background_actions,
            "selected_exit": manager.selected_exit,
            "observations": manager.observations,
            "evaluate_state_calls": manager.evaluate_state_calls,
        }
    )


class NodeDiagnosticTests(unittest.TestCase):
    def test_loaded_core_binding_records_exact_proxy_protocol_counts(self) -> None:
        binding = build_node_diagnostic_core_binding(
            """
proxies:
  - {name: vless-a, type: VLESS}
  - {name: vless-b, type: vless}
  - {name: ss-a, type: ss}
  - {name: missing-type}
""",
            generation=4,
            core_generation=9,
        )

        self.assertEqual(binding.generation, 4)
        self.assertEqual(binding.core_generation, 9)
        self.assertEqual(binding.protocol_counts(), {"ss": 1, "vless": 2})

    def test_stopped_core_makes_only_proxy_probe_unavailable(self) -> None:
        node = _node()
        client = _MihomoClient(proxy_error=AssertionError("controller should not be called"))
        result = diagnose_node_snapshot(
            node.clone(),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[node.clone()],
            core_running=False,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            _tcp_connector=lambda _target, _timeout: _Socket(),
            _monotonic=iter([1.0, 1.01]).__next__,
        )

        self.assertEqual(result["probes"]["tcp_connect"]["state"], "success")
        self.assertEqual(result["probes"]["mihomo_proxy_http"]["state"], "unavailable")
        self.assertEqual(client.calls, [])

    def test_direct_mode_runs_only_the_endpoint_probe(self) -> None:
        node = _node()
        client = _MihomoClient(
            proxy_error=AssertionError("direct mode must not call the controller")
        )
        result = diagnose_node_snapshot(
            node.clone(),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[node.clone()],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            probe_mode="direct",
            _tcp_connector=lambda _target, _timeout: _Socket(),
            _monotonic=iter([1.0, 1.025]).__next__,
        )

        self.assertEqual(set(result["probes"]), {"tcp_connect"})
        self.assertEqual(result["probes"]["tcp_connect"]["latency_ms"], 25.0)
        self.assertEqual(client.calls, [])

    def test_routed_mode_runs_only_the_loaded_proxy_probe(self) -> None:
        node = _node()
        client = _MihomoClient(
            proxies={"proxies": {"alpha": {"type": "Shadowsocks"}}},
            delay={"delay": 41},
        )
        result = diagnose_node_snapshot(
            node.clone(),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[node.clone()],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            probe_mode="routed",
            core_binding=_binding_for_nodes([node]),
            _tcp_connector=lambda _target, _timeout: (_ for _ in ()).throw(
                AssertionError("routed mode must not open a direct socket")
            ),
        )

        self.assertEqual(set(result["probes"]), {"mihomo_proxy_http"})
        self.assertEqual(
            result["probes"]["mihomo_proxy_http"]["latency_ms"],
            41.0,
        )
        self.assertEqual(
            client.calls,
            [
                ("proxies",),
                (
                    "proxy_delay",
                    "alpha",
                    "https://probe.example/204",
                    5000,
                ),
            ],
        )

    def test_tcp_failure_does_not_prevent_proxy_delay_success(self) -> None:
        node = _node()
        client = _MihomoClient(proxies={"proxies": {"alpha": {"type": "Shadowsocks"}}})

        def fail_connect(_target, _timeout):
            raise TimeoutError("tcp timed out")

        result = diagnose_node_snapshot(
            node.clone(),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[node.clone()],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            core_binding=_binding_for_nodes([node]),
            _tcp_connector=fail_connect,
            _monotonic=lambda: 1.0,
        )

        self.assertEqual(result["probes"]["tcp_connect"]["state"], "failed")
        self.assertIn("timed out", result["probes"]["tcp_connect"]["error"])
        self.assertEqual(result["probes"]["mihomo_proxy_http"]["state"], "success")

    def test_proxy_missing_from_running_core_is_unavailable_without_delay_call(self) -> None:
        node = _node()
        client = _MihomoClient(proxies={"proxies": {"DIRECT": {"type": "Direct"}}})
        result = diagnose_node_snapshot(
            node.clone(),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[node.clone()],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            core_binding=_binding_for_nodes([node]),
            _tcp_connector=lambda _target, _timeout: _Socket(),
            _monotonic=iter([1.0, 1.01]).__next__,
        )

        self.assertEqual(result["probes"]["mihomo_proxy_http"]["state"], "unavailable")
        self.assertEqual(client.calls, [("proxies",)])

    def test_duplicate_proxy_base_name_is_unavailable_without_controller_call(self) -> None:
        target = _node(name="duplicate", address="one.example")
        other = _node(name="duplicate", address="two.example")
        client = _MihomoClient(proxies={"proxies": {"duplicate": {"type": "Shadowsocks"}}})
        result = diagnose_node_snapshot(
            target.clone(),
            node_identity=diagnostic_node_identity(target),
            runtime_nodes=[target.clone(), other.clone()],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            core_binding=_binding_for_nodes([target, other]),
            _tcp_connector=lambda _target, _timeout: _Socket(),
            _monotonic=iter([1.0, 1.01]).__next__,
        )

        self.assertEqual(result["probes"]["mihomo_proxy_http"]["state"], "unavailable")
        self.assertIn("not unique", result["probes"]["mihomo_proxy_http"]["error"])
        self.assertEqual(client.calls, [])

    def test_proxy_delay_failure_keeps_successful_tcp_fact(self) -> None:
        node = _node()
        client = _MihomoClient(
            proxies={"proxies": {"alpha": {"type": "Shadowsocks"}}},
            delay_error=TimeoutError("proxy probe timed out"),
        )
        result = diagnose_node_snapshot(
            node.clone(),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[node.clone()],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            core_binding=_binding_for_nodes([node]),
            _tcp_connector=lambda _target, _timeout: _Socket(),
            _monotonic=iter([1.0, 1.01]).__next__,
        )

        self.assertEqual(result["probes"]["tcp_connect"]["state"], "success")
        self.assertEqual(result["probes"]["mihomo_proxy_http"]["state"], "failed")
        self.assertIn("timed out", result["probes"]["mihomo_proxy_http"]["error"])

    def test_same_name_core_replacement_discards_completed_proxy_latency(self) -> None:
        node = _node()
        replacement = copy.deepcopy(node)
        replacement.method = "chacha20-ietf-poly1305"
        original_binding = _binding_for_nodes([node], generation=7)
        replacement_binding = _binding_for_nodes([replacement], generation=8)
        binding_state = {"current": original_binding}

        def on_call(stage: str) -> None:
            if stage == "proxy_delay":
                binding_state["current"] = replacement_binding

        def validate(expected: NodeDiagnosticCoreBinding) -> tuple[bool, str | None]:
            if binding_state["current"] != expected:
                return False, "loaded Mihomo configuration binding changed"
            return True, None

        client = _MihomoClient(
            proxies={"proxies": {"alpha": {"type": "Shadowsocks"}}},
            delay={"delay": 31},
            on_call=on_call,
        )
        result = diagnose_node_snapshot(
            copy.deepcopy(node),
            node_identity=diagnostic_node_identity(node),
            runtime_nodes=[copy.deepcopy(node)],
            core_running=True,
            mihomo_client=client,
            probe_url="https://probe.example/204",
            core_binding=original_binding,
            binding_validator=validate,
            _tcp_connector=lambda _target, _timeout: _Socket(),
            _monotonic=iter([1.0, 1.01]).__next__,
        )

        proxy = result["probes"]["mihomo_proxy_http"]
        self.assertEqual(client.calls[-1][0], "proxy_delay")
        self.assertEqual(proxy["state"], "unavailable")
        self.assertIsNone(proxy["latency_ms"])
        self.assertIn("binding changed", proxy["error"])
        self.assertEqual(result["config_fingerprint"], node_config_fingerprint(node))

    def test_runtime_config_change_without_successful_load_invalidates_binding(self) -> None:
        node = _node()
        changed = copy.deepcopy(node)
        changed.method = "chacha20-ietf-poly1305"
        client = _MihomoClient()
        runner = _CoreRunner(True)
        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime.api_client = client
        runtime.core_runner = runner
        runtime._node_diagnostic_binding_lock = threading.RLock()
        runtime._node_diagnostic_binding_generation = 0
        runtime._node_diagnostic_core_binding = None
        runtime._node_diagnostic_binding_client = None
        runtime._node_diagnostic_binding_runner = None

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            config_path = Path(temp_dir) / "mihomo-runtime.yaml"
            config_path.write_text(
                build_full_clash_config({"tier1": [node], "tier2": [], "tier3": [], "exit": []}),
                encoding="utf-8",
            )
            runtime._runtime_config_path_for_client = lambda: config_path
            binding = runtime._refresh_node_diagnostic_core_binding()
            valid, reason = runtime._validate_node_diagnostic_core_binding(
                binding,
                client=client,
                runner=runner,
            )
            self.assertTrue(valid, reason)

            config_path.write_text(
                build_full_clash_config({"tier1": [changed], "tier2": [], "tier3": [], "exit": []}),
                encoding="utf-8",
            )
            valid, reason = runtime._validate_node_diagnostic_core_binding(
                binding,
                client=client,
                runner=runner,
            )

        self.assertFalse(valid)
        self.assertIn("without a successful load", reason)

    def test_binding_refresh_failure_never_breaks_regular_sync_success_marker(self) -> None:
        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime._core_export_sync_marker = MagicMock(return_value=("stable",))
        runtime._queue_node_diagnostic_core_binding_refresh = MagicMock(
            side_effect=ValueError("diagnostic binding parse failed")
        )
        runtime._invalidate_node_diagnostic_core_binding = MagicMock()
        runtime.logger = MagicMock()

        runtime._mark_core_export_sync_success()

        self.assertEqual(runtime._last_core_export_sync_marker, ("stable",))
        self.assertIsNone(runtime._last_failed_core_export_sync_marker)
        self.assertEqual(runtime._last_failed_core_export_sync_at, 0.0)
        runtime._invalidate_node_diagnostic_core_binding.assert_called_once_with()

    def test_non_finite_mihomo_delay_is_rejected(self) -> None:
        node = _node()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                result = diagnose_node_snapshot(
                    copy.deepcopy(node),
                    node_identity=diagnostic_node_identity(node),
                    runtime_nodes=[copy.deepcopy(node)],
                    core_running=True,
                    mihomo_client=_MihomoClient(
                        proxies={"proxies": {"alpha": {"type": "Shadowsocks"}}},
                        delay={"delay": value},
                    ),
                    probe_url="https://probe.example/204",
                    core_binding=_binding_for_nodes([node]),
                    _tcp_connector=lambda _target, _timeout: _Socket(),
                    _monotonic=iter([1.0, 1.01]).__next__,
                )
                proxy = result["probes"]["mihomo_proxy_http"]
                self.assertEqual(proxy["state"], "failed")
                self.assertIsNone(proxy["latency_ms"])

    def test_config_fingerprint_ignores_health_results_but_changes_with_config(self) -> None:
        original = _node()
        checked = original.clone()
        checked.status = NodeStatus.DEAD
        checked.latency_ms = None
        checked.last_checked_at = "later"
        checked.suspicious_reason = "timeout"
        checked.extra["runtime_audit_failure_at"] = "later"
        checked.extra["tier3_consecutive_failures"] = 12
        edited = original.clone()
        edited.address = "edited.example"

        self.assertEqual(node_config_fingerprint(original), node_config_fingerprint(checked))
        self.assertNotEqual(node_config_fingerprint(original), node_config_fingerprint(edited))


if __name__ == "__main__":
    unittest.main()
