import copy
import json
import logging
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from alerts import flush_logging_handlers
from clash_formatter import (
    build_bootstrap_clash_config,
    build_capture_disabled_clash_config,
    build_full_clash_config,
)
from core_runner import CoreOutputEvent
from desktop_runtime import (
    DesktopRuntime,
    ProviderSyncDisposition,
    RecoverableCoreStartError,
    RuntimeActivationRecoveryError,
    RuntimeSourceListenerReadinessError,
    RuntimeStartCancelled,
    _RuntimeRecoveryTicket,
)
from models import Node, NodeStatus
from node_diagnostic import build_node_diagnostic_core_binding
from runtime_state import RuntimeStateStore
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressReadiness,
    RuntimeEgressReadinessState,
    RuntimeNodeRef,
    RuntimeRecoveryOutcome,
    RuntimeRecoveryStatus,
    RuntimeRouteBinding,
)
from runtime_manual_validation import (
    ManualDataPlaneProbeResult,
    ManualProbeFailureKind,
)
from runtime_reload_gate import RuntimeReloadReason
from runtime_source_transport import RuntimeSourceRouteKind
from runtime_tier_state import (
    Tier3RuntimeQualificationSnapshot,
    tier3_export_node_signature,
)
from system_proxy import ProxySettings, SystemProxyManager


@pytest.fixture(autouse=True)
def _retry_tempdir_cleanup_while_background_writes_finish(monkeypatch):
    """A runtime under test can still be writing into its temporary data
    directory from a background thread when the test's ``with
    TemporaryDirectory()`` block ends. On a slow machine (the release runner)
    removing the directory then fails with WinError 145 ("not empty") or 32
    ("in use") although every assertion passed. Retry the removal for a
    bounded time; on a fast machine the first attempt succeeds."""
    original_cleanup = tempfile.TemporaryDirectory.cleanup

    def cleanup(self):
        deadline = time.monotonic() + 15.0
        while True:
            try:
                return original_cleanup(self)
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (32, 145) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", cleanup)


class MemoryProxyBackend:
    def __init__(self, settings: ProxySettings) -> None:
        self.settings = settings

    def read(self) -> ProxySettings:
        return ProxySettings(
            enabled=self.settings.enabled,
            server=self.settings.server,
            override=self.settings.override,
        )

    def write(self, settings: ProxySettings) -> None:
        self.settings = settings


class FakeCoreRunner:
    def __init__(self, *, running: bool, port_ok: bool) -> None:
        self.running = running
        self.port_ok = port_ok
        self.status_payload = {"running": running, "process_running": running, "proxy_ready": port_ok}

    def is_running(self) -> bool:
        return self.running

    def check_proxy_port(self, host="127.0.0.1", port=7890):
        return self.port_ok

    def status(self):
        return dict(self.status_payload)

    def get_recent_logs(self, limit=100):
        return []

    def stop(self):
        self.running = False
        self.status_payload = {"running": False, "process_running": False, "proxy_ready": False}
        return self.status()


class FakeLifecycleCoreRunner(FakeCoreRunner):
    def __init__(self) -> None:
        super().__init__(running=False, port_ok=True)
        self.started = False
        self.stopped = False
        self.hot_reloaded = 0

    def get_version(self):
        return "Mihomo test"

    def start(self):
        self.started = True
        self.running = True
        self.status_payload = {"running": True, "process_running": True, "proxy_ready": True}
        return self.status()

    def wait_for_proxy(self, host="127.0.0.1", port=7890, timeout=30):
        return True

    def stop(self):
        self.stopped = True
        self.running = False
        self.status_payload = {"running": False, "process_running": False, "proxy_ready": False}
        return self.status()

    def hot_reload(self, **_kwargs):
        self.hot_reloaded += 1
        return {"reloaded": True}


class FakeTrafficMonitor:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def snapshot(self):
        return {
            "supported": True,
            "running": self.started > self.stopped,
            "latest": None,
            "history": [],
            "session": {"upload_bytes": 0, "download_bytes": 0},
            "session_totals": {"upload_bytes": 0, "download_bytes": 0},
            "error": None,
        }


class FakeGeoUpdater:
    def __init__(self, work_dir: Path, *, create_assets: bool = True) -> None:
        self.work_dir = work_dir
        self.create_assets = create_assets
        self.update_calls = 0
        self.update_options: list[dict[str, object]] = []
        self.reconfigure_calls: list[dict[str, object]] = []

    def reconfigure(self, config, *, egress_proxy=None) -> None:
        self.reconfigure_calls.append(
            {
                "config": dict(config or {}),
                "egress_proxy": dict(egress_proxy or {}),
            }
        )

    def update_now(self, *, force: bool = True, policy=None, cancel=None, assets=None):
        self.update_calls += 1
        self.update_options.append({"policy": policy, "cancel": cancel, "assets": assets})
        if self.create_assets:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "geoip.dat").write_bytes(b"geoip")
            (self.work_dir / "geosite.dat").write_bytes(b"geosite")
        return {"errors": {}}

    def status(self):
        return {"enabled": True}


class FakeTier3Manager:
    def __init__(self, *, collect_alive: bool = True) -> None:
        self.collect_alive = collect_alive
        self.runtime_nodes = {"tier1": [], "tier2": [], "tier3": []}
        self.tier3_last_collection_at = None
        self.tier3_source_count = 0
        self.tier3_last_collection_report = {}
        self.refreshed = 0
        self.check_calls: list[bool] = []
        self.lock = threading.RLock()
        self.last_collect_tier3 = 0.0
        self.min_alive_nodes = 1
        self.background_target_alive = None
        self.primary_check_alive = False
        self.primary_check_calls: list[str] = []
        self.core_running_marks: list[dict[str, object]] = []
        self.tier3_core_running = False
        self.effective_egress_ok = None
        self.effective_states: list[tuple[object, str]] = []
        self.targeted_rechecks: list[dict[str, str]] = []
        self.runtime_circuit_updates: list[tuple[dict[str, object], int]] = []
        self.runtime_generation = 1
        self.runtime_observations: list[dict[str, object]] = []
        self.runtime_leaf_rechecks: list[dict[str, object]] = []
        self._runtime_unusable_refs: set[tuple[str, str, str, str]] = set()

    def _tier3_enabled(self) -> bool:
        return True

    def _tier3_min_alive_nodes(self) -> int:
        return self.min_alive_nodes

    def _tier3_background_target_alive_nodes(self) -> int:
        return self.background_target_alive or self.min_alive_nodes

    def refresh_tier3(self, *, suppress_intervention_alert: bool = False, startup_mode: bool = False):
        self.refreshed += 1
        self.tier3_last_collection_at = f"collect-{self.refreshed}"
        self.tier3_source_count = 2
        if not self.collect_alive:
            self.runtime_nodes["tier3"] = []
            return [], 0
        node = Node(
            name="bootstrap",
            protocol="ss",
            uri="ss://bootstrap",
            address="bootstrap.example.com",
            port=8388,
            tier="tier3",
            status=NodeStatus.ALIVE,
            method="aes-256-gcm",
            password="pass",
        )
        self.runtime_nodes["tier3"] = [node]
        return [node], 1

    def check_tier3(self, force_collect: bool = False, *, suppress_intervention_alert: bool = False):
        self.check_calls.append(bool(force_collect))
        if force_collect or not self.runtime_nodes["tier3"]:
            nodes, _alive = self.refresh_tier3(suppress_intervention_alert=suppress_intervention_alert)
            return nodes
        return list(self.runtime_nodes["tier3"])

    def check_tier1(self):
        self.primary_check_calls.append("tier1")
        if self.primary_check_alive:
            node = Node(
                name="primary",
                protocol="ss",
                uri="ss://primary",
                address="primary.example.com",
                port=8388,
                tier="tier1",
                status=NodeStatus.ALIVE,
            )
            self.runtime_nodes["tier1"] = [node]
        return list(self.runtime_nodes["tier1"])

    def check_tier2(self, force_remote_refresh: bool = False):
        self.primary_check_calls.append("tier2")
        return list(self.runtime_nodes["tier2"])

    def get_nodes_for_subscription(self, tier_id: str, limit: int | None = None):
        nodes = [
            node
            for node in self.runtime_nodes.get(tier_id, [])
            if node.status in {NodeStatus.ALIVE, NodeStatus.SLOW}
        ]
        return nodes[:limit] if limit else nodes

    def _node_identity(self, node: Node) -> str:
        return "|".join(map(str, node.dedupe_key))

    def _runtime_pool_kind(self, tier_id: str, node: Node) -> str:
        if tier_id == "tier1":
            return "tier1_variant"
        if tier_id == "tier2":
            return "tier2"
        if tier_id == "exit":
            return "gateway"
        if tier_id == "tier3":
            return "tier3b" if self._is_tier3_auto_pool_node(node) else "tier3a"
        return tier_id

    @staticmethod
    def _runtime_ref_payload(value) -> dict[str, object]:
        if isinstance(value, dict):
            payload = dict(value)
            nested = payload.get("node")
            if nested is not None:
                payload.update(FakeTier3Manager._runtime_ref_payload(nested))
            return payload
        return {
            key: getattr(value, key)
            for key in ("tier", "pool_kind", "identity", "proxy_name")
            if hasattr(value, key)
        }

    def _runtime_ref_key(self, payload: dict[str, object]) -> tuple[str, str, str, str]:
        return (
            str(payload.get("tier") or ""),
            str(payload.get("pool_kind") or ""),
            str(payload.get("identity") or ""),
            str(payload.get("proxy_name") or ""),
        )

    def _is_tier3_auto_pool_node(self, node: Node) -> bool:
        # Historical fixtures without an explicit origin represent the
        # admitted automatic pool. Only an explicit user-pool marker is 3A.
        origin = str(getattr(node, "extra", {}).get("tier3_origin") or "")
        return origin not in {"tier3a", "user"}

    def apply_runtime_health_observation(self, observation) -> dict[str, object]:
        payload = self._runtime_ref_payload(observation)
        if isinstance(observation, dict):
            payload.update(observation)
            nested = observation.get("node")
            if nested is not None:
                payload.update(self._runtime_ref_payload(nested))
        self.runtime_observations.append(dict(payload))
        key = self._runtime_ref_key(payload)
        verdict = str(payload.get("verdict") or "").casefold()
        if verdict in {"confirmed", "quarantined", "suspicious"}:
            self._runtime_unusable_refs.add(key)
            return {
                "accepted": True,
                "changed": True,
                "action": "quarantined",
                "usable": False,
                "effective_status": NodeStatus.SUSPICIOUS.value,
            }
        if verdict in {"pass", "alive", "recovered"} and key in self._runtime_unusable_refs:
            self._runtime_unusable_refs.remove(key)
            return {
                "accepted": True,
                "changed": True,
                "action": "recovered",
                "usable": True,
                "effective_status": NodeStatus.ALIVE.value,
            }
        return {"accepted": False, "changed": False, "action": "stale"}

    def enqueue_runtime_leaf_recheck(self, node_ref, **kwargs) -> dict[str, object]:
        payload = self._runtime_ref_payload(node_ref)
        payload.update(kwargs)
        self.runtime_leaf_rechecks.append(payload)
        return {
            "accepted": True,
            "action": "runtime_leaf_recheck",
            "status": "delegated" if payload.get("pool_kind") == "tier3b" else "started",
        }

    def runtime_effective_nodes(
        self,
        tier_id: str,
        nodes: list[Node] | None = None,
    ) -> list[Node]:
        effective: list[Node] = []
        for node in list(self.runtime_nodes.get(tier_id, []) if nodes is None else nodes):
            clone = node.clone()
            key = (
                tier_id,
                self._runtime_pool_kind(tier_id, node),
                self._node_identity(node),
                str(node.name or ""),
            )
            if key in self._runtime_unusable_refs:
                clone.status = NodeStatus.SUSPICIOUS
                clone.latency_ms = None
                clone.suspicious_reason = "runtime_transport_failure"
            effective.append(clone)
        return effective

    def mark_runtime_audit_node_failure(self, tier_id: str, node_identity: str, *, reason: str, marker: str):
        for index, node in enumerate(self.runtime_nodes.get(tier_id, [])):
            if self._node_identity(node) != node_identity:
                continue
            updated = node.clone()
            updated.status = NodeStatus.SUSPICIOUS
            updated.latency_ms = None
            updated.suspicious_reason = reason
            updated.extra["runtime_audit_failure_marker"] = marker
            self.runtime_nodes[tier_id][index] = updated
            return updated
        raise KeyError("node not found")

    def enqueue_tier3_targeted_recheck(self, node_identity: str, *, reason: str, source: str = "runtime"):
        self.targeted_rechecks.append(
            {
                "identity": node_identity,
                "reason": reason,
                "source": source,
            }
        )
        return {"accepted": True, "action": "tier3_targeted_recheck", "status": "started"}

    def update_tier3_runtime_circuit(
        self,
        snapshot,
        *,
        unavailable_healthy_count: int = 0,
    ) -> None:
        serializer = getattr(snapshot, "to_dict", None)
        payload = serializer() if callable(serializer) else dict(snapshot or {})
        self.runtime_circuit_updates.append(
            (dict(payload), int(unavailable_healthy_count))
        )

    def set_effective_egress_state(self, ok, reason: str = "") -> None:
        self.effective_egress_ok = ok
        self.effective_states.append((ok, reason))
        self.tier3_last_collection_report["effective_egress_ok"] = ok
        if self.tier3_core_running:
            self.tier3_last_collection_report["core_standby"] = ok is not True

    def mark_tier3_core_running(
        self,
        *,
        core_gate_latency_ms=None,
        effective_egress_ok=True,
        traffic_capture_active=None,
    ):
        self.tier3_core_running = True
        self.effective_egress_ok = effective_egress_ok
        if core_gate_latency_ms is None:
            core_gate_latency_ms = 0
        self.core_running_marks.append(
            {
                "core_gate_latency_ms": core_gate_latency_ms,
                "effective_egress_ok": effective_egress_ok,
                "traffic_capture_active": traffic_capture_active,
            }
        )
        self.tier3_last_collection_report["startup_mode"] = False
        self.tier3_last_collection_report["startup_gate_satisfied"] = True
        self.tier3_last_collection_report["core_running"] = True
        self.tier3_last_collection_report["effective_egress_ok"] = effective_egress_ok
        self.tier3_last_collection_report["traffic_capture_active"] = bool(
            effective_egress_ok is True
            if traffic_capture_active is None
            else traffic_capture_active
        )
        if core_gate_latency_ms is not None:
            self.tier3_last_collection_report["core_gate_latency_ms"] = core_gate_latency_ms
        return core_gate_latency_ms


class FakeRuntimeAuditManager:
    def __init__(self) -> None:
        self.runtime_nodes = {"tier1": [], "tier2": [], "tier3": [], "exit": []}
        self.lock = threading.RLock()
        self.fail_identities: set[str] = set()
        self.marked_failures: list[tuple[str, str, str]] = []
        self.effective_states: list[tuple[object, str]] = []
        self.targeted_rechecks: list[dict[str, str]] = []
        self.exit_active = False

    def _node_identity(self, node: Node) -> str:
        return f"{node.tier}|{node.name}|{node.protocol}|{node.address}|{node.port}"

    def _exit_active(self) -> bool:
        return self.exit_active

    def active_exit_proxy_name(self) -> str:
        return ""

    def get_nodes_for_subscription(self, tier_id: str, limit: int | None = None):
        nodes = list(self.runtime_nodes.get(tier_id, []))
        return nodes[:limit] if limit else nodes

    def _test_runtime_node_by_identity(
        self,
        tier_id: str,
        node_identity: str,
        *,
        mark_layer_check: bool = True,
        persist_result: bool = True,
    ) -> Node:
        for index, node in enumerate(self.runtime_nodes.get(tier_id, [])):
            if self._node_identity(node) != node_identity:
                continue
            updated = node.clone()
            if node_identity in self.fail_identities:
                updated.status = NodeStatus.SUSPICIOUS
                updated.suspicious_reason = "targeted failure"
            else:
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 10
                updated.suspicious_reason = None
            if persist_result:
                self.runtime_nodes[tier_id][index] = updated
            return updated
        raise KeyError("node not found")

    def mark_runtime_audit_node_failure(self, tier_id: str, node_identity: str, *, reason: str, marker: str):
        self.marked_failures.append((tier_id, node_identity, marker))
        for index, node in enumerate(self.runtime_nodes.get(tier_id, [])):
            if self._node_identity(node) == node_identity:
                updated = node.clone()
                updated.status = NodeStatus.SUSPICIOUS
                updated.suspicious_reason = reason
                updated.extra["runtime_audit_failure_marker"] = marker
                self.runtime_nodes[tier_id][index] = updated
                return updated
        raise KeyError("node not found")

    def enqueue_tier3_targeted_recheck(self, node_identity: str, *, reason: str, source: str = "runtime"):
        self.targeted_rechecks.append(
            {
                "identity": node_identity,
                "reason": reason,
                "source": source,
            }
        )
        return {"accepted": True, "action": "tier3_targeted_recheck", "status": "started"}

    def set_effective_egress_state(self, ok, reason: str = "") -> None:
        self.effective_states.append((ok, reason))


class FakeRuntimeAuditClient:
    def __init__(self, proxies_payloads: list[dict[str, object]], *, final_failures: int = 0) -> None:
        self.proxies_payloads = list(proxies_payloads)
        self.final_failures = final_failures
        self.select_calls: list[tuple[str, str]] = []
        self.delay_calls: list[str] = []

    def proxies(self):
        if len(self.proxies_payloads) > 1:
            return self.proxies_payloads.pop(0)
        return self.proxies_payloads[0]

    def proxy_delay(self, name, *, url=None, timeout_ms=5000):
        self.delay_calls.append(str(name))
        if name == "FINAL-EXIT" and self.final_failures > 0:
            self.final_failures -= 1
            raise RuntimeError("final still failed")
        return {"delay": 12, "name": name}

    def select_proxy(self, group_name: str, proxy_name: str):
        self.select_calls.append((group_name, proxy_name))
        return {"group": group_name, "selected": proxy_name}


class DesktopRuntimeRecoveryTests(unittest.TestCase):
    def _runtime(self, temp_path: Path, backend: MemoryProxyBackend) -> DesktopRuntime:
        config = {
            "client": {
                "runtime_state_path": str(temp_path / "runtime-state.json"),
                "system_proxy": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 7890,
                    "health_failures_before_restore": 2,
                },
            },
            "alerts": {"log_file": str(temp_path / "monitor.log"), "desktop": {"enabled": False}},
        }
        runtime = DesktopRuntime(config, project_root=temp_path)
        for handler in list(runtime.logger.handlers):
            handler.close()
            runtime.logger.removeHandler(handler)
        runtime.logger = logging.getLogger("desktop-runtime-recovery-test")
        runtime.logger.handlers.clear()
        runtime.logger.addHandler(logging.NullHandler())
        runtime.system_proxy = SystemProxyManager(
            config["client"]["system_proxy"],
            backend=backend,
            platform_name="windows",
        )
        runtime.runtime_state = RuntimeStateStore(temp_path / "runtime-state.json")
        # Lifecycle fixtures do not own real Mihomo listeners. Listener
        # collision behavior is exercised with real sockets in
        # test_runtime_startup_stability_regressions.py; keep unrelated start
        # tests independent of proxy software running on the host.
        runtime._preflight_runtime_listeners = lambda: ()
        runtime._wait_for_mihomo_route_ready = lambda **_kwargs: {
            "ready": True,
            "reason": "test route ready",
            "routes": {},
        }
        runtime._wait_for_runtime_source_listeners_or_stop = (
            lambda _runner, *, timeout: None
        )
        runtime._probe_mihomo_mixed_port = lambda: {"url": "https://cp.cloudflare.com", "latency_ms": 1}
        runtime._probe_mihomo_mixed_port_once = lambda **_kwargs: {
            "url": "https://cp.cloudflare.com",
            "latency_ms": 1,
        }
        # Lifecycle fixtures do not stand up a real controller graph.  The
        # post-ApplyConfig route replay/readback contract has dedicated tests
        # in test_runtime_capture_reload_regressions.py.
        runtime._verify_capture_route_after_reload = lambda **_kwargs: {
            "status": 204,
            "fixture": True,
        }
        def publish_fixture_tier3_qualification():
            # Most lifecycle tests intentionally stub qualification rather
            # than standing up a controller. Keep that fixture internally
            # consistent: a satisfied qualification refresh has also
            # published the controller snapshot for the current generation.
            # Tests that exercise selected-leaf binding replace this snapshot
            # with their explicit controller graph.
            runtime._tier3_runtime_controller_snapshot = (
                int(runtime._tier3_runtime_config_generation),
                str(runtime._tier3_runtime_config_digest),
                {"proxies": {}},
            )
            return Tier3RuntimeQualificationSnapshot(
                config_generation=1,
                config_digest="desktop-runtime-test-fixture",
                admitted_count=64,
                exported_count=64,
                qualified_count=64,
                qualified_identities=tuple(
                    f"qualified-{index}" for index in range(64)
                ),
                final_exit_ok=False,
                traffic_capture_active=False,
                observed_at_monotonic=time.monotonic(),
            )
        runtime._refresh_tier3_runtime_qualification = (
            publish_fixture_tier3_qualification
        )
        runtime.test_proxy_delay = lambda name, *, url=None, timeout_ms=5000: {"delay": 1}
        runtime._verify_final_exit_identity = lambda **_kwargs: {
            "state": "complete",
            "identity": {
                "ipv4": {"state": "confirmed", "ip": "203.0.113.1"},
                "ipv6": {"state": "unavailable", "ip": None},
            },
            "route": {
                "confirmed_before": True,
                "confirmed_after": True,
                "stable_during_check": True,
            },
        }
        return runtime

    @staticmethod
    def _commit_deferred_initial_egress_admission(
        runtime: DesktopRuntime,
    ) -> bool:
        """Model the runtime-start commit without starting its monitor thread."""

        with runtime._runtime_intent_lock:
            runtime.running = True
            runtime._desired_running = True
            lifecycle_revision = int(runtime._lifecycle_revision)
        return runtime._start_deferred_initial_egress_admission(
            lifecycle_revision=lifecycle_revision,
        )

    @staticmethod
    def _install_injected_runtime_authority(runtime: DesktopRuntime) -> None:
        """Publish a managed route for lifecycle tests without a controller.

        Production activation must never bypass the post-ApplyConfig controller
        readback.  These narrow lifecycle tests deliberately inject a fake Core
        and do not expose `/proxies`, a PID, or a loaded export contract, so the
        synthetic authority is rebuilt on every call using the current CAS
        epochs.  Dedicated controller/readback tests exercise the real path.
        """

        def publish_authority(**_kwargs):
            leaf_name = "injected-lifecycle-leaf"
            node_ref = RuntimeNodeRef(
                tier="tier3",
                pool_kind="tier3b",
                identity="injected-lifecycle-leaf",
                proxy_name=leaf_name,
            )
            controller_payload = {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK", "REJECT"],
                    },
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "TIER3-AUTO",
                        "all": ["TIER3-AUTO", "REJECT"],
                    },
                    "TIER3-AUTO": {
                        "type": "Fallback",
                        "now": leaf_name,
                        "all": [leaf_name, "REJECT"],
                    },
                    leaf_name: {"type": "Shadowsocks"},
                    "REJECT": {"type": "Reject"},
                }
            }
            snapshot_payload = {
                "proxies": controller_payload,
                "proxy_map": {
                    leaf_name: {
                        "tier": "tier3",
                        "pool_kind": "tier3b",
                        "identity": node_ref.identity,
                    }
                },
            }
            with runtime._core_lifecycle_lock:
                core_generation = int(runtime._core_generation)
            with runtime._runtime_route_snapshot_lock:
                route_epoch = max(1, int(runtime._runtime_route_epoch))
                runtime._runtime_route_epoch = route_epoch
                network_epoch = int(runtime._runtime_network_context_epoch)
                snapshot = CoreLoadedRouteSnapshot(
                    process_pid=4242,
                    process_generation=1,
                    core_generation=core_generation,
                    semantic_digest="injected-lifecycle-authority",
                    route_epoch=route_epoch,
                    network_context_epoch=network_epoch,
                    group_graph=(
                        ("FINAL-EXIT", ("PROXY-FALLBACK", "REJECT")),
                        ("PROXY-FALLBACK", ("TIER3-AUTO", "REJECT")),
                        ("TIER3-AUTO", (leaf_name, "REJECT")),
                    ),
                    group_states=(
                        CoreLoadedGroupState(
                            name="FINAL-EXIT",
                            type_name="Selector",
                            members=("PROXY-FALLBACK", "REJECT"),
                            now="PROXY-FALLBACK",
                        ),
                        CoreLoadedGroupState(
                            name="PROXY-FALLBACK",
                            type_name="Fallback",
                            members=("TIER3-AUTO", "REJECT"),
                            now="TIER3-AUTO",
                        ),
                        CoreLoadedGroupState(
                            name="TIER3-AUTO",
                            type_name="Fallback",
                            members=(leaf_name, "REJECT"),
                            now=leaf_name,
                        ),
                    ),
                    proxy_nodes=(node_ref,),
                    loaded_at_monotonic=time.monotonic(),
                )
                runtime._runtime_route_snapshot = snapshot
                runtime._runtime_route_snapshot_payload = snapshot_payload
                runtime._runtime_core_inventory_snapshot = (
                    runtime._core_inventory_from_route_snapshot(snapshot)
                )
            runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
            binding = runtime._runtime_route_binding_for_outbound(
                snapshot,
                snapshot_payload,
                "FINAL-EXIT",
            )
            if binding is None:
                raise AssertionError("injected lifecycle route did not bind")
            return snapshot, snapshot_payload, binding

        runtime._refresh_loaded_runtime_authority = publish_authority
        runtime._trigger_startup_native_fallback_health = (
            lambda *_args, **_kwargs: "injected_controller_fixture"
        )
        runtime._verify_capture_route_after_reload = (
            lambda **_kwargs: (
                publish_authority()
                and {"status": 204, "fixture": True}
            )
        )

    def _install_manual_validation_authority(self, runtime: DesktopRuntime) -> None:
        self._install_injected_runtime_authority(runtime)
        # Manual diagnostics use the controller snapshot API, not activation's
        # authority adapter. Establish its epoch before acquiring the lease.
        runtime._refresh_runtime_route_snapshot = (
            lambda **kwargs: runtime._refresh_loaded_runtime_authority(**kwargs)[0]
        )
        runtime._refresh_runtime_route_snapshot()

    def test_apply_system_proxy_override_updates_live_capture_without_core_reload(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(False, "", ""))
        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime._config_change_lock = threading.RLock()
        runtime.logger = logging.getLogger("desktop-runtime-bypass-apply-test")
        runtime.logger.handlers.clear()
        runtime.logger.addHandler(logging.NullHandler())
        runtime.logger.propagate = False
        runtime.reload_core_config = lambda **_kwargs: self.fail(
            "saving the bypass list must not reload Core"
        )
        runtime.system_proxy = SystemProxyManager(
            {"enabled": True, "host": "127.0.0.1", "port": 7890, "bypass": ["localhost"]},
            backend=backend,
            platform_name="windows",
        )
        runtime.system_proxy.enable()
        runtime.system_proxy.bypass = ["localhost", "printer.local"]

        self.assertEqual(
            runtime.apply_system_proxy_override(),
            {"applied": True, "reason": "written"},
        )
        self.assertEqual(
            backend.settings,
            ProxySettings(True, "127.0.0.1:7890", "localhost;printer.local"),
        )

        def fail_write(_settings: ProxySettings) -> None:
            raise OSError("registry denied")

        backend.write = fail_write
        runtime.system_proxy.bypass = ["localhost"]

        self.assertEqual(
            runtime.apply_system_proxy_override(),
            {"applied": False, "reason": "write_failed"},
        )
        self.assertEqual(backend.settings.override, "localhost;printer.local")
        self.assertIn("registry denied", runtime.system_proxy.last_error)

    def test_manual_204_without_controller_binding_remains_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir), MemoryProxyBackend(ProxySettings(False, "", "")),
            )
            runtime.system_proxy.enabled = False
            runtime.manager = FakeTier3Manager()
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._set_core_runtime_phase("formal_capture")
            runtime._current_runtime_failure_binding_state = lambda **_kwargs: None
            runtime.current_route_context = lambda **_kwargs: {
                "confirmed": False, "facility_failure": "fixture_controller_unavailable",
            }
            runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
            runtime._manual_current_exit_quality_contracts = lambda _session: (
                ManualDataPlaneProbeResult.https_204("neutral-a"),
                ManualDataPlaneProbeResult.https_204("neutral-b"),
            )
            queued = []
            runtime._queue_final_exit_identity_diagnostic = (
                lambda **kwargs: queued.append(kwargs)
            )
            result = runtime.check_exit_quality(proxy_url=None, request_id="unbound-204")
            self.assertEqual(result["diagnostics"]["neutral_connectivity"]["success_count"], 2)
            self.assertEqual(result["diagnostics"]["neutral_connectivity"]["state"], "indeterminate")
            self.assertFalse(result["diagnostics"]["identity_pending"])
            self.assertEqual(queued, [])

    def test_core_sync_ledger_counts_are_owned_with_the_ring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(ProxySettings(False, "", "")),
            )

            class _Manager:
                def __init__(self) -> None:
                    self.lock = threading.RLock()
                    self.tier3_last_collection_report = {}

            manager = _Manager()
            runtime.manager = manager
            runtime._record_tier3_core_sync_ledger("semantic_noop")
            runtime._record_tier3_core_sync_ledger("config_apply")
            manager.tier3_last_collection_report = {
                "core_sync_ledger_counts": {"semantic_noop": 999}
            }
            runtime._record_tier3_core_sync_ledger("semantic_noop")

            snapshot = runtime._tier3_core_sync_owner_snapshot()
            self.assertEqual(
                snapshot["core_sync_ledger_counts"],
                {"semantic_noop": 2, "config_apply": 1},
            )
            self.assertEqual(len(snapshot["core_sync_ledger"]), 1)
            self.assertEqual(
                snapshot["core_sync_ledger"][0]["kind"],
                "config_apply",
            )
            self.assertEqual(
                snapshot["core_sync_last_semantic_noop"]["sequence"],
                3,
            )
            self.assertEqual(
                snapshot["core_sync_last_actual_kind"],
                "semantic_noop",
            )
            self.assertEqual(
                manager.tier3_last_collection_report[
                    "core_sync_ledger_counts"
                ],
                snapshot["core_sync_ledger_counts"],
            )

    def test_runtime_bad_leaf_uses_unified_health_until_its_own_recheck_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override=False)
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            bad = Node(
                name="bad-leaf",
                protocol="ss",
                uri="ss://bad-leaf",
                address="bad.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
                extra={"tier3_origin": "tier3b"},
            )
            manager.runtime_nodes["tier3"] = [bad]
            runtime.manager = manager
            identity = manager._node_identity(bad)
            ref = RuntimeNodeRef("tier3", "tier3b", identity, "bad-leaf")
            binding = RuntimeRouteBinding(
                process_pid=4321,
                process_generation=2,
                core_generation=7,
                semantic_digest="semantic",
                route_epoch=3,
                observed_outbound="GENERAL",
                route_chain=("GENERAL", "FINAL-EXIT", "PROXY-FALLBACK", "bad-leaf"),
                node=ref,
            )
            snapshot = CoreLoadedRouteSnapshot(
                process_pid=4321,
                process_generation=2,
                core_generation=7,
                semantic_digest="semantic",
                route_epoch=3,
                proxy_nodes=(ref,),
            )
            failure = BoundRuntimeFailure(
                binding=binding,
                protocol="TCP",
                observed_outbound="GENERAL",
                destination_token="destination-token",
                error_type="timeout",
                observed_at_monotonic=time.monotonic(),
            )
            runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
            snapshot_payload = {"proxy_map": {"bad-leaf": {"node": bad}}}
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_snapshot = snapshot
                runtime._runtime_route_snapshot_payload = snapshot_payload
            first = runtime._runtime_egress_coordinator.observe(
                failure,
                current_snapshot=snapshot,
            )
            decision = runtime._runtime_egress_coordinator.record_probe(
                failure,
                succeeded=False,
                probe_token=first.probe_token,
                current_snapshot=snapshot,
            )
            self.assertTrue(decision.confirmed)

            transition = runtime._apply_confirmed_runtime_health(
                failure,
                snapshot,
                snapshot_payload,
                incident_id=decision.incident_id,
                quarantine_token=decision.probe_token,
            )

            self.assertTrue(transition["accepted"])
            # Runtime truth overlays the card/Core eligibility but never
            # forges the persisted self-check result.
            self.assertEqual(bad.status, NodeStatus.ALIVE)
            effective = manager.runtime_effective_nodes("tier3")
            self.assertEqual(effective[0].status, NodeStatus.SUSPICIOUS)
            self.assertEqual(
                [
                    node.name
                    for node in runtime._manager_for_mihomo_runtime_config().get_nodes_for_subscription(
                        "tier3"
                    )
                ],
                ["bad-leaf"],
            )
            self.assertEqual(len(manager.runtime_leaf_rechecks), 1)
            self.assertEqual(manager.runtime_leaf_rechecks[0]["identity"], identity)
            self.assertEqual(manager.runtime_leaf_rechecks[0]["pool_kind"], "tier3b")

            # A replacement leaf can restore traffic, but the failed member
            # remains absent from m/n/Core until its own authoritative PASS.
            runtime._runtime_egress_coordinator.mark_recovered(
                binding,
                incident_id=decision.incident_id,
                quarantine_token=decision.probe_token,
            )
            self.assertEqual(
                manager.runtime_effective_nodes("tier3")[0].status,
                NodeStatus.SUSPICIOUS,
            )
            self.assertEqual(manager.runtime_circuit_updates, [])

            recovered = manager.apply_runtime_health_observation(
                {
                    "node": ref,
                    "verdict": "pass",
                    "source": "runtime_leaf_recheck",
                    "incident_id": decision.incident_id,
                    "quarantine_token": decision.probe_token,
                }
            )
            self.assertEqual(recovered["action"], "recovered")
            self.assertEqual(
                manager.runtime_effective_nodes("tier3")[0].status,
                NodeStatus.ALIVE,
            )

    def test_runtime_failure_binding_accepts_every_managed_final_exit_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            cases = (
                ("tier1", "tier1_variant"),
                ("tier2", "tier2"),
                ("tier3", "tier3a"),
                ("tier3", "tier3b"),
                ("exit", "gateway"),
            )
            for index, (tier, pool_kind) in enumerate(cases):
                with self.subTest(tier=tier, pool_kind=pool_kind):
                    proxy_name = f"leaf-{index}"
                    ref = RuntimeNodeRef(
                        tier=tier,
                        pool_kind=pool_kind,
                        identity=f"identity-{index}",
                        proxy_name=proxy_name,
                    )
                    snapshot = CoreLoadedRouteSnapshot(
                        process_pid=4321,
                        process_generation=2,
                        core_generation=9,
                        semantic_digest="semantic",
                        route_epoch=4,
                        proxy_nodes=(ref,),
                    )
                    proxies = {
                        "proxies": {
                            "GENERAL": {
                                "type": "Selector",
                                "now": "FINAL-EXIT",
                                "all": ["FINAL-EXIT"],
                            },
                            "FINAL-EXIT": {
                                "type": "Selector",
                                "now": proxy_name,
                                "all": [proxy_name, "REJECT"],
                            },
                            proxy_name: {"type": "Shadowsocks"},
                        }
                    }
                    binding = runtime._runtime_route_binding_for_outbound(
                        snapshot,
                        {"proxies": proxies, "proxy_map": {}},
                        "GENERAL",
                    )
                    self.assertIsNotNone(binding)
                    assert binding is not None
                    self.assertEqual(binding.node, ref)
                    self.assertEqual(
                        binding.route_chain,
                        ("GENERAL", "FINAL-EXIT", proxy_name),
                    )

            facility_snapshot = CoreLoadedRouteSnapshot(
                process_pid=4321,
                process_generation=2,
                core_generation=9,
                semantic_digest="semantic",
                route_epoch=5,
            )
            self.assertIsNone(
                runtime._runtime_route_binding_for_outbound(
                    facility_snapshot,
                    {
                        "proxies": {
                            "proxies": {
                                "GENERAL": {"now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                                "FINAL-EXIT": {"now": "DIRECT", "all": ["DIRECT"]},
                            }
                        },
                        "proxy_map": {},
                    },
                    "GENERAL",
                )
            )

    def test_runtime_circuit_key_includes_outbound_and_full_route_chain(self) -> None:
        ref = RuntimeNodeRef(
            "tier1",
            "tier1_variant",
            "shared-identity",
            "shared-leaf",
        )
        common = dict(
            process_pid=4321,
            process_generation=2,
            core_generation=7,
            semantic_digest="digest",
            route_epoch=3,
            node=ref,
        )
        general = RuntimeRouteBinding(
            **common,
            observed_outbound="GENERAL",
            route_chain=("GENERAL", "FINAL-EXIT", "shared-leaf"),
        )
        final_exit = RuntimeRouteBinding(
            **common,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", "shared-leaf"),
        )
        underlay = RuntimeRouteBinding(
            **common,
            observed_outbound="PROXY-FALLBACK",
            route_chain=("PROXY-FALLBACK", "shared-leaf"),
        )

        self.assertEqual(
            len({general.circuit_key, final_exit.circuit_key, underlay.circuit_key}),
            3,
        )

    def _global_runtime_failure_fixture(
        self,
        temp_path: Path,
    ) -> tuple[
        DesktopRuntime,
        FakeTier3Manager,
        Node,
        Node,
        CoreLoadedRouteSnapshot,
        dict[str, object],
    ]:
        runtime = self._runtime(
            temp_path,
            MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override=False)
            ),
        )
        bad = Node(
            name="bad-current",
            protocol="ss",
            uri="ss://bad-current",
            address="bad-current.example.com",
            port=8388,
            tier="tier1",
            status=NodeStatus.ALIVE,
            method="aes-256-gcm",
            password="pass",
        )
        good = Node(
            name="good-next",
            protocol="ss",
            uri="ss://good-next",
            address="good-next.example.com",
            port=8389,
            tier="tier1",
            status=NodeStatus.ALIVE,
            method="aes-256-gcm",
            password="pass",
        )
        manager = FakeTier3Manager()
        manager.runtime_nodes["tier1"] = [bad, good]
        runtime.manager = manager
        runner = FakeCoreRunner(running=True, port_ok=True)
        runner.status_payload.update({"pid": 4321, "process_generation": 2})
        runtime.core_runner = runner
        runtime._core_generation = 7
        runtime._runtime_loaded_semantic_digest = lambda: "loaded-digest"
        proxies: dict[str, object] = {
            "proxies": {
                "GENERAL": {
                    "type": "Selector",
                    "now": "FINAL-EXIT",
                    "all": ["FINAL-EXIT"],
                },
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "PROXY-FALLBACK",
                    "all": ["PROXY-FALLBACK", "REJECT"],
                },
                "PROXY-FALLBACK": {
                    "type": "Fallback",
                    "now": "TIER1-AUTO",
                    "all": ["TIER1-AUTO"],
                },
                "TIER1-AUTO": {
                    "type": "Fallback",
                    "now": "bad-current",
                    "all": ["bad-current", "good-next"],
                },
                "bad-current": {"type": "Shadowsocks"},
                "good-next": {"type": "Shadowsocks"},
            }
        }
        runtime.mihomo_proxies = lambda: copy.deepcopy(proxies)
        bad_ref = RuntimeNodeRef(
            "tier1",
            "tier1_variant",
            manager._node_identity(bad),
            "bad-current",
        )
        good_ref = RuntimeNodeRef(
            "tier1",
            "tier1_variant",
            manager._node_identity(good),
            "good-next",
        )
        proxy_map = {
            "bad-current": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": bad_ref.identity,
                "proxy_name": "bad-current",
                "node": bad,
            },
            "good-next": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": good_ref.identity,
                "proxy_name": "good-next",
                "node": good,
            },
        }
        runtime._runtime_proxy_name_map = lambda: proxy_map
        runtime._runtime_loaded_export_contract = {
            "semantic_digest": "loaded-digest",
            "proxy_map": copy.deepcopy(proxy_map),
        }
        snapshot = runtime._refresh_runtime_route_snapshot()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        _loaded, payload = runtime._runtime_route_snapshot_state()
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        # This fixture models a Core that has already completed startup
        # activation and is now reporting an in-use route failure.
        runtime._runtime_initial_activation_generation = runtime._core_generation
        manager.effective_egress_ok = True
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY,
            validation_active=False,
            lifecycle_revision=runtime._lifecycle_revision,
            route_epoch=snapshot.route_epoch,
            network_context_epoch=snapshot.network_context_epoch,
        )
        self.assertTrue(runtime._try_arm_runtime_recovery())
        self.assertTrue(runtime._runtime_monitoring_is_active())
        return runtime, manager, bad, good, snapshot, payload

    @staticmethod
    def _transport_output_event(
        destination: str,
        *,
        observed_at: float,
        pid: int = 4321,
        process_generation: int = 2,
    ) -> CoreOutputEvent:
        return CoreOutputEvent(
            raw_line=(
                "[TCP] dial GENERAL (match Match/) 127.0.0.1:55231 "
                f"--> {destination}:443 error: context deadline exceeded"
            ),
            pid=pid,
            process_generation=process_generation,
            received_at_monotonic=observed_at,
        )

    @staticmethod
    def _wait_until(predicate, *, timeout: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    def test_no_capture_hot_reload_commits_final_payload_as_loaded_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            manager = FakeTier3Manager()
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runner.status_payload.update({"pid": 5151, "process_generation": 4})
            runtime.core_runner = runner
            runtime._core_generation = 11
            runtime._ensure_geodata_ready_for_core_start = lambda _path: None
            runtime._refresh_node_diagnostic_core_binding = lambda: None

            previous_node = Node(
                name="previous-formal-leaf",
                protocol="ss",
                address="previous.example.com",
                port=8387,
                tier="tier1",
                status=NodeStatus.ALIVE,
            )
            final_node = Node(
                name="final-no-capture-leaf",
                protocol="ss",
                address="final.example.com",
                port=8388,
                tier="tier1",
                status=NodeStatus.ALIVE,
            )
            previous_proxy = {
                "name": "previous-formal-leaf",
                "type": "ss",
                "server": "previous.example.com",
                "port": 8387,
                "cipher": "aes-256-gcm",
                "password": "previous",
            }
            final_proxy = {
                "name": "final-no-capture-leaf",
                "type": "ss",
                "server": "final.example.com",
                "port": 8388,
                "cipher": "aes-256-gcm",
                "password": "final",
            }
            previous_info = {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "previous-identity",
                "proxy_name": "previous-formal-leaf",
                "node": previous_node,
                "proxy": previous_proxy,
            }
            final_info = {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "final-identity",
                "proxy_name": "final-no-capture-leaf",
                "node": final_node,
                "proxy": final_proxy,
            }
            runtime._runtime_proxy_name_map = lambda: {
                "final-no-capture-leaf": final_info
            }
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_loaded_export_contract = {
                    "semantic_digest": "previous-formal-digest",
                    "proxy_map": {
                        "previous-formal-leaf": previous_info,
                    },
                    "runtime_config_path": str(
                        temp_path / "previous-formal.yaml"
                    ),
                }

            formal_document = {
                "mixed-port": 7890,
                "allow-lan": True,
                "tun": {"enable": True, "stack": "mixed"},
                "sniffer": {"enable": True},
                "proxies": [final_proxy],
                "proxy-groups": [
                    {
                        "name": "FINAL-EXIT",
                        "type": "select",
                        "proxies": ["final-no-capture-leaf", "REJECT"],
                    }
                ],
                "rules": ["MATCH,FINAL-EXIT"],
            }
            formal_payload = yaml.safe_dump(
                formal_document,
                allow_unicode=True,
                sort_keys=False,
            )
            final_payload = build_capture_disabled_clash_config(formal_payload)
            formal_contract = runtime._runtime_export_contract_for_payload(
                formal_payload
            )
            final_contract = runtime._runtime_export_contract_for_payload(
                final_payload
            )
            self.assertNotEqual(
                formal_contract["semantic_digest"],
                final_contract["semantic_digest"],
            )
            runtime_config_path = temp_path / "mihomo-runtime.yaml"

            def write_formal_payload() -> Path:
                written = runtime._write_mihomo_runtime_payload(
                    formal_payload,
                    runtime_config_path,
                )
                runtime._record_pending_runtime_export_contract(
                    formal_payload,
                    written,
                )
                return written

            runtime.write_mihomo_runtime_config = write_formal_payload
            runtime.mihomo_proxies = lambda: {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "final-no-capture-leaf",
                        "all": ["final-no-capture-leaf", "REJECT"],
                    },
                    "final-no-capture-leaf": {"type": "Shadowsocks"},
                }
            }

            loaded_path = runtime._hot_reload_runtime_config_phase(
                runner,
                capture_enabled=False,
            )

            self.assertEqual(loaded_path, runtime_config_path)
            self.assertEqual(
                runtime_config_path.read_text(encoding="utf-8"),
                final_payload,
            )
            with runtime._runtime_route_snapshot_lock:
                loaded_contract = copy.deepcopy(
                    runtime._runtime_loaded_export_contract
                )
            self.assertEqual(
                loaded_contract["semantic_digest"],
                final_contract["semantic_digest"],
            )
            self.assertNotEqual(
                loaded_contract["semantic_digest"],
                formal_contract["semantic_digest"],
            )
            self.assertNotEqual(
                loaded_contract["semantic_digest"],
                "previous-formal-digest",
            )
            self.assertEqual(
                set(loaded_contract["proxy_map"]),
                {"final-no-capture-leaf"},
            )
            self.assertNotIn(
                "previous-formal-leaf",
                loaded_contract["proxy_map"],
            )

            snapshot = runtime._refresh_runtime_route_snapshot()
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(
                snapshot.semantic_digest,
                final_contract["semantic_digest"],
            )
            self.assertEqual(
                [ref.proxy_name for ref in snapshot.proxy_nodes],
                ["final-no-capture-leaf"],
            )
            _current_snapshot, snapshot_payload = (
                runtime._runtime_route_snapshot_state()
            )
            self.assertEqual(
                set(snapshot_payload["proxy_map"]),
                {"final-no-capture-leaf"},
            )
            # Returned snapshot payloads are immutable observations: a caller
            # cannot mutate the loaded proxy map used by later attribution.
            snapshot_payload["proxy_map"].clear()
            _again, snapshot_payload_again = runtime._runtime_route_snapshot_state()
            self.assertEqual(
                set(snapshot_payload_again["proxy_map"]),
                {"final-no-capture-leaf"},
            )

    def test_core_output_to_unified_health_and_single_recovery_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            bad_probe_started = threading.Event()
            release_bad_probe = threading.Event()
            fast_switch_started = threading.Event()
            release_fast_switch = threading.Event()
            fast_switch_calls: list[dict[str, object]] = []

            def probe_leaf(ref, _payload, *, timeout_ms=1500, **_kwargs):
                self.assertLessEqual(timeout_ms, 1500)
                if ref.proxy_name == "bad-current":
                    bad_probe_started.set()
                    release_bad_probe.wait(1.0)
                    raise RuntimeError("bad leaf timed out")
                return {"delay": 12, "name": ref.proxy_name}

            def fast_switch(*_args, **kwargs):
                fast_switch_calls.append(dict(kwargs))
                fast_switch_started.set()
                release_fast_switch.wait(1.0)
                return {
                    "fast_path_available": True,
                    "post_switch_connectivity_verified": True,
                    "effective_egress_ok": True,
                    "alternative_leaf": "good-next",
                    "selected_chain": ["GENERAL", "FINAL-EXIT", "good-next"],
                    "post_switch_result": {"status": 204},
                    "loaded_route_epoch": snapshot.route_epoch + 1,
                    "selection_action": {
                        "group": "TIER1-AUTO",
                        "target": "good-next",
                        "mode": "fixed",
                    },
                }

            runtime._probe_runtime_leaf = probe_leaf
            runtime._fast_recover_confirmed_alternative = fast_switch
            runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("confirmed alternatives must not enter the legacy audit")
            )
            runtime._close_runtime_connections_for_binding = lambda _binding: 0
            started = time.monotonic()
            first = self._transport_output_event(
                "cp.cloudflare.com",
                observed_at=started,
            )
            second = self._transport_output_event(
                "www.gstatic.com",
                observed_at=started + 0.1,
            )

            runtime._handle_runtime_output_event(first, snapshot, payload)
            self.assertTrue(bad_probe_started.wait(1.0))
            runtime._handle_runtime_output_event(second, snapshot, payload)
            release_bad_probe.set()
            self.assertTrue(fast_switch_started.wait(1.0))

            self.assertEqual(len(manager.runtime_observations), 1)
            observation = manager.runtime_observations[0]
            self.assertEqual(observation["verdict"], "confirmed")
            self.assertEqual(observation["tier"], "tier1")
            self.assertEqual(observation["pool_kind"], "tier1_variant")
            self.assertEqual(observation["proxy_name"], "bad-current")
            self.assertEqual(len(manager.runtime_leaf_rechecks), 1)
            self.assertEqual(bad.status, NodeStatus.ALIVE)
            self.assertEqual(
                manager.runtime_effective_nodes("tier1")[0].status,
                NodeStatus.SUSPICIOUS,
            )

            # A third duplicate while the transaction is active cannot create
            # a second Core mutation or a second health transition.
            runtime._handle_runtime_output_event(second, snapshot, payload)
            self.assertEqual(len(fast_switch_calls), 1)
            release_fast_switch.set()
            self.assertTrue(
                self._wait_until(
                    lambda: not runtime._runtime_failure_recovery_threads,
                    timeout=1.5,
                )
            )
            self.assertEqual(len(fast_switch_calls), 1)
            self.assertEqual(len(manager.runtime_observations), 1)
            circuit = runtime._runtime_egress_coordinator.current_snapshot_for(
                next(
                    binding
                    for binding in runtime._runtime_incident_bindings.values()
                    if binding.node.proxy_name == "bad-current"
                )
            )
            self.assertIsNotNone(circuit)
            assert circuit is not None
            self.assertEqual(circuit.state, "quarantined")
            # Replacement success restores traffic only; it cannot clear the
            # failed leaf's canonical health overlay.
            self.assertEqual(
                manager.runtime_effective_nodes("tier1")[0].status,
                NodeStatus.SUSPICIOUS,
            )

    def test_core_output_without_verified_backup_finishes_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, _bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            runtime._probe_runtime_leaf = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(RuntimeError("proxy connection refused"))
            )
            runtime._probe_managed_alternative = lambda *_args, **_kwargs: (
                None,
                None,
            )
            runtime._fast_recover_confirmed_alternative = (
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("an unverified backup must not be selected")
                )
            )

            runtime._handle_runtime_output_event(
                self._transport_output_event(
                    "cp.cloudflare.com",
                    observed_at=time.monotonic(),
                ),
                snapshot,
                payload,
            )

            self.assertTrue(
                self._wait_until(
                    lambda: not runtime._runtime_failure_recovery_threads,
                    timeout=1.5,
                )
            )
            self.assertEqual(manager.runtime_observations, [])
            self.assertEqual(manager.runtime_leaf_rechecks, [])
            self.assertIsNone(runtime._runtime_route_mutation_lease)
            circuits = runtime._runtime_egress_coordinator.current_snapshots()
            self.assertEqual(len(circuits), 1)
            self.assertEqual(circuits[0].state, "indeterminate")

    def test_confirmed_runtime_health_projects_across_all_managed_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            manager = FakeTier3Manager()
            nodes = {
                "tier1": Node(
                    name="tier1-variant",
                    protocol="ss",
                    address="tier1.example.com",
                    port=8001,
                    tier="tier1",
                    status=NodeStatus.ALIVE,
                ),
                "tier2": Node(
                    name="tier2-static",
                    protocol="ss",
                    address="tier2.example.com",
                    port=8002,
                    tier="tier2",
                    status=NodeStatus.ALIVE,
                ),
                "tier3a": Node(
                    name="tier3-user",
                    protocol="ss",
                    address="tier3a.example.com",
                    port=8003,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    extra={"tier3_origin": "tier3a"},
                ),
                "tier3b": Node(
                    name="tier3-auto",
                    protocol="ss",
                    address="tier3b.example.com",
                    port=8004,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    extra={"tier3_origin": "tier3b"},
                ),
                "gateway": Node(
                    name="fixed-gateway",
                    protocol="ss",
                    address="gateway.example.com",
                    port=8005,
                    tier="exit",
                    status=NodeStatus.ALIVE,
                ),
            }
            manager.runtime_nodes["tier1"] = [nodes["tier1"]]
            manager.runtime_nodes["tier2"] = [nodes["tier2"]]
            manager.runtime_nodes["tier3"] = [nodes["tier3a"], nodes["tier3b"]]
            manager.runtime_nodes["exit"] = [nodes["gateway"]]
            runtime.manager = manager
            refs = (
                RuntimeNodeRef(
                    "tier1",
                    "tier1_variant",
                    manager._node_identity(nodes["tier1"]),
                    "tier1-variant",
                ),
                RuntimeNodeRef(
                    "tier2",
                    "tier2",
                    manager._node_identity(nodes["tier2"]),
                    "tier2-static",
                ),
                RuntimeNodeRef(
                    "tier3",
                    "tier3a",
                    manager._node_identity(nodes["tier3a"]),
                    "tier3-user",
                ),
                RuntimeNodeRef(
                    "tier3",
                    "tier3b",
                    manager._node_identity(nodes["tier3b"]),
                    "tier3-auto",
                ),
                RuntimeNodeRef(
                    "exit",
                    "gateway",
                    manager._node_identity(nodes["gateway"]),
                    "fixed-gateway",
                ),
            )
            snapshot = CoreLoadedRouteSnapshot(
                process_pid=4321,
                process_generation=2,
                core_generation=7,
                semantic_digest="digest",
                route_epoch=3,
                proxy_nodes=refs,
            )
            snapshot_payload = {"proxy_map": {}}
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_snapshot = snapshot
                runtime._runtime_route_snapshot_payload = snapshot_payload
            runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
            runtime._close_runtime_connections_for_binding = lambda _binding: 0
            for index, ref in enumerate(refs):
                binding = RuntimeRouteBinding(
                    process_pid=4321,
                    process_generation=2,
                    core_generation=7,
                    semantic_digest="digest",
                    route_epoch=3,
                    observed_outbound="GENERAL",
                    route_chain=("GENERAL", "FINAL-EXIT", ref.proxy_name),
                    node=ref,
                )
                transition = runtime._apply_confirmed_runtime_health(
                    BoundRuntimeFailure(
                        binding=binding,
                        protocol="TCP",
                        observed_outbound="GENERAL",
                        destination_token=f"destination-{index}",
                        error_type="timeout",
                        observed_at_monotonic=time.monotonic() + index,
                    ),
                    snapshot,
                    snapshot_payload,
                    incident_id=f"incident-{index}",
                    quarantine_token=f"token-{index}",
                )
                self.assertTrue(transition["accepted"])

            for tier_id, raw_nodes in manager.runtime_nodes.items():
                self.assertTrue(
                    all(node.status is NodeStatus.ALIVE for node in raw_nodes),
                    tier_id,
                )
                self.assertTrue(
                    all(
                        node.status is NodeStatus.SUSPICIOUS
                        for node in manager.runtime_effective_nodes(tier_id)
                    ),
                    tier_id,
                )
            self.assertEqual(
                [entry["pool_kind"] for entry in manager.runtime_leaf_rechecks],
                ["tier1_variant", "tier2", "tier3a", "tier3b", "gateway"],
            )
            core_view = runtime._manager_for_mihomo_runtime_config()
            self.assertEqual(core_view.get_nodes_for_subscription("tier1"), [])
            self.assertEqual(core_view.get_nodes_for_subscription("tier2"), [])
            # Tier3A is removed from the direct graph. Tier3B remains in the
            # provider for drain-only use while the route lease prevents new
            # connections from selecting the quarantined member.
            self.assertEqual(
                [node.name for node in core_view.get_nodes_for_subscription("tier3")],
                ["tier3-auto"],
            )
            self.assertEqual(core_view.get_nodes_for_subscription("exit"), [])

    def test_queued_core_output_is_stale_after_loaded_snapshot_changes_without_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _manager, _bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            probes: list[str] = []
            runtime._start_runtime_independent_probe = (
                lambda failure, *_args, **_kwargs: probes.append(
                    failure.binding.node.proxy_name
                )
            )
            base = dict(
                process_pid=snapshot.process_pid,
                process_generation=snapshot.process_generation,
                core_generation=snapshot.core_generation,
                semantic_digest=snapshot.semantic_digest,
                route_epoch=snapshot.route_epoch,
                group_graph=snapshot.group_graph,
                proxy_nodes=snapshot.proxy_nodes,
            )
            variants = (
                {"process_pid": snapshot.process_pid + 1},
                {"process_generation": snapshot.process_generation + 1},
                {"core_generation": snapshot.core_generation + 1},
                {"semantic_digest": "new-loaded-digest"},
                {"route_epoch": snapshot.route_epoch + 1},
            )
            route_change_guards: list[tuple[str, str]] = []
            runtime._submit_current_route_probe_failure = (
                lambda reason, *, source="runtime_probe": (
                    route_change_guards.append((str(reason), str(source))) or True
                )
            )
            for index, changed in enumerate(variants):
                with self.subTest(changed=tuple(changed)):
                    newer = CoreLoadedRouteSnapshot(
                        **{**base, **changed}
                    )
                    with runtime._runtime_route_snapshot_lock:
                        runtime._runtime_route_snapshot = newer
                        runtime._runtime_route_snapshot_payload = payload
                    runtime._runtime_egress_coordinator.restart(
                        current_snapshot=newer
                    )
                    runtime._refresh_runtime_route_snapshot = (
                        lambda **_kwargs: newer
                    )
                    runtime._handle_runtime_output_event(
                        self._transport_output_event(
                            f"stale-{index}.example.com",
                            observed_at=time.monotonic() + index,
                        ),
                        snapshot,
                        payload,
                    )
                    self.assertEqual(probes, [])
                    self.assertEqual(route_change_guards, [])

            # PID/process generation are also rejected before queueing.
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_snapshot = snapshot
                runtime._runtime_route_snapshot_payload = payload
            runtime._runtime_egress_coordinator.restart(current_snapshot=snapshot)
            runtime._runtime_egress_coordinator.restart(
                current_snapshot=snapshot
            )
            runtime._on_core_output_event(
                self._transport_output_event(
                    "wrong-pid.example.com",
                    observed_at=time.monotonic(),
                    pid=snapshot.process_pid + 1,
                )
            )
            runtime._on_core_output_event(
                self._transport_output_event(
                    "wrong-generation.example.com",
                    observed_at=time.monotonic(),
                    process_generation=snapshot.process_generation + 1,
                )
            )
            self.assertEqual(
                runtime._runtime_egress_coordinator.queue_snapshot().pending_count,
                0,
            )

    def test_recovery_cas_rejects_route_change_before_manager_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, _bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            binding = runtime._runtime_route_binding_for_outbound(
                snapshot,
                payload,
                "GENERAL",
            )
            self.assertIsNotNone(binding)
            assert binding is not None
            failure = BoundRuntimeFailure(
                binding=binding,
                protocol="TCP",
                observed_outbound="GENERAL",
                destination_token="old-route-destination",
                error_type="timeout",
                observed_at_monotonic=time.monotonic(),
            )
            first = runtime._runtime_egress_coordinator.observe(
                failure,
                current_snapshot=snapshot,
            )
            confirmed = runtime._runtime_egress_coordinator.record_probe(
                failure,
                succeeded=False,
                probe_token=first.probe_token,
                current_snapshot=snapshot,
            )
            self.assertTrue(confirmed.confirmed)
            newer = CoreLoadedRouteSnapshot(
                process_pid=snapshot.process_pid,
                process_generation=snapshot.process_generation,
                core_generation=snapshot.core_generation,
                semantic_digest=snapshot.semantic_digest,
                route_epoch=snapshot.route_epoch + 1,
                group_graph=snapshot.group_graph,
                proxy_nodes=snapshot.proxy_nodes,
            )
            runtime._runtime_egress_coordinator.set_current_snapshot(newer)
            audits: list[str] = []
            runtime.audit_runtime_egress = lambda **_kwargs: (
                audits.append("audit")
                or {
                    "post_switch_verified": True,
                    "post_switch_connectivity_verified": True,
                    "effective_egress_ok": True,
                }
            )

            runtime._start_runtime_recovery(
                failure,
                snapshot,
                payload,
                incident_id=confirmed.incident_id,
                quarantine_token=confirmed.probe_token,
                confirmed_alternative=next(
                    ref
                    for ref in snapshot.proxy_nodes
                    if ref.proxy_name == "good-next"
                ),
            )
            self.assertTrue(
                self._wait_until(
                    lambda: not runtime._runtime_failure_recovery_threads,
                    timeout=1.0,
                )
            )
            self.assertEqual(manager.runtime_observations, [])
            self.assertEqual(manager.runtime_leaf_rechecks, [])
            self.assertEqual(audits, [])

    def test_confirmed_recovery_worker_applies_health_and_uses_fast_alternative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, _bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            binding = runtime._runtime_route_binding_for_outbound(
                snapshot,
                payload,
                "GENERAL",
            )
            self.assertIsNotNone(binding)
            assert binding is not None
            failure = BoundRuntimeFailure(
                binding=binding,
                protocol="TCP",
                observed_outbound="GENERAL",
                destination_token="confirmed-worker-destination",
                error_type="timeout",
                observed_at_monotonic=time.monotonic(),
            )
            first = runtime._runtime_egress_coordinator.observe(
                failure,
                current_snapshot=snapshot,
            )
            confirmed = runtime._runtime_egress_coordinator.record_probe(
                failure,
                succeeded=False,
                probe_token=first.probe_token,
                current_snapshot=snapshot,
            )
            self.assertTrue(confirmed.confirmed)
            fast_switches: list[dict[str, object]] = []

            def fast_switch(*_args, **kwargs):
                fast_switches.append(dict(kwargs))
                return {
                    "fast_path_available": True,
                    "post_switch_connectivity_verified": True,
                    "effective_egress_ok": True,
                    "selected_chain": ["FINAL-EXIT", "good-next"],
                    "alternative_leaf": "good-next",
                    "post_switch_result": {"status": 204},
                    "loaded_route_epoch": snapshot.route_epoch + 1,
                    "selection_action": {
                        "group": "TIER1-AUTO",
                        "target": "good-next",
                        "mode": "fixed",
                    },
                }

            runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("confirmed alternatives must not enter the legacy audit")
            )
            runtime._fast_recover_confirmed_alternative = fast_switch
            runtime._start_runtime_connection_cleanup = lambda _binding: None
            runtime._start_runtime_recovery(
                failure,
                snapshot,
                payload,
                incident_id=confirmed.incident_id,
                quarantine_token=confirmed.probe_token,
                confirmed_alternative=next(
                    ref
                    for ref in snapshot.proxy_nodes
                    if ref.proxy_name == "good-next"
                ),
            )

            self.assertTrue(
                self._wait_until(
                    lambda: not runtime._runtime_failure_recovery_threads,
                    timeout=1.0,
                )
            )
            self.assertEqual(len(fast_switches), 1)
            self.assertEqual(
                fast_switches[0]["incident_id"],
                confirmed.incident_id,
            )
            self.assertTrue(manager.runtime_observations)
            self.assertEqual(manager.runtime_observations[0]["verdict"], "confirmed")
            self.assertTrue(manager.runtime_leaf_rechecks)
            self.assertEqual(
                runtime._runtime_route_mutation_lease["to"],
                "good-next",
            )
            # Replacement success restores traffic only; the failed member
            # remains quarantined until its own authoritative PASS.
            self.assertEqual(
                manager.runtime_effective_nodes("tier1")[0].status,
                NodeStatus.SUSPICIOUS,
            )

    def test_early_confirmed_recovery_has_no_reload_close_or_pressure_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, _bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            binding = runtime._runtime_route_binding_for_outbound(
                snapshot,
                payload,
                "GENERAL",
            )
            self.assertIsNotNone(binding)
            assert binding is not None
            failure = BoundRuntimeFailure(
                binding=binding,
                protocol="TCP",
                observed_outbound="GENERAL",
                destination_token="early-confirmed-destination",
                error_type="proxy_connect_refused",
                observed_at_monotonic=time.monotonic(),
                deterministic_proxy_endpoint=True,
            )
            decision = runtime._runtime_egress_coordinator.observe(
                failure,
                current_snapshot=snapshot,
            )
            ticket = runtime._ensure_runtime_recovery_ticket(
                failure,
                snapshot,
                decision.incident_id,
            )
            self.assertIsNotNone(ticket)
            assert ticket is not None
            alternative = next(
                ref
                for ref in snapshot.proxy_nodes
                if ref.proxy_name == "good-next"
            )
            runtime.reload_core_config = lambda **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("runtime recovery must not reload Core")
                )
            )
            runtime._start_runtime_connection_cleanup = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("runtime recovery must not close connections")
                )
            )
            runtime._runtime_recovery_pressure = SimpleNamespace(
                acquire=lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(
                        AssertionError(
                            "runtime recovery must not acquire collection pressure"
                        )
                    )
                )
            )
            switches: list[str] = []
            runtime._fast_recover_confirmed_alternative = (
                lambda _failure, _snapshot, _payload, ref, **_kwargs: (
                    switches.append(ref.proxy_name)
                    or {
                        "fast_path_available": True,
                        "post_switch_connectivity_verified": True,
                        "effective_egress_ok": True,
                        "post_switch_result": {"status": 204},
                        "loaded_route_epoch": snapshot.route_epoch + 1,
                        "selection_action": {
                            "group": "TIER1-AUTO",
                            "target": ref.proxy_name,
                            "mode": "fixed",
                        },
                    }
                )
            )

            outcome = runtime._run_runtime_early_confirmed_recovery(
                failure,
                snapshot,
                payload,
                ticket=ticket,
                probe_token=decision.probe_token,
                alternative=alternative,
                alternative_probe_result={"delay": 7},
            )

            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertIs(outcome.status, RuntimeRecoveryStatus.RECOVERED)
            self.assertEqual(switches, ["good-next"])
            self.assertEqual(len(manager.runtime_observations), 1)
            self.assertEqual(
                manager.runtime_effective_nodes("tier1")[0].status,
                NodeStatus.SUSPICIOUS,
            )

    def test_early_confirmed_recovery_failure_cannot_leave_ready_true(self) -> None:
        for failure_kind in ("replacement_failed", "arm_failed"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as temp_dir:
                runtime, manager, _bad, _good, snapshot, payload = (
                    self._global_runtime_failure_fixture(Path(temp_dir))
                )
                binding = runtime._runtime_route_binding_for_outbound(
                    snapshot,
                    payload,
                    "GENERAL",
                )
                self.assertIsNotNone(binding)
                assert binding is not None
                failure = BoundRuntimeFailure(
                    binding=binding,
                    protocol="TCP",
                    observed_outbound="GENERAL",
                    destination_token=f"{failure_kind}-destination",
                    error_type="proxy_connect_refused",
                    observed_at_monotonic=time.monotonic(),
                    deterministic_proxy_endpoint=True,
                )
                decision = runtime._runtime_egress_coordinator.observe(
                    failure,
                    current_snapshot=snapshot,
                )
                ticket = runtime._ensure_runtime_recovery_ticket(
                    failure,
                    snapshot,
                    decision.incident_id,
                )
                self.assertIsNotNone(ticket)
                assert ticket is not None
                alternative = next(
                    ref
                    for ref in snapshot.proxy_nodes
                    if ref.proxy_name == "good-next"
                )
                runtime.reload_core_config = lambda **_kwargs: self.fail(
                    "runtime recovery must not reload Core"
                )
                runtime._start_runtime_connection_cleanup = lambda *_args, **_kwargs: self.fail(
                    "runtime recovery must not close connections"
                )
                if failure_kind == "replacement_failed":
                    runtime._fast_recover_confirmed_alternative = (
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            RuntimeError("selector commit failed")
                        )
                    )
                else:
                    runtime._fast_recover_confirmed_alternative = (
                        lambda *_args, **_kwargs: {
                            "effective_egress_ok": True,
                            "post_switch_connectivity_verified": True,
                            "post_switch_result": {"status": 204},
                            "loaded_route_epoch": snapshot.route_epoch + 1,
                            "selection_action": {
                                "group": "TIER1-AUTO",
                                "target": alternative.proxy_name,
                                "mode": "fixed",
                            },
                        }
                    )
                    runtime._try_arm_runtime_recovery = lambda: False

                outcome = runtime._run_runtime_early_confirmed_recovery(
                    failure,
                    snapshot,
                    payload,
                    ticket=ticket,
                    probe_token=decision.probe_token,
                    alternative=alternative,
                    alternative_probe_result={"delay": 7},
                )

                self.assertIsNotNone(outcome)
                assert outcome is not None
                self.assertIs(outcome.status, RuntimeRecoveryStatus.CONTROLLER_HOLD)
                self.assertFalse(manager.effective_egress_ok)
                self.assertFalse(
                    runtime._runtime_egress_readiness.validation_active
                )
                self.assertEqual(
                    runtime._runtime_egress_readiness.state,
                    RuntimeEgressReadinessState.CONTROLLER_HOLD
                    if failure_kind == "arm_failed"
                    else RuntimeEgressReadinessState.DATA_PLANE_FAILED,
                )

    def test_unrelated_outbound_failure_is_ignored_not_global_facility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, _bad, _good, snapshot, payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            facility_reasons: list[str] = []
            runtime._record_runtime_facility_degradation = lambda reason: (
                facility_reasons.append(str(reason))
                or {"failure_domain": "facility"}
            )
            event = CoreOutputEvent(
                raw_line=(
                    "[TCP] dial BACKGROUND-SOURCE (match Match/) "
                    "127.0.0.1:55231 --> unrelated.example.com:443 "
                    "error: i/o timeout"
                ),
                pid=snapshot.process_pid,
                process_generation=snapshot.process_generation,
                received_at_monotonic=time.monotonic(),
            )

            runtime._handle_runtime_output_event(event, snapshot, payload)

            self.assertEqual(facility_reasons, [])
            self.assertEqual(manager.runtime_observations, [])

    def test_early_profile_enqueues_core_output_without_guard_self_wake(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _manager, _bad, _good, snapshot, _payload = (
                self._global_runtime_failure_fixture(Path(temp_dir))
            )
            runtime._runtime_failure_queue = queue.Queue(maxsize=512)
            now = time.monotonic()
            for index in range(513):
                runtime._on_core_output_event(
                    self._transport_output_event(
                        f"burst-{index}.example.com",
                        observed_at=now + (index / 10_000.0),
                        pid=snapshot.process_pid,
                        process_generation=snapshot.process_generation,
                    )
                )
            queue_state = runtime._runtime_egress_coordinator.queue_snapshot()
            self.assertEqual(queue_state.pending_count, 512)
            self.assertEqual(queue_state.dropped_count, 1)
            self.assertFalse(queue_state.overflow_guard_required)

            guard_submitted = threading.Event()
            submissions: list[tuple[str, str]] = []
            runtime._probe_mihomo_mixed_port_once = lambda **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("neutral guard failed"))

            def submit(
                reason: str,
                *,
                source: str = "runtime_probe",
                **_authority: object,
            ) -> bool:
                submissions.append((reason, source))
                guard_submitted.set()
                return True

            runtime._submit_current_route_probe_failure = submit
            runtime._maybe_probe_runtime_failure_guard()
            self.assertFalse(guard_submitted.wait(0.05))
            self.assertEqual(submissions, [])
            self.assertFalse(
                runtime._runtime_egress_coordinator.queue_snapshot().overflow_guard_required
            )

    def test_gateway_failure_is_attributed_to_underlay_or_facility_never_both(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            gateway = Node(
                name="fixed-gateway",
                protocol="ss",
                uri="ss://fixed-gateway",
                address="gateway.example.com",
                port=9443,
                tier="exit",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            bad_underlay = Node(
                name="bad-underlay",
                protocol="ss",
                uri="ss://bad-underlay",
                address="bad-underlay.example.com",
                port=8388,
                tier="tier1",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            good_underlay = Node(
                name="good-underlay",
                protocol="ss",
                uri="ss://good-underlay",
                address="good-underlay.example.com",
                port=8389,
                tier="tier1",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager = FakeTier3Manager()
            manager.runtime_nodes["exit"] = [gateway]
            manager.runtime_nodes["tier1"] = [bad_underlay, good_underlay]
            runtime.manager = manager
            gateway_ref = RuntimeNodeRef(
                "exit",
                "gateway",
                manager._node_identity(gateway),
                "fixed-gateway",
            )
            bad_ref = RuntimeNodeRef(
                "tier1",
                "tier1_variant",
                manager._node_identity(bad_underlay),
                "bad-underlay",
            )
            good_ref = RuntimeNodeRef(
                "tier1",
                "tier1_variant",
                manager._node_identity(good_underlay),
                "good-underlay",
            )
            snapshot = CoreLoadedRouteSnapshot(
                process_pid=4444,
                process_generation=3,
                core_generation=8,
                semantic_digest="gateway-digest",
                route_epoch=6,
                proxy_nodes=(gateway_ref, bad_ref, good_ref),
            )
            proxies: dict[str, object] = {
                "proxies": {
                    "GENERAL": {"now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "EXIT-SELECT",
                        "all": ["EXIT-SELECT", "REJECT"],
                    },
                    "EXIT-SELECT": {
                        "type": "Selector",
                        "now": "fixed-gateway",
                        "all": ["fixed-gateway"],
                    },
                    "fixed-gateway": {"type": "Shadowsocks"},
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "bad-underlay",
                        "all": ["bad-underlay", "good-underlay"],
                    },
                    "bad-underlay": {"type": "Shadowsocks"},
                    "good-underlay": {"type": "Shadowsocks"},
                }
            }
            payload = {
                "proxies": proxies,
                "proxy_map": {
                    "fixed-gateway": {
                        "node": gateway,
                        "proxy": {
                            "name": "fixed-gateway",
                            "dialer-proxy": "PROXY-FALLBACK",
                        },
                    },
                    "bad-underlay": {"node": bad_underlay},
                    "good-underlay": {"node": good_underlay},
                },
            }
            runtime.mihomo_proxies = lambda: copy.deepcopy(proxies)
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_snapshot = snapshot
                runtime._runtime_route_snapshot_payload = payload
            runtime._runtime_egress_coordinator.restart(current_snapshot=snapshot)
            gateway_binding = RuntimeRouteBinding(
                process_pid=4444,
                process_generation=3,
                core_generation=8,
                semantic_digest="gateway-digest",
                route_epoch=6,
                observed_outbound="GENERAL",
                route_chain=("GENERAL", "FINAL-EXIT", "EXIT-SELECT", "fixed-gateway"),
                node=gateway_ref,
            )
            gateway_failure = BoundRuntimeFailure(
                binding=gateway_binding,
                protocol="TCP",
                observed_outbound="GENERAL",
                destination_token="gateway-destination",
                error_type="timeout",
                observed_at_monotonic=time.monotonic(),
            )
            observation_ready = threading.Event()
            original_apply = manager.apply_runtime_health_observation

            def apply(observation):
                result = original_apply(observation)
                observation_ready.set()
                return result

            manager.apply_runtime_health_observation = apply
            runtime._probe_runtime_leaf = lambda ref, _payload, **_kwargs: (
                (_ for _ in ()).throw(RuntimeError("underlay failed"))
                if ref.proxy_name == "bad-underlay"
                else {"delay": 9, "name": ref.proxy_name}
            )
            runtime.audit_runtime_egress = lambda **_kwargs: {
                "post_switch_verified": True,
                "post_switch_connectivity_verified": True,
                "effective_egress_ok": True,
                "leaf_proxy": "good-underlay",
            }
            runtime._close_runtime_connections_for_binding = lambda _binding: 0
            first = runtime._runtime_egress_coordinator.observe(
                gateway_failure,
                current_snapshot=snapshot,
            )
            runtime._start_runtime_independent_probe(
                gateway_failure,
                snapshot,
                payload,
                probe_token=first.probe_token,
            )
            self.assertTrue(observation_ready.wait(1.0))
            self.assertEqual(len(manager.runtime_observations), 1)
            self.assertEqual(
                manager.runtime_observations[0]["identity"],
                bad_ref.identity,
            )
            self.assertNotEqual(
                manager.runtime_observations[0]["identity"],
                gateway_ref.identity,
            )

            # If neither the underlay nor any managed alternative can be
            # proven healthy, the failure domain stays facility-wide and no
            # node receives a health penalty.
            self.assertTrue(
                self._wait_until(
                    lambda: (
                        not runtime._runtime_failure_recovery_threads
                        and not runtime._runtime_failure_probe_bindings
                    ),
                    timeout=3.5,
                )
            )
            self.assertEqual(runtime._runtime_facility_hold_reason(), "")
            runtime._runtime_egress_coordinator.restart(current_snapshot=snapshot)
            manager.runtime_observations.clear()
            manager._runtime_unusable_refs.clear()
            runtime._probe_runtime_leaf = lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("facility-wide failure"))
            facility_recorded = threading.Event()
            facility_reasons: list[str] = []

            def record_facility(reason: str) -> dict[str, object]:
                facility_reasons.append(reason)
                facility_recorded.set()
                return {"failure_domain": "facility", "effective_egress_ok": False}

            runtime._record_runtime_facility_degradation = record_facility
            retry = runtime._runtime_egress_coordinator.observe(
                gateway_failure,
                current_snapshot=snapshot,
            )
            runtime._start_runtime_independent_probe(
                gateway_failure,
                snapshot,
                payload,
                probe_token=retry.probe_token,
            )
            self.assertTrue(
                facility_recorded.wait(3.5),
                msg=(
                    f"retry={retry!r} "
                    f"probe_bindings={runtime._runtime_failure_probe_bindings!r} "
                    f"recovery_bindings={runtime._runtime_failure_recovery_bindings!r} "
                    f"facility={runtime._runtime_facility_hold_reason()!r} "
                    f"lease={runtime._runtime_route_mutation_lease!r}"
                ),
            )
            self.assertEqual(manager.runtime_observations, [])
            self.assertTrue(facility_reasons)

    def test_runtime_connection_cleanup_never_closes_from_chain_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            binding = RuntimeRouteBinding(
                process_pid=4321,
                process_generation=2,
                core_generation=7,
                semantic_digest="digest",
                route_epoch=3,
                observed_outbound="GENERAL",
                route_chain=("GENERAL", "FINAL-EXIT", "bad-leaf"),
                node=RuntimeNodeRef(
                    "tier2",
                    "tier2",
                    "tier2-identity",
                    "bad-leaf",
                ),
            )
            runtime.mihomo_connections = lambda: {
                "connections": [
                    {"id": "owned", "chains": ["FINAL-EXIT", "bad-leaf"]},
                    {"id": "other", "chains": ["FINAL-EXIT", "good-leaf"]},
                    {"id": "unknown"},
                    {"id": "empty", "chains": []},
                ]
            }
            closed: list[str] = []
            runtime.close_mihomo_connection = lambda connection_id: (
                closed.append(connection_id) or {"closed": connection_id}
            )
            runtime.close_all_mihomo_connections = lambda: (_ for _ in ()).throw(
                AssertionError("global connection close is forbidden")
            )

            self.assertEqual(
                runtime._close_runtime_connections_for_binding(binding),
                0,
            )
            self.assertEqual(closed, [])

    def test_manual_neutral_failure_without_verified_backup_stays_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            manager = FakeRuntimeAuditManager()
            manager.effective_egress_ok = True
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._set_core_runtime_phase("formal_capture")
            runtime._set_effective_egress_state(True, "")
            self._install_manual_validation_authority(runtime)
            runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
            runtime._probe_mihomo_mixed_port_once = lambda **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("neutral 204 timeout"))
            order: list[str] = []
            submitted = threading.Event()
            identity_started = threading.Event()
            release_identity = threading.Event()

            def submit(_reason: str, *, source: str = "runtime_probe") -> bool:
                order.append(source)
                submitted.set()
                return True

            class BlockingIdentityService:
                def check(self, **_kwargs):
                    order.append("identity")
                    identity_started.set()
                    release_identity.wait(1.0)
                    return {
                        "state": "partial",
                        "identity": {
                            "ipv4": {"state": "unavailable", "ip": None},
                            "ipv6": {"state": "unavailable", "ip": None},
                        },
                        "route": {
                            "confirmed_before": True,
                            "confirmed_after": True,
                            "stable_during_check": True,
                        },
                        "diagnostics": {},
                    }

            runtime._submit_current_route_probe_failure = submit
            runtime.exit_quality_service = BlockingIdentityService()
            runtime._runtime_worker_token = lambda: None
            runtime._running_core_mixed_proxy_url = (
                lambda: "http://127.0.0.1:7890"
            )
            runtime._verify_final_exit_identity = (
                lambda *, request_id: BlockingIdentityService().check(
                    request_id=request_id
                )
            )
            runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("identity diagnostics must not own a Core audit")
            )
            result = runtime.check_exit_quality(
                proxy_url=None,
                request_id="neutral-first",
            )

            self.assertFalse(submitted.wait(0.05))
            self.assertFalse(identity_started.wait(0.05))
            self.assertEqual(order, [])
            self.assertEqual(
                result["diagnostics"]["neutral_connectivity"]["state"],
                "failed",
            )
            self.assertFalse(result["diagnostics"]["identity_pending"])

    def test_manual_selector_change_revokes_automatic_failback_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            runtime.api_client = FakeRuntimeAuditClient([{"proxies": {}}])
            queued: list[str] = []
            runtime._queue_runtime_route_snapshot_refresh = lambda: None
            runtime._queue_incremental_egress_recheck = lambda: queued.append(
                "revalidate"
            )
            binding = RuntimeRouteBinding(
                process_pid=4321,
                process_generation=2,
                core_generation=7,
                semantic_digest="digest",
                route_epoch=3,
                observed_outbound="FINAL-EXIT",
                route_chain=("FINAL-EXIT", "PROXY-FALLBACK", "old-leaf"),
                node=RuntimeNodeRef(
                    "tier1",
                    "tier1_variant",
                    "old-identity",
                    "old-leaf",
                ),
            )
            loaded_payload = {
                "proxies": {
                    "proxies": {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "PROXY-FALLBACK",
                            "all": ["PROXY-FALLBACK", "REJECT"],
                        },
                        "PROXY-FALLBACK": {
                            "type": "Selector",
                            "now": "new-leaf",
                            "all": ["old-leaf", "new-leaf"],
                        },
                    }
                }
            }
            lease = {
                "group": "FINAL-EXIT",
                "from": "old-leaf",
                "to": "new-leaf",
                "incident_id": "incident-1",
                "route_epoch": 3,
            }
            runtime._runtime_route_mutation_lease = dict(lease)

            runtime.select_mihomo_proxy(
                "PROXY-FALLBACK",
                "new-leaf",
                revalidate=False,
            )
            self.assertEqual(runtime._runtime_route_mutation_lease, lease)
            runtime.select_mihomo_proxy(
                "PROXY-FALLBACK",
                "user-leaf",
                revalidate=True,
            )
            self.assertIsNone(runtime._runtime_route_mutation_lease)
            self.assertEqual(queued, ["revalidate"])

            before_restore = list(runtime.api_client.select_calls)
            restored = runtime._restore_runtime_selector_path(
                binding,
                loaded_payload,
                incident_id="incident-1",
            )
            self.assertEqual(restored, [])
            self.assertEqual(runtime.api_client.select_calls, before_restore)

    def _runtime_failback_fixture(self, temp_path: Path):
        runtime, manager, _bad, _good, failed_snapshot, failed_payload = (
            self._global_runtime_failure_fixture(temp_path)
        )
        recovered_ref = next(
            ref
            for ref in failed_snapshot.proxy_nodes
            if ref.proxy_name == "bad-current"
        )
        backup_ref = next(
            ref
            for ref in failed_snapshot.proxy_nodes
            if ref.proxy_name == "good-next"
        )
        failed_binding = runtime._runtime_route_binding_for_outbound(
            failed_snapshot,
            failed_payload,
            "FINAL-EXIT",
        )
        self.assertIsNotNone(failed_binding)
        assert failed_binding is not None
        self.assertEqual(failed_binding.node.key, recovered_ref.key)

        backup_payload = copy.deepcopy(failed_payload)
        backup_payload["proxies"]["proxies"]["TIER1-AUTO"]["now"] = (
            "good-next"
        )
        backup_snapshot = CoreLoadedRouteSnapshot(
            process_pid=failed_snapshot.process_pid,
            process_generation=failed_snapshot.process_generation,
            core_generation=failed_snapshot.core_generation,
            semantic_digest=failed_snapshot.semantic_digest,
            route_epoch=failed_snapshot.route_epoch + 1,
            group_graph=failed_snapshot.group_graph,
            proxy_nodes=failed_snapshot.proxy_nodes,
        )
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = backup_snapshot
            runtime._runtime_route_snapshot_payload = backup_payload
        runtime._runtime_egress_coordinator.restart(
            current_snapshot=backup_snapshot
        )
        runtime._set_core_runtime_phase("formal_capture")

        incident_id = "failback-incident"
        runtime._runtime_incident_bindings[incident_id] = failed_binding
        runtime._runtime_incident_route_payloads[incident_id] = failed_payload
        runtime._runtime_incident_recovery_state[incident_id] = "recovered"
        failback_now = time.monotonic()
        runtime._runtime_route_mutation_lease = {
            "group": "TIER1-AUTO",
            "from": recovered_ref.proxy_name,
            "to": backup_ref.proxy_name,
            "target_member": backup_ref.proxy_name,
            "incident_id": incident_id,
            "route_epoch": failed_binding.route_epoch,
            "mode": "selector",
            "selector_intent_revision": runtime._runtime_selector_intent_revision,
            "verified_at_monotonic": failback_now - 31.0,
        }
        runtime._runtime_last_verified_route = {
            "verified_at_monotonic": failback_now - 31.0,
            "incident_id": incident_id,
            "leaf_proxy": backup_ref.proxy_name,
        }
        runtime._runtime_failback_pass_history[recovered_ref.key] = [
            failback_now - 6.0
        ]
        payload = {
            "incident_id": incident_id,
            "transition": {"action": "recovered"},
        }
        return (
            runtime,
            manager,
            payload,
            recovered_ref,
            backup_ref,
            backup_snapshot,
            backup_payload,
            failed_payload,
        )

    def test_runtime_failback_clears_lease_only_after_exact_leaf_and_204(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            (
                runtime,
                manager,
                payload,
                recovered_ref,
                _backup_ref,
                backup_snapshot,
                backup_payload,
                promoted_payload,
            ) = self._runtime_failback_fixture(Path(temp_dir))
            incident_id = str(payload["incident_id"])
            lease = dict(runtime._runtime_route_mutation_lease)
            recovered_hold_key = runtime._runtime_leaf_export_hold_key(
                recovered_ref
            )
            runtime._runtime_failback_export_holds.add(recovered_hold_key)
            reload_calls: list[bool] = []
            restore_calls: list[tuple[str, str]] = []
            leaf_probes: list[str] = []
            refresh_states = iter(
                (
                    (backup_snapshot, backup_payload),
                    (
                        CoreLoadedRouteSnapshot(
                            process_pid=backup_snapshot.process_pid,
                            process_generation=backup_snapshot.process_generation,
                            core_generation=backup_snapshot.core_generation,
                            semantic_digest=backup_snapshot.semantic_digest,
                            route_epoch=backup_snapshot.route_epoch + 1,
                            group_graph=backup_snapshot.group_graph,
                            proxy_nodes=backup_snapshot.proxy_nodes,
                        ),
                        promoted_payload,
                    ),
                )
            )

            def refresh(**_kwargs):
                snapshot, route_payload = next(refresh_states)
                with runtime._runtime_route_snapshot_lock:
                    runtime._runtime_route_snapshot = snapshot
                    runtime._runtime_route_snapshot_payload = route_payload
                return snapshot

            runtime._refresh_runtime_route_snapshot = refresh
            runtime._hot_reload_runtime_audit_overlay = (
                lambda *, queue_revalidation=False, **_kwargs: (
                    reload_calls.append(bool(queue_revalidation))
                    or {"reloaded": True}
                )
            )
            runtime._probe_runtime_leaf = (
                lambda ref, _route_payload, **_kwargs: (
                    leaf_probes.append(ref.proxy_name)
                    or {"delay": 8, "name": ref.proxy_name}
                )
            )
            runtime._restore_runtime_selector_path = (
                lambda binding, _route_payload, *, incident_id, **_kwargs: (
                    restore_calls.append(
                        (binding.node.proxy_name, str(incident_id))
                    )
                    or [{"group": "TIER1-AUTO", "proxy": "bad-current"}]
                )
            )
            neutral_started = threading.Event()
            release_neutral = threading.Event()

            def verify_204(**_kwargs):
                current_snapshot, current_payload = (
                    runtime._runtime_route_snapshot_state()
                )
                self.assertIsNotNone(current_snapshot)
                self.assertIsInstance(current_payload, dict)
                promoted = runtime._runtime_route_binding_for_outbound(
                    current_snapshot,
                    current_payload,
                    "FINAL-EXIT",
                )
                self.assertIsNotNone(promoted)
                assert promoted is not None
                self.assertEqual(promoted.node.key, recovered_ref.key)
                neutral_started.set()
                self.assertTrue(release_neutral.wait(1.0))
                return {"status": 204, "latency_ms": 9}

            runtime._verify_runtime_final_exit = verify_204

            runtime._start_runtime_failback(payload, recovered_ref)

            self.assertTrue(neutral_started.wait(1.0))
            # Exact route selection alone is insufficient: the automatic
            # lease remains live until the neutral 204 has completed.
            self.assertEqual(runtime._runtime_route_mutation_lease, lease)
            release_neutral.set()
            self.assertTrue(
                self._wait_until(
                    lambda: not runtime._runtime_failure_recovery_threads,
                    timeout=1.0,
                )
            )
            self.assertIsNone(runtime._runtime_route_mutation_lease)
            self.assertEqual(
                runtime._runtime_incident_recovery_state[incident_id],
                "promoted",
            )
            self.assertNotIn(
                recovered_hold_key,
                runtime._runtime_failback_export_holds,
            )
            self.assertEqual(reload_calls, [])
            self.assertEqual(leaf_probes, [recovered_ref.proxy_name])
            self.assertEqual(
                restore_calls,
                [(recovered_ref.proxy_name, incident_id)],
            )
            self.assertEqual(
                runtime._runtime_last_verified_route["leaf_proxy"],
                recovered_ref.proxy_name,
            )
            self.assertTrue(manager.effective_states[-1][0])

    def test_runtime_failback_failure_rolls_back_verified_backup_and_retries(
        self,
    ) -> None:
        for failure_mode in ("other_leaf", "neutral_204"):
            with self.subTest(failure_mode=failure_mode), tempfile.TemporaryDirectory() as temp_dir:
                (
                    runtime,
                    manager,
                    payload,
                    recovered_ref,
                    backup_ref,
                    backup_snapshot,
                    backup_payload,
                    recovered_payload,
                ) = self._runtime_failback_fixture(Path(temp_dir))
                incident_id = str(payload["incident_id"])
                lease = dict(runtime._runtime_route_mutation_lease)
                recovered_hold_key = runtime._runtime_leaf_export_hold_key(
                    recovered_ref
                )
                runtime._runtime_failback_export_holds.add(
                    recovered_hold_key
                )
                promoted_payload = (
                    backup_payload
                    if failure_mode == "other_leaf"
                    else recovered_payload
                )
                snapshots = tuple(
                    CoreLoadedRouteSnapshot(
                        process_pid=backup_snapshot.process_pid,
                        process_generation=backup_snapshot.process_generation,
                        core_generation=backup_snapshot.core_generation,
                        semantic_digest=backup_snapshot.semantic_digest,
                        route_epoch=backup_snapshot.route_epoch + offset,
                        group_graph=backup_snapshot.group_graph,
                        proxy_nodes=backup_snapshot.proxy_nodes,
                    )
                    for offset in range(1, 5)
                )
                refresh_states = iter(
                    (
                        (snapshots[0], backup_payload),
                        (snapshots[1], promoted_payload),
                        (snapshots[2], promoted_payload),
                        (snapshots[3], backup_payload),
                    )
                )
                refresh_calls: list[int] = []

                def refresh(**_kwargs):
                    snapshot, route_payload = next(refresh_states)
                    refresh_calls.append(snapshot.route_epoch)
                    with runtime._runtime_route_snapshot_lock:
                        runtime._runtime_route_snapshot = snapshot
                        runtime._runtime_route_snapshot_payload = route_payload
                    return snapshot

                runtime._refresh_runtime_route_snapshot = refresh
                reload_calls: list[bool] = []
                runtime._hot_reload_runtime_audit_overlay = (
                    lambda *, queue_revalidation=False, **_kwargs: (
                        reload_calls.append(bool(queue_revalidation))
                        or {"reloaded": True}
                    )
                )
                runtime._probe_runtime_leaf = (
                    lambda ref, _route_payload, **_kwargs: {
                        "delay": 8,
                        "name": ref.proxy_name,
                    }
                )
                restore_calls: list[str] = []
                runtime._restore_runtime_selector_path = (
                    lambda binding, _route_payload, *, incident_id, **_kwargs: (
                        restore_calls.append(binding.node.proxy_name)
                        or [{"group": "TIER1-AUTO", "proxy": binding.node.proxy_name}]
                    )
                )
                automatic_restore_calls: list[tuple[str, str]] = []
                runtime._apply_runtime_automatic_selection = (
                    lambda action, **_kwargs: (
                        automatic_restore_calls.append(
                            (
                                str(action.get("group") or ""),
                                str(action.get("target") or ""),
                            )
                        )
                        or {"ok": True}
                    )
                )
                neutral_calls: list[str] = []

                def verify_204(**_kwargs):
                    neutral_calls.append("neutral")
                    if failure_mode == "neutral_204" and len(neutral_calls) == 1:
                        raise RuntimeError("promoted leaf 204 failed")
                    return {"status": 204, "latency_ms": 7}

                runtime._verify_runtime_final_exit = verify_204
                runtime._submit_current_route_probe_failure = (
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError(
                            "promotion failure must not create a node incident"
                        )
                    )
                )
                retry_requested = threading.Event()
                retry_calls: list[tuple[str, tuple[str, str, str, str]]] = []
                original_start = runtime._start_runtime_failback

                def record_retry(retry_payload, retry_ref):
                    retry_calls.append(
                        (str(retry_payload.get("incident_id") or ""), retry_ref.key)
                    )
                    retry_requested.set()

                runtime._start_runtime_failback = record_retry
                original_start(payload, recovered_ref)

                self.assertTrue(retry_requested.wait(1.0))
                self.assertTrue(
                    self._wait_until(
                        lambda: not runtime._runtime_failure_recovery_threads,
                        timeout=1.0,
                    )
                )
                self.assertEqual(len(refresh_calls), 4)
                self.assertEqual(reload_calls, [])
                self.assertEqual(
                    restore_calls,
                    [recovered_ref.proxy_name],
                )
                self.assertEqual(
                    automatic_restore_calls,
                    [("TIER1-AUTO", backup_ref.proxy_name)],
                )
                self.assertEqual(
                    len(neutral_calls),
                    1 if failure_mode == "other_leaf" else 2,
                )
                self.assertIn(
                    recovered_hold_key,
                    runtime._runtime_failback_export_holds,
                )
                self.assertEqual(runtime._runtime_route_mutation_lease, lease)
                self.assertEqual(
                    runtime._runtime_failback_failures[recovered_ref.key],
                    1,
                )
                self.assertEqual(
                    runtime._runtime_last_verified_route["leaf_proxy"],
                    backup_ref.proxy_name,
                )
                self.assertTrue(
                    runtime._runtime_last_verified_route["promotion_rollback"]
                )
                self.assertTrue(manager.effective_states[-1][0])
                self.assertEqual(
                    retry_calls,
                    [(incident_id, recovered_ref.key)],
                )

    @staticmethod
    def _subscription_refresh_runtime() -> DesktopRuntime:
        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime._runtime_lifecycle_lock = threading.RLock()
        runtime.logger = logging.getLogger("subscription-refresh-runtime-test")
        runtime.logger.handlers.clear()
        runtime.logger.addHandler(logging.NullHandler())
        runtime.manager = None
        runtime.core_runner = None
        return runtime

    def test_runtime_config_helpers_preserve_defaults_for_invalid_nested_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)

            runtime.config["client"]["mihomo_controller"] = {
                "host": "10.0.0.2",
                "port": "19090",
            }
            self.assertEqual(runtime.mihomo_controller_base_url(), "http://10.0.0.2:19090")

            runtime.config["client"]["mihomo_controller"] = "invalid"
            runtime.config["client"]["system_proxy"] = "invalid"
            self.assertEqual(runtime._mihomo_controller_config(), {})
            self.assertEqual(runtime._system_proxy_config(), {})
            self.assertEqual(runtime.mihomo_controller_base_url(), "http://127.0.0.1:9090")

            runtime.config["client"] = "invalid"
            self.assertEqual(runtime._client_config(), {})
            self.assertEqual(runtime._client_mixed_port(), 7890)

    def test_status_suppresses_stale_start_deferred_when_core_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            runtime.manager = None
            runtime.running = True
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            runtime.last_error = "Startup blocked: stale deferred core gate check"

            status = runtime.status(include_runtime_nodes=False)

            self.assertTrue(status["core_running"])
            self.assertFalse(status["start_deferred"])
            self.assertIsNone(status["last_error"])

    def test_status_exposes_protocols_only_for_current_loaded_core_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            runtime.manager = None
            runtime.running = True
            runtime.last_error = None
            runner = FakeCoreRunner(running=True, port_ok=True)
            runtime.core_runner = runner
            runtime_config_path = temp_path / "mihomo-runtime.yaml"
            runtime_config = """
proxies:
  - {name: edge-a, type: vless}
  - {name: edge-b, type: vless}
  - {name: backup, type: ss}
"""
            runtime_config_path.write_text(runtime_config, encoding="utf-8")
            runtime._runtime_config_path_for_client = lambda: runtime_config_path
            binding = build_node_diagnostic_core_binding(
                runtime_config_path.read_bytes(),
                generation=3,
                core_generation=runtime._core_generation,
            )
            runtime._node_diagnostic_core_binding = binding
            runtime._node_diagnostic_binding_client = runtime.api_client
            runtime._node_diagnostic_binding_runner = runner

            status = runtime.status(include_runtime_nodes=False)
            self.assertEqual(
                status["core"]["loaded_protocol_counts"],
                {"ss": 1, "vless": 2},
            )

            runtime_config_path.write_text(
                "proxies:\n  - {name: staged-only, type: vmess}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                runtime.status(include_runtime_nodes=False)["core"]["loaded_protocol_counts"],
                {},
            )

            runner.stop()
            self.assertEqual(
                runtime.status(include_runtime_nodes=False)["core"]["loaded_protocol_counts"],
                {},
            )

    def test_diagnose_subscription_delegates_to_temporary_preview_service(self) -> None:
        calls: list[tuple[dict[str, object], str]] = []

        class FakePreviewService:
            def diagnose_subscription(self, item, request_id):
                calls.append((item, request_id))
                return {
                    "request_id": request_id,
                    "tier": item["tier"],
                    "url": item["url"],
                    "diagnostic_only": True,
                    "status": "completed",
                }

        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime.subscription_preview_service = FakePreviewService()
        runtime.manager = object()
        runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
        runtime.reload_core_config = lambda **_kwargs: self.fail(
            "manual diagnosis must not reload the core"
        )
        item = {
            "tier": "tier2",
            "kind": "subscription",
            "url": "https://sub.example/tier2",
        }

        result = runtime.diagnose_subscription(item, "request-1")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "request-1")
        self.assertIsNot(calls[0][0], item)
        self.assertTrue(result["diagnostic_only"])
        self.assertNotIn("stored", result)
        self.assertNotIn("applied", result)
        self.assertNotIn("retained_previous", result)

    def test_refresh_subscription_validates_subscription_identity(self) -> None:
        runtime = DesktopRuntime.__new__(DesktopRuntime)

        invalid_items = (
            None,
            {"tier": "tier2", "kind": "node", "url": "https://sub.example/list"},
            {"tier": "tier2", "kind": "subscription", "url": ""},
            {"tier": "tier1", "kind": "subscription", "url": "https://sub.example/list"},
        )
        for item in invalid_items:
            with self.subTest(item=item), self.assertRaises(ValueError):
                runtime.refresh_subscription(item)

    def test_refresh_subscription_offline_uses_owned_manager_without_core_reload(self) -> None:
        runtime = self._subscription_refresh_runtime()
        alive = Node(
            name="sub-node",
            protocol="vless",
            uri="vless://sub",
            address="sub.example",
            port=443,
            tier="tier2",
            status=NodeStatus.ALIVE,
        )
        calls: list[object] = []

        class FakeManager:
            def refresh_subscription_url_system(self, tier: str, url: str):
                calls.append(("refresh", tier, url))
                return [alive]

        manager = FakeManager()
        runtime.core_runner = FakeCoreRunner(running=False, port_ok=False)
        runtime._ensure_subscription_refresh_manager = (
            lambda *, target_tier=None: calls.append(("ensure", target_tier)) or manager
        )
        runtime._release_subscription_refresh_manager = (
            lambda released: calls.append(("release", released))
        )
        runtime.reload_core_config = lambda **_kwargs: self.fail(
            "an offline refresh must not reload the core"
        )

        result = runtime.refresh_subscription(
            {
                "tier": "tier2",
                "kind": "subscription",
                "url": "https://sub.example/list",
                "label": "Primary subscription",
            }
        )

        self.assertEqual(
            calls,
            [
                ("ensure", "tier2"),
                ("refresh", "tier2", "https://sub.example/list"),
                ("release", manager),
            ],
        )
        self.assertTrue(result["stored"])
        self.assertFalse(result["core_running"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["retained_previous"])
        self.assertIsNone(result["apply_error"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["healthy"], 1)

    def test_refresh_subscription_hot_applies_when_core_is_running(self) -> None:
        runtime = self._subscription_refresh_runtime()
        alive = Node(
            name="exit-node",
            protocol="trojan",
            uri="trojan://sub",
            address="exit.example",
            port=443,
            tier="exit",
            status=NodeStatus.ALIVE,
        )
        calls: list[object] = []

        class FakeManager:
            def refresh_subscription_url_system(self, tier: str, url: str):
                calls.append(("refresh", tier, url))
                return [alive]

        runtime.manager = FakeManager()
        runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
        runtime.reload_core_config = (
            lambda **_kwargs: calls.append("reload") or {"reloaded": True}
        )

        result = runtime.refresh_subscription(
            {
                "tier": "exit",
                "kind": "subscription",
                "key": "https://sub.example/exit",
            }
        )

        self.assertEqual(
            calls,
            [("refresh", "exit", "https://sub.example/exit"), "reload"],
        )
        self.assertTrue(result["stored"])
        self.assertTrue(result["core_running"])
        self.assertTrue(result["applied"])
        self.assertFalse(result["retained_previous"])
        self.assertIsNone(result["apply_error"])
        self.assertEqual(result["reload"], {"reloaded": True})

    def test_refresh_subscription_reports_saved_but_not_applied(self) -> None:
        runtime = self._subscription_refresh_runtime()
        alive = Node(
            name="sub-node",
            tier="tier2",
            status=NodeStatus.ALIVE,
        )

        class FakeManager:
            def refresh_subscription_url_system(self, _tier: str, _url: str):
                return [alive]

        runtime.manager = FakeManager()
        runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
        runtime.reload_core_config = lambda **_kwargs: {
            "kept_existing_core": True,
            "reason": "controller rejected config",
        }

        result = runtime.refresh_subscription(
            {
                "tier": "tier2",
                "kind": "subscription",
                "url": "https://sub.example/list",
            }
        )

        self.assertTrue(result["stored"])
        self.assertTrue(result["core_running"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["retained_previous"])
        self.assertEqual(result["apply_error"], "controller rejected config")

    def test_refresh_subscription_failure_retains_previous_and_skips_reload(self) -> None:
        runtime = self._subscription_refresh_runtime()
        calls: list[str] = []

        class FakeManager:
            cache_marker = "previous"

            def refresh_subscription_url_system(self, _tier: str, _url: str):
                raise RuntimeError("download failed")

        manager = FakeManager()
        runtime.manager = manager
        runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
        runtime.reload_core_config = (
            lambda **_kwargs: calls.append("reload") or {"reloaded": True}
        )

        result = runtime.refresh_subscription(
            {
                "tier": "tier2",
                "kind": "subscription",
                "url": "https://sub.example/list",
            }
        )

        self.assertFalse(result["stored"])
        self.assertTrue(result["core_running"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["retained_previous"])
        self.assertIsNone(result["apply_error"])
        self.assertEqual(result["error"], "download failed")
        self.assertEqual(manager.cache_marker, "previous")
        self.assertEqual(calls, [])

    def test_subscription_diagnostic_cancellation_delegates_to_service(self) -> None:
        class FakePreviewService:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            def cancel(self, request_id):
                self.cancelled.append(request_id)
                return True

            def cancel_all(self):
                return 3

        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime.subscription_preview_service = FakePreviewService()

        self.assertTrue(runtime.cancel_subscription_diagnosis("request-2"))
        self.assertEqual(runtime.subscription_preview_service.cancelled, ["request-2"])
        self.assertEqual(runtime.cancel_subscription_diagnostics(), 3)

    def test_import_subscription_runs_initial_tier_refresh_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            alive = Node(
                name="sub-node",
                protocol="vless",
                uri="vless://sub",
                address="sub.example",
                port=443,
                tier="tier2",
                status=NodeStatus.ALIVE,
            )
            calls: list[object] = []

            class FakeManager:
                def __init__(self) -> None:
                    self.subscription_refreshes: list[tuple[str, str]] = []

                def refresh_subscription_url_system(self, tier: str, url: str):
                    self.subscription_refreshes.append((tier, url))
                    return [alive]

                def check_tier3(self, force_collect: bool = False, *, suppress_intervention_alert: bool = False):
                    raise AssertionError("tier3 check must not run during tier2 subscription import")

            manager = FakeManager()
            runtime.manager = manager
            runtime.import_service.import_tier2_subscription = (
                lambda payload: calls.append(("import", payload)) or {"path": "config.local.yaml"}
            )
            runtime.reload_config = lambda **kwargs: calls.append(("reload", kwargs)) or {}
            runtime.reload_core_config = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("import refresh should not hot reload directly"))

            result = runtime.import_tier2_subscription({"url": "https://sub.example/list"})

            self.assertEqual(calls, [("import", {"url": "https://sub.example/list"}), ("reload", {"target_tier": "tier2"})])
            self.assertEqual(manager.subscription_refreshes, [("tier2", "https://sub.example/list")])
            self.assertEqual(result["refresh"]["url"], "https://sub.example/list")
            self.assertEqual(result["refresh"]["total"], 1)
            self.assertEqual(result["refresh"]["healthy"], 1)

    def test_import_subscription_creates_refresh_manager_when_runtime_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            alive = Node(
                name="sub-node",
                protocol="vless",
                uri="vless://sub",
                address="sub.example",
                port=443,
                tier="tier2",
                status=NodeStatus.ALIVE,
            )
            calls: list[object] = []

            class FakeManager:
                instances: list["FakeManager"] = []

                def __init__(self, **kwargs: object) -> None:
                    self.subscription_operation_coordinator = kwargs.get(
                        "subscription_operation_coordinator"
                    )
                    self.subscription_refreshes: list[tuple[str, str]] = []
                    self.runtime_nodes = {
                        "tier1": [Node(name="tier1-preloaded", tier="tier1")],
                        "tier2": [],
                        "tier3": [Node(name="tier3-preloaded", tier="tier3", status=NodeStatus.ALIVE)],
                        "exit": [Node(name="exit-preloaded", tier="exit")],
                    }
                    self.last_pool_alive_count = 1
                    self.tier3_source_count = 1
                    self.tier3_level = 1
                    self.tier3_level_reason = "steady"
                    self.tier3_last_collection_report = {"preloaded": True}
                    self.tier3_last_collection_at = "previous"
                    self.exit_last_refresh_at = "previous"
                    self.closed = False
                    FakeManager.instances.append(self)

                def refresh_subscription_url_system(self, tier: str, url: str):
                    self.subscription_refreshes.append((tier, url))
                    self.runtime_nodes[tier] = [alive]
                    return [alive]

                def check_tier3(self, force_collect: bool = False, *, suppress_intervention_alert: bool = False):
                    raise AssertionError("tier3 check must not run during tier2 subscription import")

                def close(self) -> None:
                    self.closed = True

            runtime.import_service.import_tier2_subscription = (
                lambda payload: calls.append(("import", payload)) or {"path": "config.local.yaml"}
            )
            runtime.reload_config = lambda **kwargs: calls.append(("reload", kwargs)) or {}
            self.assertIsNone(runtime.manager)

            with patch("desktop_runtime.ProxyTierManager", FakeManager):
                result = runtime.import_tier2_subscription({"url": "https://sub.example/list"})

            self.assertEqual(calls, [("import", {"url": "https://sub.example/list"}), ("reload", {"target_tier": "tier2"})])
            self.assertEqual(len(FakeManager.instances), 1)
            self.assertIsNone(runtime.manager)
            self.assertIsNone(runtime._offline_subscription_manager)
            self.assertTrue(FakeManager.instances[0].closed)
            self.assertEqual(FakeManager.instances[0].subscription_refreshes, [("tier2", "https://sub.example/list")])
            self.assertEqual(FakeManager.instances[0].runtime_nodes["tier1"], [])
            self.assertEqual(FakeManager.instances[0].runtime_nodes["tier3"], [])
            self.assertEqual(FakeManager.instances[0].runtime_nodes["exit"], [])
            self.assertEqual(FakeManager.instances[0].last_pool_alive_count, 0)
            self.assertEqual(result["refresh"]["url"], "https://sub.example/list")
            self.assertEqual(result["refresh"]["total"], 1)
            self.assertEqual(result["refresh"]["healthy"], 1)
            self.assertTrue(result["refresh"]["stored"])
            self.assertTrue(result["refresh"]["system_owned"])
            self.assertIs(
                FakeManager.instances[0].subscription_operation_coordinator,
                runtime.subscription_operation_coordinator,
            )

    def test_manual_diagnosis_without_core_does_not_create_manager(self) -> None:
        class FakePreviewService:
            def diagnose_subscription(self, item, request_id):
                return {
                    "request_id": request_id,
                    "tier": item["tier"],
                    "url": item["url"],
                    "diagnostic_only": True,
                    "status": "completed",
                    "nodes": [],
                }

        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime.manager = None
        runtime.core_runner = None
        runtime._offline_subscription_manager = None
        runtime.subscription_preview_service = FakePreviewService()

        with patch(
            "desktop_runtime.ProxyTierManager",
            side_effect=AssertionError("manual diagnosis must not create Manager"),
        ):
            result = runtime.diagnose_subscription(
                {
                    "tier": "tier2",
                    "kind": "subscription",
                    "url": "https://sub.example/offline",
                },
                "request-offline",
            )

        self.assertTrue(result["diagnostic_only"])
        self.assertIsNone(runtime.manager)
        self.assertIsNone(runtime._offline_subscription_manager)

    def test_tier3_pool_alive_change_updates_counter_without_alert(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)

            class FakeManager:
                last_pool_alive_count = 1

            sent = []
            runtime.alerts.send_event = lambda event: sent.append(event) or True

            manager = FakeManager()
            runtime._notify_tier3_pool_alive_change(manager, 1, 3)

            self.assertEqual(manager.last_pool_alive_count, 3)
            self.assertEqual(sent, [])

    def test_invalid_alerts_section_does_not_break_runtime_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = DesktopRuntime(
                {
                    "alerts": "invalid",
                    "client": {
                        "runtime_state_path": str(temp_path / "runtime-state.json"),
                        "system_proxy": {"enabled": False},
                    },
                },
                project_root=temp_path,
            )
            for handler in list(runtime.logger.handlers):
                handler.close()
                runtime.logger.removeHandler(handler)

            self.assertEqual(runtime.alerts_config, {})

    def test_set_network_mode_repairs_invalid_local_client_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            runtime.app_paths.local_config_path.write_text("client: invalid\n", encoding="utf-8")
            runtime.network_mode = "tun"
            runtime.reload_config = lambda **_kwargs: {}

            result = runtime.set_network_mode("system_proxy")

            self.assertTrue(result["changed"])
            self.assertEqual(runtime.config_store.read_local()["client"], {"network_mode": "system_proxy"})

    def test_config_mutation_wrappers_reload_after_successful_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            calls: list[str] = []
            runtime.config_store.write_sources = lambda payload: calls.append("write") or {"path": "sources.yaml"}
            runtime.reload_config = lambda **kwargs: calls.append(("reload", kwargs)) or {}

            result = runtime.write_sources_config({"github_repos": []})

            self.assertEqual(result, {"path": "sources.yaml"})
            self.assertEqual(calls, ["write", ("reload", {"target_tier": None})])

    def test_reload_config_does_not_restart_traffic_monitor_for_tier3_standby_or_stop_intent(
        self,
    ) -> None:
        scenarios = (
            {
                "name": "tier3_standby",
                "effective_egress_ok": False,
                "traffic_capture_active": False,
                "stop_requested": False,
            },
            {
                "name": "stop_intent",
                "effective_egress_ok": True,
                "traffic_capture_active": True,
                "stop_requested": True,
            },
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    backend = MemoryProxyBackend(
                        ProxySettings(enabled=False, server="", override=False)
                    )
                    runtime = self._runtime(temp_path, backend)
                    loaded_config = {
                        "client": {
                            "runtime_state_path": str(
                                temp_path / "runtime-state.json"
                            ),
                            "system_proxy": {
                                "enabled": True,
                                "host": "127.0.0.1",
                                "port": 7890,
                            },
                        },
                        "tiers": {
                            "tier1": {"nodes": []},
                            "tier2": {
                                "nodes": [],
                                "subscription_urls": [],
                            },
                            "tier3": {
                                "enabled": True,
                                "min_alive_nodes": 1,
                            },
                        },
                        "alerts": {
                            "log_file": str(temp_path / "monitor.log"),
                            "desktop": {"enabled": False},
                        },
                        "egress_proxy": {
                            "enabled": True,
                            "scheme": "http",
                            "server": "raw-explicit.example",
                            "port": 8080,
                            "use_for": {"collection": True},
                        },
                    }
                    manager = FakeTier3Manager()
                    manager.runtime_nodes["tier3"] = [
                        Node(
                            name="standby-node",
                            protocol="ss",
                            uri="ss://standby-node",
                            address="standby-node.example.com",
                            port=8388,
                            tier="tier3",
                            status=NodeStatus.ALIVE,
                            method="aes-256-gcm",
                            password="pass",
                        )
                    ]
                    manager.tier3_core_running = True
                    manager.effective_egress_ok = scenario[
                        "effective_egress_ok"
                    ]
                    manager.tier3_traffic_capture_active = scenario[
                        "traffic_capture_active"
                    ]
                    runtime.config = loaded_config
                    runtime.manager = manager
                    runtime.core_runner = FakeCoreRunner(
                        running=True,
                        port_ok=True,
                    )
                    previous_monitor = FakeTrafficMonitor()
                    previous_monitor.start()
                    replacement_monitor = FakeTrafficMonitor()
                    runtime.traffic_monitor = previous_monitor
                    runtime._build_traffic_monitor = (
                        lambda: replacement_monitor
                    )
                    runtime._build_api_client = lambda: None
                    runtime._sync_system_proxy_config = lambda *_args: None
                    geo_reconfigure_calls: list[dict[str, object]] = []
                    runtime.geo_updater.reconfigure = lambda *_args, **kwargs: (
                        geo_reconfigure_calls.append(dict(kwargs))
                    )
                    runtime._reconfigure_manager_after_config_reload = (
                        lambda **_kwargs: None
                    )
                    runtime._sync_subscription_server_config = lambda: None
                    runtime.status = lambda: {}
                    if scenario["stop_requested"]:
                        runtime.stop_event.set()

                    with (
                        patch(
                            "desktop_runtime.bootstrap_user_files",
                            lambda _paths: None,
                        ),
                        patch(
                            "desktop_runtime.load_config",
                            return_value=loaded_config,
                        ),
                        patch(
                            "desktop_runtime.apply_release_path_overrides",
                            side_effect=lambda config, _paths: config,
                        ),
                    ):
                        runtime.reload_config()

                    self.assertTrue(runtime._tier3_is_primary_export())
                    self.assertEqual(previous_monitor.stopped, 1)
                    self.assertEqual(replacement_monitor.started, 0)
                    self.assertEqual(
                        geo_reconfigure_calls[-1]["egress_proxy"],
                        {},
                    )

    def test_invalid_mihomo_client_paths_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False))
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_binary_path"] = ""
            runtime.config["client"]["mihomo_runtime_config"] = None
            runtime.config["client"]["mihomo_work_dir"] = 123
            runtime.network_mode = "tun"

            runner = runtime._get_core_runner()
            expected_root = temp_path.resolve()

            self.assertIs(runner.output_event_sink.__self__, runtime)
            self.assertIs(
                runner.output_event_sink.__func__,
                DesktopRuntime._on_core_output_event,
            )
            self.assertEqual(runner.binary_path, expected_root / "bin" / "mihomo.exe")
            self.assertEqual(runner.runtime_config_path, expected_root / "data" / "mihomo-runtime.yaml")
            self.assertEqual(runner.work_dir, expected_root / "data" / "mihomo")
            self.assertEqual(
                runtime._runtime_config_path_for_client(),
                expected_root / "data" / "mihomo-runtime.yaml",
            )
            self.assertEqual(runtime._missing_geodata_assets(), ["geoip", "geosite"])
            self.assertIsNone(runtime._core_program_path_for_killswitch())

    def test_recover_stale_system_proxy_restores_snapshot(self) -> None:
        from system_proxy import _proxy_transaction

        with tempfile.TemporaryDirectory() as temp_dir, _proxy_transaction():
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
            runtime = self._runtime(temp_path, backend)
            runtime.runtime_state.save_proxy_snapshot(
                original=ProxySettings(enabled=True, server="old:8080", override="old"),
                target="127.0.0.1:7890",
            )

            self.assertTrue(runtime.recover_stale_system_proxy())
            self.assertEqual(backend.settings.server, "old:8080")
            self.assertNotIn("system_proxy", runtime.runtime_state.load_proxy_journal())

    def test_core_process_exit_keeps_system_proxy_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
            runtime = self._runtime(temp_path, backend)
            runtime.system_proxy.original_settings = ProxySettings(enabled=True, server="old:8080", override="old")
            runtime.system_proxy.active = True
            runtime.core_runner = FakeCoreRunner(running=False, port_ok=False)

            runtime._monitor_core_health()

            self.assertEqual(backend.settings.server, "127.0.0.1:7890")
            self.assertTrue(runtime.system_proxy.active)

    def test_string_false_disables_system_proxy_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["system_proxy"]["enabled"] = "false"

            runtime._sync_system_proxy_config()

            self.assertFalse(runtime.system_proxy.enabled)

    def test_write_ui_preferences_does_not_reload_runtime_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.app_paths.config_dir.mkdir(parents=True, exist_ok=True)
            runtime.app_paths.local_config_path.write_text(
                "ui:\n  language: en_US\n  theme: midnight\n",
                encoding="utf-8",
            )
            runtime.config["ui"] = {"language": "en_US", "theme": "midnight"}
            runtime.reload_config = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("UI-only writes must not reload manager/core services")
            )

            runtime.write_ui_preferences(
                {
                    "language": "ja_JP",
                    "theme": "absolutely",
                }
            )

            local_config = yaml.safe_load(runtime.app_paths.local_config_path.read_text(encoding="utf-8"))
            self.assertEqual(local_config["ui"]["language"], "ja_JP")
            self.assertEqual(local_config["ui"]["theme"], "absolutely")
            self.assertNotIn("click_sound", local_config["ui"])
            self.assertEqual(runtime.config["ui"]["language"], "ja_JP")

    def test_reconfigure_manager_after_config_reload_updates_runtime_bindings(self) -> None:
        class FakeTier3Controller:
            def __init__(self) -> None:
                self.config: dict[str, object] = {}

        class FakeManager:
            def __init__(self) -> None:
                self.operation_lock = threading.RLock()
                self.lock = threading.RLock()
                self.tier3_controller = FakeTier3Controller()
                self.loaded = False

            def _build_observation_cache(self):
                return {"rebuilt": True}

            def _tier3_controller_config(self):
                return {"controller": "updated"}

            def _load_initial_state(self) -> None:
                self.loaded = True

        class FakeKillSwitch:
            def __init__(self) -> None:
                self.calls: list[tuple[dict[str, object], list[object], str]] = []

            def reconfigure(self, config, nodes, *, core_program_path) -> None:
                self.calls.append((config, nodes, str(core_program_path)))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config = {
                "client": {"network_mode": "tun", "ipv6": "false"},
                "paths": {
                    "sources_file": "custom-sources.yaml",
                    "tier3_nodes_file": "custom-nodes.json",
                    "tier_observation_cache_file": "custom-observation.json",
                },
                "tiers": {
                    "tier1": {
                        "nodes": [
                            {"name": "one", "address": "example.com", "port": 443},
                            {
                                "name": "draft",
                                "address": "draft.example.com",
                                "port": 443,
                                "enabled": False,
                            },
                        ]
                    },
                    "tier2": {"cache_file": "custom-tier2.json"},
                    "exit": {"cache_file": "custom-exit.json"},
                },
                "testing": {"validation_mode": "strict"},
                "killswitch": {"enabled": False},
            }
            manager = FakeManager()
            kill_switch = FakeKillSwitch()
            warn_calls: list[str] = []
            runtime.manager = manager
            runtime.kill_switch = kill_switch  # type: ignore[assignment]
            runtime._warn_if_killswitch_unavailable = lambda: warn_calls.append("warn")

            runtime._reconfigure_manager_after_config_reload()

            self.assertIs(manager.config, runtime.config)
            self.assertEqual(manager.client_config, {"network_mode": "tun", "ipv6": "false"})
            self.assertEqual(manager.sources_file, "custom-sources.yaml")
            self.assertEqual(manager.tier3_nodes_file, "custom-nodes.json")
            self.assertEqual(manager.tier2_cache_file, "custom-tier2.json")
            self.assertEqual(manager.exit_cache_file, "custom-exit.json")
            self.assertEqual(manager.tier_observation_cache_file, "custom-observation.json")
            self.assertEqual(manager.observation_cache, {"rebuilt": True})
            self.assertEqual(manager.testing_config, {"validation_mode": "strict"})
            self.assertEqual(manager.validation_mode, "strict")
            self.assertEqual(manager.tier3_controller.config, {"controller": "updated"})
            self.assertTrue(manager.loaded)
            self.assertEqual(len(kill_switch.calls), 1)
            self.assertEqual(kill_switch.calls[0][0], {"enabled": False})
            self.assertEqual(kill_switch.calls[0][1], [{"name": "one", "address": "example.com", "port": 443}])
            self.assertEqual(warn_calls, ["warn"])

            runtime.config["testing"] = "invalid"
            manager.loaded = False
            runtime._reconfigure_manager_after_config_reload()

            self.assertEqual(manager.testing_config, {})
            self.assertEqual(manager.validation_mode, "optional")
            self.assertTrue(manager.loaded)

            runtime.config["tiers"] = "invalid"
            manager.loaded = False
            runtime._reconfigure_manager_after_config_reload()

            self.assertEqual(kill_switch.calls[-1][1], [])
            self.assertEqual(manager.tier2_cache_file, "data/tier2_cache.json")
            self.assertEqual(manager.exit_cache_file, "data/exit_cache.json")
            self.assertTrue(manager.loaded)

            runtime.config["paths"] = "invalid"
            manager.loaded = False
            runtime._reconfigure_manager_after_config_reload()

            self.assertEqual(manager.paths, {})
            self.assertEqual(manager.sources_file, "sources.yaml")
            self.assertEqual(manager.tier3_nodes_file, "data/nodes.json")
            self.assertTrue(manager.loaded)

            runtime.config["killswitch"] = "invalid"
            manager.loaded = False
            runtime._reconfigure_manager_after_config_reload()

            self.assertEqual(kill_switch.calls[-1][0], {})
            self.assertTrue(manager.loaded)

    def test_repeated_port_failure_keeps_system_proxy_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
            runtime = self._runtime(temp_path, backend)
            runtime.system_proxy.original_settings = ProxySettings(enabled=False, server="", override="")
            runtime.system_proxy.active = True
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=False)

            runtime._monitor_core_health()
            self.assertTrue(runtime.system_proxy.active)
            runtime._monitor_core_health()

            self.assertTrue(backend.settings.enabled)
            self.assertEqual(backend.settings.server, "127.0.0.1:7890")
            self.assertTrue(runtime.system_proxy.active)

    def test_active_system_proxy_health_check_does_not_probe_registry_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
            runtime = self._runtime(temp_path, backend)
            runtime.system_proxy.active = True
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            target_checks = []
            runtime.system_proxy.is_target_active = lambda: target_checks.append("checked") or True

            runtime._monitor_core_health()

            self.assertEqual(target_checks, [])

    def test_process_and_port_health_do_not_promote_pending_effective_egress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.effective_egress_ok = None
            runtime.manager = manager
            runtime.running = True
            runtime.system_proxy.active = True
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)

            runtime._sync_effective_egress_state_from_core()
            runtime._monitor_core_health()

            self.assertFalse(
                any(ok is True for ok, _reason in manager.effective_states)
            )

    def test_background_remote_refresh_route_requires_verified_formal_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()

            manager.effective_egress_ok = False
            self.assertIsNone(runtime._remote_refresh_egress_proxy_config())

            manager.effective_egress_ok = True
            self.assertIsNone(runtime._remote_refresh_egress_proxy_config())

            runtime._set_core_runtime_phase("formal_capture")
            self.assertEqual(
                runtime._remote_refresh_egress_proxy_config(),
                {
                    "enabled": True,
                    "scheme": "http",
                    "server": "127.0.0.1",
                    "port": 7890,
                    "use_for": {"collection": True},
                },
            )

            runtime.config["egress_proxy"] = {
                "enabled": True,
                "scheme": "http",
                "server": "explicit.example.com",
                "port": 8080,
                "use_for": {"collection": True},
            }
            manager.effective_egress_ok = False
            self.assertIsNone(runtime._remote_refresh_egress_proxy_config())
            manager.effective_egress_ok = None
            self.assertIsNone(runtime._remote_refresh_egress_proxy_config())

            manager.effective_egress_ok = True
            self.assertEqual(
                runtime._remote_refresh_egress_proxy_config()["server"],
                "127.0.0.1",
            )
            runtime._set_core_runtime_phase("formal_no_capture")
            self.assertIsNone(runtime._remote_refresh_egress_proxy_config())

    def test_effective_egress_state_uses_shared_capture_decision(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.states: list[tuple[bool | None, str]] = []

            def set_effective_egress_state(self, ok: bool | None, reason: str = "") -> None:
                self.states.append((ok, reason))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            recorder = Recorder()
            runtime.manager = recorder
            runtime.running = True
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.active = False
            runtime.system_proxy.enabled = True
            runtime.system_proxy.is_target_active = lambda: False

            runtime._sync_effective_egress_state_from_core()

            self.assertEqual(recorder.states[-1], (False, "system proxy capture is inactive"))

            runtime.system_proxy.is_target_active = lambda: True
            runtime.core_runner = FakeCoreRunner(running=False, port_ok=False)

            runtime._sync_effective_egress_state_from_core()

            self.assertEqual(recorder.states[-1], (False, "mihomo core is not running"))

    def test_monitor_core_health_reuses_effective_state_when_core_missing(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.states: list[tuple[bool | None, str]] = []

            def set_effective_egress_state(self, ok: bool | None, reason: str = "") -> None:
                self.states.append((ok, reason))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            recorder = Recorder()
            runtime.manager = recorder
            runtime.core_runner = None
            runtime.health_failure_count = 3

            runtime.running = False
            runtime._monitor_core_health()

            self.assertEqual(recorder.states[-1], (None, ""))
            self.assertEqual(runtime.health_failure_count, 0)

            runtime.running = True
            runtime._monitor_core_health()

            self.assertEqual(recorder.states[-1], (False, "mihomo core is not running"))

    def test_core_restart_failure_recording_preserves_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)

            runtime._record_core_restart_failure(RuntimeError("restart failed"), "restart failed")

            self.assertEqual(runtime.last_error, "restart failed")

    def test_recoverable_core_start_error_recording_can_defer_last_error_update(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.states: list[tuple[bool | None, str]] = []

            def set_effective_egress_state(self, ok: bool | None, reason: str = "") -> None:
                self.states.append((ok, reason))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            recorder = Recorder()
            runtime.manager = recorder
            runtime.last_error = "previous"

            error = runtime._record_recoverable_core_start_error(
                RecoverableCoreStartError("tier3 recovering"),
                update_last_error=False,
            )

            self.assertEqual(error, "tier3 recovering")
            self.assertEqual(runtime.last_error, "previous")
            self.assertEqual(recorder.states[-1], (False, "tier3 recovering"))

            runtime._record_recoverable_core_start_error(
                RecoverableCoreStartError("still recovering"),
                update_last_error=True,
            )

            self.assertEqual(runtime.last_error, "still recovering")
            self.assertEqual(recorder.states[-1], (False, "still recovering"))

    def test_health_restart_helper_never_stops_core_while_runtime_is_stopping(self) -> None:
        # Core-restart race fix (10-26): while the runtime stops, the stop
        # path owns the Core; a health restart neither stops nor starts it.
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.running = True
            runtime.stop_event.set()
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.running = True

            def fail_start_core():
                raise AssertionError("restart should be gated")

            runtime.start_core = fail_start_core

            outcome = runtime._attempt_core_restart_after_health_failure(
                failure_message="Failed to restart mihomo after proxy port health failure",
                stop_core_first=True,
            )

            self.assertEqual(outcome, "stopping")
            self.assertFalse(runtime.core_runner.stopped)
            self.assertFalse(runtime.core_runner.started)
            self.assertIsNone(runtime.last_error)

    def test_wait_for_core_proxy_helper_preserves_health_error_stop_and_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.status_payload = {"last_health_error": "port blocked"}
            wait_calls = []

            def fail_wait_for_proxy(host="127.0.0.1", port=7890, timeout=30):
                wait_calls.append((host, port, timeout))
                return False

            runner.wait_for_proxy = fail_wait_for_proxy
            cause = RuntimeError("Bastion warmup failed")

            with self.assertRaisesRegex(TimeoutError, "port blocked") as captured:
                runtime._wait_for_core_proxy_or_stop(
                    runner,
                    host="127.0.0.1",
                    port=7890,
                    timeout=2,
                    cause=cause,
                )

            self.assertEqual(wait_calls, [("127.0.0.1", 7890, 2)])
            self.assertTrue(runner.stopped)
            self.assertIs(captured.exception.__cause__, cause)

    def test_restart_core_after_runtime_config_update_preserves_order_and_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runtime_config = temp_path / "mihomo-runtime.yaml"
            calls = []
            cause = RuntimeError("Bastion warmup failed")
            cause_ref = cause

            def stop_runner():
                calls.append("stop")

            def write_runtime_config():
                calls.append("write")
                return runtime_config

            def start_runner():
                calls.append("start")

            def wait_for_proxy(runner_arg, *, host, port, timeout, cause=None):
                calls.append(("wait", runner_arg is runner, host, port, timeout, cause is cause_ref))

            runner.stop = stop_runner
            runner.start = start_runner
            runtime.write_mihomo_runtime_config = write_runtime_config
            runtime._wait_for_core_proxy_or_stop = wait_for_proxy

            runtime._restart_core_after_runtime_config_update(
                runner,
                host="127.0.0.1",
                port=7890,
                timeout=2,
                log_message="restart after repair",
                cause=cause,
            )

            self.assertEqual(
                calls,
                [
                    "stop",
                    "write",
                    "start",
                    ("wait", True, "127.0.0.1", 7890, 2, True),
                ],
            )

    def test_tier3_startup_gate_reports_empty_validated_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            runtime.manager = manager
            runtime._tier3_required_for_start = True
            runtime._last_tier3_startup_error = "collection failed"

            with self.assertRaisesRegex(
                RecoverableCoreStartError,
                "no validated proxy nodes.*collection failed",
            ):
                runtime._require_tier3_ready_for_core_start()

    def test_tier3_startup_gate_reports_underfilled_verified_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 2
            manager.refresh_tier3()
            runtime.manager = manager
            runtime._tier3_required_for_start = True

            with self.assertRaisesRegex(RecoverableCoreStartError, "only 1/2 validated proxy nodes"):
                runtime._require_tier3_ready_for_core_start()

    def test_system_proxy_capture_after_core_start_skips_tun_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.network_mode = "tun"
            runtime.system_proxy.enabled = True
            runner = FakeLifecycleCoreRunner()

            def fail_enable_system_proxy():
                raise AssertionError("TUN mode must not enable the system proxy")

            runtime.enable_system_proxy = fail_enable_system_proxy

            runtime._apply_system_proxy_capture_after_core_start(runner)

            self.assertFalse(runner.stopped)
            self.assertFalse(runtime.system_proxy.active)

    def test_system_proxy_capture_failure_keeps_standby_core_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = True
            runner = FakeLifecycleCoreRunner()

            def fail_enable_system_proxy():
                raise RuntimeError("registry unavailable")

            runtime.enable_system_proxy = fail_enable_system_proxy

            with self.assertRaisesRegex(RuntimeError, "registry unavailable"):
                runtime._apply_system_proxy_capture_after_core_start(runner)

            self.assertFalse(runner.stopped)

    def test_system_proxy_capture_rejects_unretained_windows_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = True
            runner = FakeLifecycleCoreRunner()
            restored: list[str] = []
            original_restore = runtime.restore_system_proxy
            runtime.system_proxy.is_target_active = lambda: False

            def restore() -> dict[str, object]:
                restored.append("restore")
                return original_restore()

            runtime.restore_system_proxy = restore

            with self.assertRaisesRegex(
                RuntimeError,
                "did not retain the Tesseract target",
            ):
                runtime._apply_system_proxy_capture_after_core_start(runner)

            self.assertEqual(restored, ["restore"])
            self.assertFalse(backend.settings.enabled)
            self.assertFalse(runner.stopped)

    def test_system_proxy_host_proof_uses_exact_readback_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = True
            runtime.system_proxy.enable()
            runtime._default_probe_urls = lambda: [
                "https://probe.example/generate_204"
            ]
            observed: list[tuple[str, int, str]] = []

            runtime._run_brokered_runtime_probe = (
                lambda callback, **_kwargs: callback(None)
            )

            def probe_via_target(
                *,
                proxy_host: str,
                proxy_port: int,
                probe_url: str,
                **_kwargs: object,
            ) -> tuple[int, bytes]:
                observed.append((proxy_host, proxy_port, probe_url))
                return 204, b""

            runtime._abortable_https_204_via_http_proxy = probe_via_target

            proof = runtime._verify_system_proxy_host_capture(
                deadline_monotonic=time.monotonic() + 1.0,
            )

            self.assertTrue(proof["verified"])
            self.assertEqual(
                observed,
                [
                    (
                        "127.0.0.1",
                        7890,
                        "https://probe.example/generate_204",
                    )
                ],
            )

    def test_system_proxy_host_proof_uses_wininet_preconfig_in_production(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = True
            runtime.system_proxy.enable()
            runtime._default_probe_urls = lambda: [
                "https://probe.example/generate_204"
            ]
            runtime._run_brokered_runtime_probe = (
                lambda callback, **_kwargs: callback(None)
            )
            runtime._system_proxy_host_probe_uses_wininet_preconfig = lambda: True
            observed: list[tuple[str, float]] = []

            def wininet_probe(
                *,
                probe_url: str,
                timeout_seconds: float,
                **_kwargs: object,
            ) -> tuple[int, bytes]:
                observed.append((probe_url, timeout_seconds))
                return 204, b""

            runtime._abortable_https_204_via_wininet_preconfig = wininet_probe
            runtime._abortable_https_204_via_http_proxy = (
                lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("explicit proxy adapter must not prove host capture")
                )
            )

            proof = runtime._verify_system_proxy_host_capture(
                deadline_monotonic=time.monotonic() + 4.0,
            )

            self.assertTrue(proof["verified"])
            self.assertEqual(proof["transport"], "wininet_preconfig")
            self.assertEqual(observed[0][0], "https://probe.example/generate_204")
            self.assertGreater(observed[0][1], 1.5)
            self.assertLessEqual(observed[0][1], 3.0)

    def test_abortable_https_proxy_preserves_tunnel_after_connect_headers(self) -> None:
        sent: list[bytes] = []

        class FakeSocket:
            def settimeout(self, _timeout: float) -> None:
                pass

            def do_handshake(self) -> None:
                pass

            def sendall(self, payload: bytes) -> None:
                sent.append(payload)

            def close(self) -> None:
                pass

        class FakeHeaderReader:
            def __init__(self) -> None:
                self._lines = iter((b"Proxy-Agent: fixture\r\n", b"\r\n"))

            def readline(self, _limit: int) -> bytes:
                return next(self._lines)

        class FakeConnectResponse:
            def __init__(self) -> None:
                self.fp = FakeHeaderReader()

            def _read_status(self) -> tuple[int, int, str]:
                return 11, 200, "Connection established"

            def begin(self) -> None:
                raise AssertionError(
                    "CONNECT must not apply ordinary response-body framing"
                )

            def close(self) -> None:
                pass

        class FakeFinalResponse:
            status = 204

            def begin(self) -> None:
                pass

            def read(self, _limit: int) -> bytes:
                return b""

            def close(self) -> None:
                pass

        raw_socket = FakeSocket()
        tls_socket = FakeSocket()
        tls_context = SimpleNamespace(
            wrap_socket=lambda _raw, *, server_hostname, do_handshake_on_connect: (
                tls_socket
                if server_hostname == "probe.example" and do_handshake_on_connect is False
                else (_ for _ in ()).throw(AssertionError(server_hostname))
            )
        )
        abort_owner = SimpleNamespace(bind=lambda _resource: None, clear=lambda _resource: None)

        with (
            patch(
                "desktop_runtime.socket.create_connection",
                return_value=raw_socket,
            ),
            patch(
                "desktop_runtime.ssl.create_default_context",
                return_value=tls_context,
            ),
            patch(
                "desktop_runtime.http.client.HTTPResponse",
                side_effect=(FakeConnectResponse(), FakeFinalResponse()),
            ),
        ):
            status, body = DesktopRuntime._abortable_https_204_via_http_proxy(
                proxy_host="127.0.0.1",
                proxy_port=7890,
                probe_url="https://probe.example/generate_204",
                timeout_seconds=1.0,
                abort_owner=abort_owner,
            )

        self.assertEqual((status, body), (204, b""))
        self.assertTrue(sent[0].startswith(b"CONNECT probe.example:443 HTTP/1.1"))
        self.assertTrue(sent[1].startswith(b"GET /generate_204 HTTP/1.1"))

    def test_abortable_probe_reserves_cleanup_before_broker_deadline(self) -> None:
        runtime = object.__new__(DesktopRuntime)
        runtime._client_config = lambda: {"mihomo_mixed_port": 7890}
        runtime._system_proxy_config = lambda: {"host": "127.0.0.1"}
        runtime._client_mixed_port = lambda _config: 7890
        runtime._default_probe_urls = lambda: [
            "https://probe.example/generate_204",
            "https://backup.example/generate_204",
        ]
        runtime._safe_probe_endpoint_label = lambda value: value
        runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
        runtime._prepare_abortable_probe = (
            lambda *_args, **_kwargs: SimpleNamespace()
        )
        observed_timeouts: list[float] = []

        def probe(**kwargs: object) -> tuple[int, bytes]:
            observed_timeouts.append(float(kwargs["timeout_seconds"]))
            return 204, b""

        runtime._abortable_https_204_via_http_proxy = probe
        context = SimpleNamespace(
            execution_deadline_monotonic=time.monotonic() + 1.0,
        )

        result = runtime._probe_mihomo_mixed_port_once_leased(
            timeout_seconds=1.0,
            execution_context=context,
        )

        self.assertEqual(result["status"], 204)
        self.assertEqual(len(observed_timeouts), 1)
        self.assertLess(observed_timeouts[0], 0.90)
        self.assertGreater(observed_timeouts[0], 0.70)

    def test_core_runner_uses_absolute_release_paths_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            binary_path = temp_path / "resources" / "bin" / "mihomo.exe"
            runtime_config = temp_path / "data" / "mihomo-runtime.yaml"
            work_dir = temp_path / "data" / "mihomo"
            config = {
                "client": {
                    "mihomo_binary_path": str(binary_path),
                    "mihomo_runtime_config": str(runtime_config),
                    "mihomo_work_dir": str(work_dir),
                    "runtime_state_path": str(temp_path / "data" / "runtime-state.json"),
                    "system_proxy": {"enabled": True, "host": "127.0.0.1", "port": 7890},
                },
                "alerts": {"log_file": str(temp_path / "data" / "monitor.log"), "desktop": {"enabled": False}},
            }
            runtime = DesktopRuntime(config, project_root=temp_path)
            for handler in list(runtime.logger.handlers):
                handler.close()
                runtime.logger.removeHandler(handler)
            runtime.system_proxy = SystemProxyManager(
                config["client"]["system_proxy"],
                backend=backend,
                platform_name="windows",
            )

            runner = runtime._get_core_runner()

            self.assertEqual(runner.binary_path, binary_path)
            self.assertEqual(runner.runtime_config_path, runtime_config)
            self.assertEqual(runner.work_dir, work_dir)

    def test_killswitch_core_path_is_none_when_tun_binary_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["network_mode"] = "tun"
            runtime.config["client"]["mihomo_binary_path"] = str(temp_path / "missing-mihomo.exe")
            runtime.network_mode = "tun"

            self.assertIsNone(runtime._core_program_path_for_killswitch())

    def test_tun_core_exit_attempts_restart_after_health_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.running = True
            runtime._desired_running = True
            runtime.network_mode = "tun"
            runtime.manager = FakeTier3Manager()
            runtime.system_proxy.active = False
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            traffic = FakeTrafficMonitor()
            runtime.traffic_monitor = traffic
            runtime._verify_runtime_final_exit = lambda **_kwargs: {
                "url": "https://probe.example",
                "status": 204,
                "latency_ms": 25,
            }
            runtime._probe_runtime_leaf = (
                lambda *_args, **_kwargs: {"delay": 25}
            )

            runtime._monitor_core_health()
            runtime._wait_for_tier3_egress_activation()
            self.assertFalse(runtime.core_runner.started)
            runtime._monitor_core_health()
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(runtime.core_runner.started)
            self.assertEqual(traffic.started, 1)

    def test_system_proxy_core_exit_attempts_restart_without_restoring_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.running = True
            runtime._desired_running = True
            runtime.manager = FakeTier3Manager()
            runtime.system_proxy.active = True
            runtime.system_proxy.original_settings = ProxySettings(enabled=False, server="", override="")
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            traffic = FakeTrafficMonitor()
            runtime.traffic_monitor = traffic
            runtime._probe_mihomo_mixed_port = lambda: {
                "url": "https://probe.example",
                "latency_ms": 25,
            }
            runtime._probe_runtime_leaf = (
                lambda *_args, **_kwargs: {"delay": 25}
            )
            runtime._verify_system_proxy_host_capture = (
                lambda **_kwargs: {"required": True, "verified": True}
            )

            runtime._monitor_core_health()
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(runtime.core_runner.started)
            self.assertEqual(traffic.started, 1)
            self.assertTrue(runtime.system_proxy.active)
            self.assertTrue(backend.settings.enabled)
            self.assertEqual(backend.settings.server, "127.0.0.1:7890")

    def test_start_core_no_capture_reaches_ready_without_traffic_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.manager = object()
            runtime.running = True
            runtime._desired_running = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            traffic = FakeTrafficMonitor()
            runtime.traffic_monitor = traffic
            identity_requests: list[str] = []
            runtime._verify_final_exit_identity = lambda **kwargs: (
                identity_requests.append(str(kwargs.get("request_id") or ""))
                or {"state": "complete"}
            )

            status = runtime.start_core()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            self.assertTrue(status["running"])
            self.assertEqual(traffic.started, 0)
            self.assertEqual(traffic.stopped, 0)
            self.assertEqual(identity_requests, [])
            self.assertEqual(runtime.core_runner.hot_reloaded, 0)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.READY,
            )

    def test_start_core_identity_failure_does_not_block_204_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.manager = object()
            runtime.running = True
            runtime._desired_running = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = (
                lambda: temp_path / "mihomo-runtime.yaml"
            )
            runtime.core_runner = FakeLifecycleCoreRunner()
            traffic = FakeTrafficMonitor()
            runtime.traffic_monitor = traffic

            def fail_identity(**_kwargs):
                raise RuntimeError("identity TLS unexpected EOF")

            runtime._verify_final_exit_identity = fail_identity

            status = runtime.start_core()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            self.assertTrue(status["running"])
            self.assertTrue(runtime.core_runner.is_running())
            self.assertEqual(traffic.started, 0)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.READY,
            )

    def test_bootstrap_provider_waits_for_missing_http_rule_provider_with_healthy_node(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            node = manager.runtime_nodes["tier3"][0]
            base = build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [node], "exit": []},
                routing_config={
                    "rule_providers": {
                        "remote": {
                            "type": "http",
                            "behavior": "domain",
                            "url": "https://rules.example/remote.yaml",
                            "path": "rules/missing.yaml",
                        }
                    }
                },
            )
            runtime_config = temp_path / "bootstrap.yaml"
            runtime_config.write_text(
                build_bootstrap_clash_config(base),
                encoding="utf-8",
            )
            runtime.write_mihomo_bootstrap_runtime_config = lambda: runtime_config
            runtime._runtime_config_uses_geodata = lambda _path: False

            provider_path = temp_path / "data" / "mihomo" / "rules" / "missing.yaml"

            class InitializingRuleProviderController:
                def __init__(self) -> None:
                    self.proxy_calls = 0
                    self.provider_calls = 0

                def proxies(self):
                    self.proxy_calls += 1
                    if self.proxy_calls >= 2:
                        provider_path.parent.mkdir(parents=True, exist_ok=True)
                        provider_path.write_text("payload: []\n", encoding="utf-8")
                    loaded_config = yaml.safe_load(
                        runtime_config.read_text(encoding="utf-8")
                    )
                    entries = {
                        str(proxy["name"]): {
                            "type": str(proxy.get("type") or "Proxy")
                        }
                        for proxy in loaded_config.get("proxies", [])
                    }
                    for group in loaded_config.get("proxy-groups", []):
                        candidates = [str(item) for item in group["proxies"]]
                        entries[str(group["name"])] = {
                            "type": str(group.get("type") or "Selector"),
                            "now": candidates[0],
                            "all": candidates,
                        }
                    return {"proxies": entries}

                def providers_rules(self):
                    self.provider_calls += 1
                    updated_at = (
                        "0001-01-01T00:00:00Z"
                        if self.provider_calls == 1
                        else "2026-08-10T10:00:00Z"
                    )
                    return {
                        "providers": {
                            "remote": {
                                "ruleCount": 0,
                                "updatedAt": updated_at,
                            }
                        }
                    }

            controller = InitializingRuleProviderController()
            runtime.api_client = controller
            runtime._wait_for_mihomo_route_ready = (
                DesktopRuntime._wait_for_mihomo_route_ready.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )

            proxy_url = runtime._runtime_source_fetch_proxy_provider("bootstrap")

            self.assertEqual(proxy_url, "http://127.0.0.1:7890")
            self.assertTrue(runtime.core_runner.started)
            self.assertGreaterEqual(controller.proxy_calls, 3)
            self.assertEqual(controller.provider_calls, 2)
            self.assertEqual(runtime._current_core_runtime_phase(), "bootstrap")
            self.assertEqual(
                manager.tier3_last_collection_report[
                    "runtime_source_bootstrap_status"
                ],
                "ready",
            )

    def test_http_rule_provider_without_path_uses_initialized_controller_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                ),
            )
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text(
                yaml.safe_dump(
                    {
                        "rule-providers": {
                            "remote": {
                                "type": "http",
                                "url": "https://rules.example/provider.yaml",
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            class InitializedProviderController:
                def providers_rules(self):
                    return {
                        "providers": {
                            "remote": {
                                "ruleCount": 0,
                                "updatedAt": "2026-08-10T10:00:00Z",
                            }
                        }
                    }

            runtime.api_client = InitializedProviderController()

            state = runtime._mihomo_rule_providers_ready(runtime_config)

            self.assertTrue(state["ready"])
            self.assertEqual(state["providers"], ["remote"])

    def test_manager_runtime_hooks_bind_and_detach_source_proxy_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()

            runtime._attach_manager_runtime_hooks(manager)

            # Source-supply threads get the non-blocking provider; preview and
            # discovery keep the blocking one (see the tests below).
            self.assertEqual(
                manager.runtime_source_fetch_proxy_provider,
                runtime._runtime_source_supply_proxy_provider,
            )
            self.assertEqual(
                manager.runtime_source_fetch_endpoint_provider,
                runtime._runtime_source_fetch_endpoints_provider,
            )

            runtime._detach_manager_runtime_hooks(manager)

            self.assertIsNone(manager.runtime_source_fetch_proxy_provider)
            self.assertIsNone(manager.runtime_source_fetch_endpoint_provider)

    def _supply_provider_runtime(self, temp_dir: str) -> tuple[DesktopRuntime, list[str]]:
        runtime = self._runtime(
            Path(temp_dir),
            MemoryProxyBackend(ProxySettings(enabled=False, server="", override="")),
        )
        calls: list[str] = []

        def ensure_bootstrap() -> str:
            calls.append(threading.current_thread().name)
            return "http://127.0.0.1:18080"

        runtime._ensure_bootstrap_core_proxy = ensure_bootstrap  # type: ignore[method-assign]
        return runtime, calls

    def test_source_supply_never_waits_for_a_core_transition_it_does_not_own(self) -> None:
        """start_core holds the transition lock across Tier1/Tier2 checks.

        A supply thread that queued there delayed the first source transport by
        5.9-8.2 s in recorded sessions; it must get the neutral None at once and
        must not start, reload or gate the Core while another owner is inside.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, calls = self._supply_provider_runtime(temp_dir)
            held = threading.Event()
            release = threading.Event()

            def owner() -> None:
                with runtime._core_transition_lock:
                    held.set()
                    release.wait(5)

            thread = threading.Thread(target=owner, name="core-transition-owner")
            thread.start()
            try:
                self.assertTrue(held.wait(2))
                started = time.monotonic()
                self.assertIsNone(runtime._runtime_source_supply_proxy_provider("bootstrap"))
                self.assertLess(time.monotonic() - started, 0.1)
                self.assertEqual(calls, [])
            finally:
                release.set()
                thread.join(5)

            # Once the owner is done the next poll does the side effects again.
            self.assertEqual(
                runtime._runtime_source_supply_proxy_provider("bootstrap"),
                "http://127.0.0.1:18080",
            )
            self.assertEqual(len(calls), 1)

    def test_source_supply_reenters_a_transition_its_own_thread_owns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, calls = self._supply_provider_runtime(temp_dir)
            with runtime._core_transition_lock:
                self.assertEqual(
                    runtime._runtime_source_supply_proxy_provider("bootstrap"),
                    "http://127.0.0.1:18080",
                )
            self.assertEqual(len(calls), 1)

    def test_source_supply_non_bootstrap_phases_do_not_touch_the_transition_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _calls = self._supply_provider_runtime(temp_dir)
            phases: list[str] = []
            runtime._runtime_source_fetch_proxy_provider = (  # type: ignore[method-assign]
                lambda phase: phases.append(phase) or None
            )
            held = threading.Event()
            release = threading.Event()

            def owner() -> None:
                with runtime._core_transition_lock:
                    held.set()
                    release.wait(5)

            thread = threading.Thread(target=owner, name="core-transition-owner")
            thread.start()
            try:
                self.assertTrue(held.wait(2))
                started = time.monotonic()
                self.assertIsNone(runtime._runtime_source_supply_proxy_provider("core"))
                self.assertLess(time.monotonic() - started, 0.1)
            finally:
                release.set()
                thread.join(5)
            self.assertEqual(phases, ["core"])

    def test_preview_collection_settings_keep_the_blocking_bootstrap_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _calls = self._supply_provider_runtime(temp_dir)
            runtime.manager = SimpleNamespace(_collection_settings=lambda: {})
            requested: list[str] = []
            runtime._runtime_source_fetch_proxy_provider = (  # type: ignore[method-assign]
                lambda phase: requested.append(phase) or "http://127.0.0.1:18081"
            )

            def supply_must_not_be_used(_phase: str) -> str:  # pragma: no cover
                raise AssertionError("preview must not use the non-blocking supply provider")

            runtime._runtime_source_supply_proxy_provider = supply_must_not_be_used  # type: ignore[method-assign]

            settings = runtime._manager_collection_settings()

            self.assertEqual(requested, ["bootstrap"])
            self.assertEqual(settings["runtime_source_fetch_proxy_url"], "http://127.0.0.1:18081")
            self.assertTrue(settings["require_remote_source_route"])

    def test_runtime_source_transport_is_stable_within_runtime_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime._runtime_generation = 7
            runtime.config["egress_proxy"] = {
                "enabled": True,
                "scheme": "http",
                "server": "127.0.0.1",
                "port": 18080,
                "use_for": {"collection": True},
            }
            candidates = iter(
                [
                    (19101, 19102, 19103),
                    (19201, 19202, 19203),
                ]
            )
            runtime._runtime_source_candidate_ports = lambda _count: next(candidates)

            first = runtime._runtime_source_transport_for_config()
            second = runtime._runtime_source_transport_for_config()
            reallocated = runtime._runtime_source_transport_for_config(
                force_reallocate=True
            )

            self.assertEqual(first.runtime_generation, 7)
            self.assertEqual(first.config_generation, 1)
            self.assertEqual(second.config_generation, 2)
            self.assertEqual(reallocated.config_generation, 3)
            self.assertEqual(
                [listener.port for listener in first.listeners],
                [listener.port for listener in second.listeners],
            )
            self.assertNotEqual(
                [listener.port for listener in second.listeners],
                [listener.port for listener in reallocated.listeners],
            )
            self.assertEqual(
                [listener.credentials for listener in first.listeners],
                [listener.credentials for listener in reallocated.listeners],
            )

    def test_initial_core_load_reallocates_private_listener_ports_after_bind_failure(self) -> None:
        class CountingRunner(FakeLifecycleCoreRunner):
            def __init__(self) -> None:
                super().__init__()
                self.start_count = 0
                self.stop_count = 0

            def start(self):
                self.start_count += 1
                return super().start()

            def stop(self):
                self.stop_count += 1
                return super().stop()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runner = CountingRunner()
            runtime.core_runner = runner
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("proxies: []\n", encoding="utf-8")
            runtime.write_mihomo_no_capture_runtime_config = lambda: runtime_config
            runtime._ensure_geodata_ready_for_core_start = lambda _path: None
            runtime._wait_for_core_proxy_or_stop = lambda *_args, **_kwargs: None
            readiness_calls: list[str] = []

            def wait_for_listeners(_runner, *, timeout):
                readiness_calls.append("ready")
                if len(readiness_calls) == 1:
                    raise RuntimeSourceListenerReadinessError(
                        "synthetic private listener bind conflict"
                    )

            runtime._wait_for_runtime_source_listeners_or_stop = wait_for_listeners
            reallocations: list[bool] = []
            runtime._runtime_source_transport_for_config = (
                lambda *, force_reallocate=False: reallocations.append(
                    bool(force_reallocate)
                )
            )
            runtime._tier3_is_primary_export = lambda: True
            runtime._mark_tier3_core_process_ready = lambda **_kwargs: None
            runtime._queue_tier3_egress_activation = lambda: None

            result = runtime._start_core_once()

            self.assertTrue(result["running"])
            self.assertEqual(runner.start_count, 2)
            self.assertEqual(runner.stop_count, 1)
            self.assertEqual(readiness_calls, ["ready", "ready"])
            self.assertEqual(reallocations, [True])

    def test_initial_core_load_stops_after_three_private_listener_conflicts(self) -> None:
        class CountingRunner(FakeLifecycleCoreRunner):
            def __init__(self) -> None:
                super().__init__()
                self.start_count = 0
                self.stop_count = 0

            def start(self):
                self.start_count += 1
                return super().start()

            def stop(self):
                self.stop_count += 1
                return super().stop()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runner = CountingRunner()
            runtime.core_runner = runner
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("proxies: []\n", encoding="utf-8")
            runtime.write_mihomo_no_capture_runtime_config = lambda: runtime_config
            runtime._ensure_geodata_ready_for_core_start = lambda _path: None
            runtime._wait_for_core_proxy_or_stop = lambda *_args, **_kwargs: None
            runtime._wait_for_runtime_source_listeners_or_stop = (
                lambda _runner, *, timeout: (_ for _ in ()).throw(
                    RuntimeSourceListenerReadinessError(
                        "synthetic private listener bind conflict"
                    )
                )
            )
            reallocations: list[bool] = []
            runtime._runtime_source_transport_for_config = (
                lambda *, force_reallocate=False: reallocations.append(
                    bool(force_reallocate)
                )
            )

            with self.assertRaises(RuntimeSourceListenerReadinessError):
                runtime._start_core_once()

            self.assertEqual(runner.start_count, 3)
            self.assertEqual(runner.stop_count, 3)
            self.assertEqual(reallocations, [True, True])

    def test_runtime_source_endpoint_provider_gates_mutation_and_final_exit_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime._runtime_generation = 9
            runtime.config["egress_proxy"] = {
                "enabled": True,
                "scheme": "http",
                "server": "127.0.0.1",
                "port": 18080,
                "use_for": {"collection": True},
            }
            runtime._runtime_source_candidate_ports = lambda _count: (
                19301,
                19302,
                19303,
            )
            runtime._runtime_source_transport_for_config()
            runtime._mark_runtime_source_transport_loaded()
            runtime._runtime_source_listener_requires_auth = lambda _endpoint: True
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            manager = FakeTier3Manager()
            manager.effective_egress_ok = True
            runtime.manager = manager
            runtime._set_core_runtime_phase("formal_capture")

            ready = runtime._runtime_source_fetch_endpoints_provider("core")

            self.assertEqual(
                [endpoint.route_kind for endpoint in ready],
                [
                    RuntimeSourceRouteKind.FINAL_EXIT,
                    RuntimeSourceRouteKind.EGRESS,
                    RuntimeSourceRouteKind.DIRECT,
                ],
            )
            self.assertFalse(ready[0].ready)
            self.assertTrue(all(endpoint.ready for endpoint in ready[1:]))

            manager.effective_egress_ok = False
            degraded = runtime._runtime_source_fetch_endpoints_provider(
                "core",
                RuntimeSourceRouteKind.DIRECT,
            )
            self.assertEqual(
                degraded[0].route_kind,
                RuntimeSourceRouteKind.DIRECT,
            )
            self.assertFalse(
                next(
                    endpoint
                    for endpoint in degraded
                    if endpoint.route_kind is RuntimeSourceRouteKind.FINAL_EXIT
                ).ready
            )

            class PoolSnapshot:
                healthy = 1

            manager.tier3_auto_pool_count_snapshot = lambda: PoolSnapshot()
            manager.tier3_startup_gate_satisfied = False
            manager.tier3_traffic_capture_active = False
            runtime._last_core_export_sync_marker = (("tier3", ()),)
            runtime._set_core_runtime_phase("bootstrap")
            stale_export = runtime._runtime_source_fetch_endpoints_provider(
                "bootstrap"
            )
            self.assertFalse(
                next(
                    endpoint
                    for endpoint in stale_export
                    if endpoint.route_kind is RuntimeSourceRouteKind.FINAL_EXIT
                ).ready
            )

            runtime._last_core_export_sync_marker = (
                ("tier3", (("loaded",),)),
            )
            source_only = runtime._runtime_source_fetch_endpoints_provider(
                "bootstrap"
            )
            self.assertFalse(
                next(
                    endpoint
                    for endpoint in source_only
                    if endpoint.route_kind is RuntimeSourceRouteKind.FINAL_EXIT
                ).ready
            )
            self.assertFalse(manager.effective_egress_ok)
            self.assertFalse(manager.tier3_startup_gate_satisfied)
            self.assertFalse(manager.tier3_traffic_capture_active)

            runtime._set_core_runtime_phase("formal_capture")
            runtime._set_effective_egress_state(None, "test route mutation")
            self.assertEqual(
                runtime._runtime_source_fetch_endpoints_provider("core"),
                (),
            )

    def test_verified_no_capture_route_opens_only_private_source_final_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                ),
            )
            runtime._runtime_generation = 9
            runtime._runtime_source_candidate_ports = lambda count: (
                19401,
                19402,
                19403,
            )[: int(count)]
            runtime._runtime_source_transport_for_config()
            runtime._mark_runtime_source_transport_loaded()
            runtime._runtime_source_listener_requires_auth = lambda _endpoint: True
            runner = FakeCoreRunner(running=True, port_ok=True)
            runner.status_payload.update(pid=4321, process_generation=7)
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_no_capture")
            manager = FakeTier3Manager()
            manager.effective_egress_ok = False
            manager.tier3_startup_gate_satisfied = False
            manager.tier3_traffic_capture_active = False
            wakes: list[str] = []
            manager.notify_tier3_source_supply_route_changed = wakes.append
            runtime.manager = manager
            ref = RuntimeNodeRef(
                tier="tier3",
                pool_kind="auto",
                identity="private-source-proof",
                proxy_name="private-source-proof",
            )
            snapshot = CoreLoadedRouteSnapshot(
                process_pid=4321,
                process_generation=7,
                core_generation=int(runtime._core_generation),
                semantic_digest="source-proof-digest",
                route_epoch=int(runtime._runtime_route_epoch),
                proxy_nodes=(ref,),
            )
            binding = RuntimeRouteBinding(
                process_pid=snapshot.process_pid,
                process_generation=snapshot.process_generation,
                core_generation=snapshot.core_generation,
                semantic_digest=snapshot.semantic_digest,
                route_epoch=snapshot.route_epoch,
                observed_outbound="FINAL-EXIT",
                route_chain=("FINAL-EXIT", ref.proxy_name),
                node=ref,
            )
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_snapshot = snapshot
                runtime._runtime_route_snapshot_payload = {"proxies": {
                    "FINAL-EXIT": {"type": "Selector", "all": [ref.proxy_name], "now": ref.proxy_name},
                    ref.proxy_name: {"type": "Shadowsocks", "alive": True},
                }}

            self.assertTrue(
                runtime._set_tier3_source_control_qualification(snapshot, binding)
            )
            endpoints = runtime._runtime_source_fetch_endpoints_provider("core")

            self.assertTrue(
                next(
                    endpoint
                    for endpoint in endpoints
                    if endpoint.route_kind is RuntimeSourceRouteKind.FINAL_EXIT
                ).ready
            )
            self.assertFalse(manager.effective_egress_ok)
            self.assertFalse(manager.tier3_startup_gate_satisfied)
            self.assertFalse(manager.tier3_traffic_capture_active)
            self.assertEqual(wakes, ["verified_final_exit_ready"])

            runtime._runtime_generation += 1
            self.assertFalse(runtime._source_control_final_exit_ready(manager))

    def test_core_marker_is_pure_and_loaded_source_endpoint_stays_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime._runtime_generation = 19
            manager = FakeTier3Manager()
            manager.effective_egress_ok = False
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="source-only",
                    protocol="ss",
                    uri="ss://source-only",
                    address="source-only.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]

            class PoolSnapshot:
                healthy = 1

            manager.tier3_auto_pool_count_snapshot = lambda: PoolSnapshot()
            runtime.manager = manager
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            runtime._set_core_runtime_phase("bootstrap")
            runtime._runtime_source_candidate_ports = lambda _count: (19401, 19402)
            runtime._runtime_source_listener_requires_auth = lambda _endpoint: True
            rendered = runtime._runtime_source_transport_for_config()
            runtime._mark_runtime_source_transport_loaded()
            runtime._last_core_export_sync_marker = (
                ("tier3", (("source-only",),)),
            )

            marker_one = runtime._core_export_sync_marker()
            marker_two = runtime._core_export_sync_marker()
            first = runtime._runtime_source_fetch_endpoints_provider("bootstrap")
            second = runtime._runtime_source_fetch_endpoints_provider("bootstrap")

            self.assertEqual(marker_one, marker_two)
            self.assertIs(runtime._runtime_source_transport_snapshot(), rendered)
            self.assertEqual(
                runtime._runtime_source_config_generation,
                rendered.config_generation,
            )
            self.assertEqual(first, second)
            self.assertTrue(first)
            readiness = {
                endpoint.route_kind: endpoint.ready for endpoint in first
            }
            self.assertFalse(readiness[RuntimeSourceRouteKind.FINAL_EXIT])
            self.assertTrue(
                all(
                    ready
                    for route_kind, ready in readiness.items()
                    if route_kind is not RuntimeSourceRouteKind.FINAL_EXIT
                )
            )

    def test_source_transport_switches_only_after_successful_core_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime._runtime_generation = 20
            manager = FakeTier3Manager()
            manager.effective_egress_ok = False
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="source-only",
                    protocol="ss",
                    uri="ss://source-only",
                    address="source-only.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            wake_reasons: list[str] = []
            manager.notify_tier3_source_supply_route_changed = wake_reasons.append

            class PoolSnapshot:
                healthy = 1

            manager.tier3_auto_pool_count_snapshot = lambda: PoolSnapshot()
            runtime.manager = manager
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            runtime._set_core_runtime_phase("bootstrap")
            runtime._runtime_source_candidate_ports = lambda _count: (19501, 19502)
            runtime._runtime_source_listener_requires_auth = lambda _endpoint: True
            first_generation = runtime._runtime_source_transport_for_config()
            runtime._mark_runtime_source_transport_loaded()
            runtime._last_core_export_sync_marker = (
                ("tier3", (("source-only",),)),
            )
            first_endpoints = runtime._runtime_source_fetch_endpoints_provider(
                "bootstrap"
            )

            proposed_generation = runtime._runtime_source_transport_for_config()
            before_sync = runtime._runtime_source_fetch_endpoints_provider("bootstrap")
            runtime._mark_core_export_sync_success(
                (("tier3", (("source-only",), ("second",))),)
            )
            after_sync = runtime._runtime_source_fetch_endpoints_provider("bootstrap")

            self.assertGreater(
                proposed_generation.config_generation,
                first_generation.config_generation,
            )
            self.assertEqual(before_sync, first_endpoints)
            self.assertTrue(after_sync)
            self.assertEqual(
                {endpoint.config_generation for endpoint in after_sync},
                {proposed_generation.config_generation},
            )
            self.assertIn(
                "runtime_source_listener_generation_loaded",
                wake_reasons,
            )

    def test_disabled_tier3_does_not_publish_or_render_private_source_transport(self) -> None:
        class DisabledTier3Manager(FakeTier3Manager):
            def _tier3_enabled(self) -> bool:
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.manager = DisabledTier3Manager()
            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            runtime._set_core_runtime_phase("formal_capture")
            base_payload = build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []}
            )

            with patch(
                "desktop_runtime.render_subscription_route",
                return_value=("application/yaml; charset=utf-8", base_payload),
            ):
                runtime_path = runtime.write_mihomo_runtime_config()

            parsed = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
            self.assertNotIn("listeners", parsed)
            self.assertIsNone(runtime._runtime_source_transport_snapshot())
            self.assertEqual(
                runtime._runtime_source_fetch_endpoints_provider("core"),
                (),
            )

    def test_default_subscription_preview_uses_manager_collection_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            expected = {
                "require_remote_source_route": True,
                "runtime_source_fetch_proxy_url": "http://127.0.0.1:7890",
                "runtime_source_fetch_proxy_first": True,
            }
            calls: list[str] = []
            manager._collection_settings = lambda: calls.append("settings") or expected
            runtime.manager = manager

            effective = runtime.subscription_preview_service._effective_collection_settings(
                runtime.config
            )

            self.assertEqual(effective, expected)
            self.assertEqual(calls, ["settings"])

    def test_source_discovery_blocks_raw_explicit_and_uses_verified_formal_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.read_sources_config = lambda: {}
            runtime.write_sources_config = lambda _payload: None
            explicit = {
                "enabled": True,
                "scheme": "http",
                "server": "bootstrap.example",
                "port": 8080,
                "use_for": {"collection": True},
            }

            def collection_settings_for_phase() -> dict[str, object]:
                runner = runtime.core_runner
                if runner is None or not runner.is_running():
                    return {
                        "require_remote_source_route": True,
                        "egress_proxy": explicit,
                    }
                proxy_url = runtime._runtime_source_fetch_proxy_provider("core")
                if proxy_url:
                    return {
                        "require_remote_source_route": True,
                        "runtime_source_fetch_proxy_url": proxy_url,
                    }
                return {"require_remote_source_route": True}

            manager._collection_settings = collection_settings_for_phase
            observed_proxy_urls: list[str] = []

            class Outcome:
                result = {"enabled": True, "discovered": 0, "added": 0, "sources": []}
                merged_sources = None

            def discover_update(**kwargs):
                observed_proxy_urls.append(str(kwargs.get("proxy_url") or ""))
                return Outcome()

            with patch(
                "desktop_runtime.discover_tier3_sources_update",
                side_effect=discover_update,
            ):
                runtime._runtime_source_fetch_proxy_provider = lambda _phase: None
                with self.assertRaisesRegex(
                    RuntimeError,
                    "validated proxy transport",
                ):
                    runtime.discover_tier3_sources(force=True)
                self.assertEqual(observed_proxy_urls, [])

                runner = FakeLifecycleCoreRunner()
                runner.start()
                runtime.core_runner = runner
                runtime._set_core_runtime_phase("formal_capture")
                manager.effective_egress_ok = None
                with self.assertRaisesRegex(
                    RuntimeError,
                    "validated proxy transport",
                ):
                    runtime.discover_tier3_sources(force=True)

                manager.effective_egress_ok = True
                runtime._set_effective_egress_state(True, "")
                runtime._runtime_source_fetch_proxy_provider = (
                    DesktopRuntime._runtime_source_fetch_proxy_provider.__get__(
                        runtime,
                        DesktopRuntime,
                    )
                )
                runtime.discover_tier3_sources(force=True)

            self.assertEqual(
                observed_proxy_urls,
                [
                    "http://127.0.0.1:7890",
                ],
            )

    def test_core_source_proxy_requires_verified_formal_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.manager = manager
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")

            manager.effective_egress_ok = None
            self.assertIsNone(runtime._runtime_source_fetch_proxy_provider("core"))
            self.assertIsNone(
                runtime._runtime_source_fetch_proxy_provider("bootstrap")
            )

            manager.effective_egress_ok = False
            self.assertIsNone(runtime._runtime_source_fetch_proxy_provider("core"))

            runtime._set_core_runtime_phase("formal_no_capture")
            self.assertIsNone(runtime._runtime_source_fetch_proxy_provider("core"))
            self.assertIsNone(
                runtime._runtime_source_fetch_proxy_provider("bootstrap")
            )

            manager.effective_egress_ok = True
            runtime._set_core_runtime_phase("formal_capture")
            self.assertEqual(
                runtime._runtime_source_fetch_proxy_provider("core"),
                "http://127.0.0.1:7890",
            )
            self.assertIsNone(
                runtime._runtime_source_fetch_proxy_provider("bootstrap")
            )

            runtime.stop_event.set()
            self.assertIsNone(runtime._runtime_source_fetch_proxy_provider("core"))

    def test_stop_and_restart_revoke_core_provider_before_runner_exits(self) -> None:
        for operation in ("stop", "restart"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                backend = MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                )
                runtime = self._runtime(temp_path, backend)
                manager = FakeTier3Manager()
                manager.effective_egress_ok = True
                manager.tier3_traffic_capture_active = True
                runner = FakeLifecycleCoreRunner()
                runner.start()
                runtime.manager = manager
                runtime.core_runner = runner
                runtime.running = True
                runtime._set_core_runtime_phase("formal_capture")
                runtime.traffic_monitor = FakeTrafficMonitor()
                self.assertEqual(
                    runtime._runtime_source_fetch_proxy_provider("core"),
                    "http://127.0.0.1:7890",
                )

                stop_entered = threading.Event()
                release_stop = threading.Event()

                def blocked_stop():
                    stop_entered.set()
                    if not release_stop.wait(timeout=2.0):
                        raise TimeoutError("test did not release runner stop")
                    runner.running = False
                    runner.status_payload = {
                        "running": False,
                        "process_running": False,
                        "proxy_ready": False,
                    }
                    runner.stopped = True
                    return {
                        **runner.status(),
                        "stopped": True,
                        "still_running": False,
                        "cleanup_pending": False,
                    }

                runner.stop = blocked_stop
                if operation == "restart":
                    runtime.start_core = lambda: {"running": False, "deferred": True}
                    target = runtime.restart_core
                else:
                    target = runtime.stop_core
                results: list[dict[str, object]] = []
                worker = threading.Thread(target=lambda: results.append(target()))
                worker.start()
                self.assertTrue(stop_entered.wait(timeout=1.0))

                self.assertIsNone(
                    runtime._runtime_source_fetch_proxy_provider("core")
                )
                self.assertEqual(runtime._current_core_runtime_phase(), "stopping")
                self.assertFalse(manager.tier3_traffic_capture_active)
                self.assertEqual(manager.effective_states[-1][0], False)

                release_stop.set()
                worker.join(timeout=2.0)
                self.assertFalse(worker.is_alive())
                self.assertTrue(results)

    def test_exit_quality_network_runs_data_plane_before_identity_or_health_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            # This test isolates data-plane/identity ordering, not Windows
            # capture. A separate regression covers the enabled system-proxy
            # target readback gate.
            runtime.system_proxy.enabled = False
            manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.manager = manager
            runtime.core_runner = runner
            self._install_manual_validation_authority(runtime)
            runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
            network_calls: list[str] = []

            class ExitQualityService:
                def check(self, *, proxy_url: str, request_id: str):
                    network_calls.append(proxy_url)
                    return {
                        "request_id": request_id,
                        "identity": {
                            "ipv4": {
                                "state": "confirmed",
                                "ip": "203.0.113.8",
                            }
                        },
                        "diagnostics": {},
                    }

            runtime.exit_quality_service = ExitQualityService()
            runtime._manual_current_exit_quality_contracts = lambda _session: (
                ManualDataPlaneProbeResult.https_204("neutral-a"),
                ManualDataPlaneProbeResult.https_204("neutral-b"),
            )
            identity_queued: list[str] = []
            runtime._queue_final_exit_identity_diagnostic = (
                lambda *, request_id: identity_queued.append(request_id)
            )
            runtime_states = (
                ("bootstrap", True),
                ("formal_no_capture", True),
                ("formal_capture", None),
                ("formal_capture", False),
            )
            for index, (phase, effective) in enumerate(runtime_states):
                with self.subTest(phase=phase, effective=effective):
                    runtime._set_core_runtime_phase(phase)
                    manager.effective_egress_ok = effective
                    result = runtime.check_exit_quality(
                        proxy_url="http://untrusted.example:8080",
                        request_id=f"data-plane-{index}",
                    )
                    self.assertEqual(
                        result["diagnostics"]["neutral_connectivity"]["state"],
                        "ok",
                    )
            self.assertEqual(network_calls, [])

            runtime._set_core_runtime_phase("formal_capture")
            manager.effective_egress_ok = True
            runtime._set_effective_egress_state(True, "")
            result = runtime.check_exit_quality(
                proxy_url="http://untrusted.example:8080",
                request_id="verified",
            )
            self.assertEqual(result["request_id"], "verified")
            self.assertEqual(network_calls, [])
            self.assertEqual(
                identity_queued,
                [
                    "data-plane-0",
                    "data-plane-1",
                    "data-plane-2",
                    "data-plane-3",
                    "verified",
                ],
            )

    def test_route_mutations_revoke_provider_before_write_or_select(self) -> None:
        for mutation in ("reload", "select"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                backend = MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                )
                runtime = self._runtime(temp_path, backend)
                manager = FakeTier3Manager()
                manager.effective_egress_ok = True
                manager.runtime_nodes["tier3"] = [
                    Node(
                        name="healthy-tier3",
                        protocol="ss",
                        uri="ss://healthy-tier3",
                        address="tier3.example.com",
                        port=8388,
                        tier="tier3",
                        status=NodeStatus.ALIVE,
                        method="aes-256-gcm",
                        password="pass",
                    )
                ]
                runtime.config["tiers"] = {
                    "tier1": {"nodes": []},
                    "tier2": {"nodes": [], "subscription_urls": []},
                    "tier3": {"enabled": True, "bootstrap_enabled": True},
                }
                runner = FakeLifecycleCoreRunner()
                runner.start()
                runtime.manager = manager
                runtime.core_runner = runner
                runtime.running = True
                runtime._set_core_runtime_phase("formal_capture")
                runtime._set_effective_egress_state(True, "")
                runtime._exit_quality_route_cache = {"route_marker": "old"}
                runtime.geo_updater = FakeGeoUpdater(temp_path / "geo")
                runtime._queue_incremental_egress_recheck = lambda: None
                mutation_entered = threading.Event()
                release_mutation = threading.Event()

                def block_mutation() -> None:
                    mutation_entered.set()
                    if not release_mutation.wait(timeout=2.0):
                        raise TimeoutError("test did not release route mutation")

                if mutation == "reload":
                    def write_runtime() -> Path:
                        block_mutation()
                        return temp_path / "mihomo-runtime.yaml"

                    runtime.write_mihomo_runtime_config = write_runtime
                    target = lambda: runtime.reload_core_config(
                        mode="incremental_export",
                        reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                    )
                else:
                    class BlockingClient:
                        def select_proxy(self, _group: str, _proxy: str):
                            block_mutation()
                            return {"selected": True}

                    runtime.api_client = BlockingClient()
                    target = lambda: runtime.select_mihomo_proxy(
                        "FINAL-EXIT",
                        "PROXY-FALLBACK",
                    )

                results: list[dict[str, object]] = []
                errors: list[Exception] = []

                def run_mutation() -> None:
                    try:
                        results.append(target())
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=run_mutation)
                worker.start()
                self.assertTrue(mutation_entered.wait(timeout=1.0))

                self.assertIsNone(
                    runtime._runtime_source_fetch_proxy_provider("core")
                )
                self.assertEqual(runtime._exit_quality_route_cache, {})
                self.assertEqual(manager.effective_states[-1][0], None)
                self.assertEqual(
                    runtime.geo_updater.reconfigure_calls[-1][
                        "egress_proxy"
                    ],
                    {},
                )

                release_mutation.set()
                worker.join(timeout=2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(results)

    def test_runtime_audit_overlay_reload_is_blocked_without_route_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            manager = FakeTier3Manager()
            manager.effective_egress_ok = True
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")
            runtime._set_effective_egress_state(True, "")
            runtime._exit_quality_route_cache = {"route_marker": "verified"}

            result = runtime._hot_reload_runtime_audit_overlay()

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["reload_blocked"])
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertEqual(
                runtime._exit_quality_route_cache,
                {"route_marker": "verified"},
            )
            self.assertTrue(manager.effective_states[-1][0])

    def test_runtime_audit_is_read_only_in_stability_rollback_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(
                Path(temp_dir),
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override=False)
                ),
            )
            manager = FakeRuntimeAuditManager()
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {
                                "type": "Selector",
                                "now": "PROXY-FALLBACK",
                                "all": ["PROXY-FALLBACK", "REJECT"],
                            },
                            "PROXY-FALLBACK": {
                                "type": "Fallback",
                                "now": "leaf-a",
                                "all": ["leaf-a", "REJECT"],
                            },
                            "leaf-a": {"type": "Shadowsocks"},
                            "REJECT": {"type": "Reject"},
                        }
                    }
                ]
            )

            result = runtime.audit_runtime_egress(
                "synthetic runtime failure",
                confirmed_transport_failure=True,
            )

            self.assertFalse(result["attempted"])
            self.assertTrue(result["diagnostic_only"])
            self.assertIn("automatic runtime recovery is disabled", result["reason"])
            self.assertEqual(runtime.api_client.select_calls, [])
            self.assertEqual(runtime.api_client.delay_calls, [])
            self.assertEqual(manager.marked_failures, [])

    def test_failed_route_mutations_never_leave_effective_state_pending(self) -> None:
        for failure_kind in (
            "incremental_hot_reload",
            "runtime_config_write",
        ):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                backend = MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                )
                runtime = self._runtime(temp_path, backend)
                manager = FakeTier3Manager()
                manager.effective_egress_ok = True
                manager.runtime_nodes["tier3"] = [
                    Node(
                        name="mutation-tier3",
                        protocol="ss",
                        uri="ss://mutation-tier3",
                        address="mutation-tier3.example.com",
                        port=8388,
                        tier="tier3",
                        status=NodeStatus.ALIVE,
                        method="aes-256-gcm",
                        password="pass",
                    )
                ]
                runtime.config["tiers"] = {
                    "tier1": {"nodes": []},
                    "tier2": {"nodes": [], "subscription_urls": []},
                    "tier3": {"enabled": True, "bootstrap_enabled": True},
                }
                runner = FakeLifecycleCoreRunner()
                runner.start()
                runtime.manager = manager
                runtime.core_runner = runner
                runtime.running = True
                runtime._set_core_runtime_phase("formal_capture")
                runtime._set_effective_egress_state(True, "")
                runtime._core_generation = 1
                runtime._runtime_initial_activation_generation = 1
                runtime._probe_mihomo_mixed_port = lambda: (
                    _ for _ in ()
                ).throw(RuntimeError("authoritative 204 failed"))
                runtime._verify_runtime_final_exit = lambda **_kwargs: (
                    _ for _ in ()
                ).throw(RuntimeError("authoritative 204 failed"))
                runtime_config = temp_path / "mihomo-runtime.yaml"
                runtime_config.write_text("old-loaded-config", encoding="utf-8")
                runtime.config["client"]["mihomo_runtime_config"] = str(
                    runtime_config
                )
                runtime.config["client"]["mihomo_controller"] = {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 9090,
                }

                if failure_kind == "runtime_config_write":
                    runtime.write_mihomo_runtime_config = lambda: (
                        _ for _ in ()
                    ).throw(RuntimeError("write failed"))
                    with self.assertRaisesRegex(RuntimeError, "write failed"):
                        runtime.reload_core_config(
                            mode="incremental_export",
                            reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                        )
                elif failure_kind == "incremental_hot_reload":
                    reload_calls = {"count": 0}

                    def hot_reload(**_kwargs):
                        reload_calls["count"] += 1
                        return {
                            "reloaded": reload_calls["count"] > 1,
                            "reason": "controller rejected new config",
                        }

                    runner.hot_reload = hot_reload

                    def write_new_config() -> Path:
                        runtime_config.write_text("new-config", encoding="utf-8")
                        return runtime_config

                    runtime.write_mihomo_runtime_config = write_new_config
                    result = runtime.reload_core_config(
                        mode="incremental_export",
                        reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                    )
                    self.assertTrue(result["kept_existing_core"])
                    self.assertTrue(result["restore_reload"]["reloaded"])
                else:
                    runtime.write_mihomo_runtime_config = lambda: runtime_config
                    runner.hot_reload = lambda **_kwargs: {
                        "reloaded": False,
                        "reason": "overlay rejected",
                    }
                    result = runtime._hot_reload_runtime_audit_overlay()
                    self.assertFalse(result["reloaded"])

                self.assertTrue(
                    runtime._wait_for_tier3_egress_activation(timeout=30.0)
                )
                self.assertFalse(runtime._core_route_mutation_is_pending())
                # A route mutation invalidates the previous authority.  When
                # the replacement config cannot be applied or rebound, the
                # runtime must publish a terminal data-plane failure instead
                # of reasserting the old, now-unproven SERVING state.
                self.assertFalse(manager.effective_states[-1][0])

    def test_first_run_bootstrap_starts_without_geodata_rules(
        self,
    ) -> None:
        # A first run has no GeoData and no explicit proxy.  The bootstrap
        # used to refuse to start, so no source was ever fetched, the pool
        # stayed empty and GeoData (proxy-only) could never be downloaded.
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_work_dir"] = str(
                temp_path / "data" / "mihomo"
            )
            manager = FakeTier3Manager(collect_alive=False)
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            self.assertEqual(runtime._missing_geodata_assets(), ["geoip", "geosite"])
            self.assertTrue(runtime._bootstrap_geodata_rules_deferred())
            runtime_config = temp_path / "bootstrap.yaml"
            runtime_config.write_text(
                build_bootstrap_clash_config(
                    "rules:\n"
                    "- DOMAIN-SUFFIX,local,DIRECT\n"
                    "- GEOSITE,CN,DIRECT\n"
                    "- GEOIP,CN,DIRECT,no-resolve\n"
                    "- MATCH,GENERAL\n"
                    "proxy-groups:\n"
                    "- name: PROXY-FALLBACK\n"
                    "  type: fallback\n"
                    "  proxies: [REJECT]\n",
                    defer_geodata_rules=runtime._bootstrap_geodata_rules_deferred(),
                ),
                encoding="utf-8",
            )
            runtime.write_mihomo_bootstrap_runtime_config = lambda: runtime_config

            proxy_url = runtime._runtime_source_fetch_proxy_provider("bootstrap")

            self.assertEqual(proxy_url, "http://127.0.0.1:7890")
            self.assertTrue(runtime.core_runner.started)
            self.assertEqual(runtime._current_core_runtime_phase(), "bootstrap")
            self.assertEqual(
                manager.tier3_last_collection_report[
                    "runtime_source_bootstrap_status"
                ],
                "ready",
            )
            rules = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))["rules"]
            self.assertEqual(rules, ["DOMAIN-SUFFIX,local,DIRECT", "MATCH,GENERAL"])

    def test_bootstrap_keeps_geodata_rules_once_geodata_or_proxy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            work_dir = temp_path / "data" / "mihomo"
            runtime.config["client"]["mihomo_work_dir"] = str(work_dir)
            runtime.config["egress_proxy"] = {
                "enabled": True,
                "scheme": "http",
                "server": "proxy.example.com",
                "port": 8080,
                "use_for": {"collection": True},
            }
            self.assertFalse(runtime._bootstrap_geodata_rules_deferred())
            runtime.config["egress_proxy"] = {"enabled": False}
            work_dir.mkdir(parents=True)
            (work_dir / "geoip.dat").write_bytes(b"geoip")
            (work_dir / "geosite.dat").write_bytes(b"geosite")
            self.assertFalse(runtime._bootstrap_geodata_rules_deferred())

    def _bootstrap_pool_runtime(self, temp_path: Path, *, pool_alive: bool):
        from runtime_source_transport import (
            RuntimeSourceListenerPorts,
            build_runtime_source_transport_generation,
        )

        backend = MemoryProxyBackend(
            ProxySettings(enabled=False, server="", override="")
        )
        runtime = self._runtime(temp_path, backend)
        work_dir = temp_path / "data" / "mihomo"
        runtime.config["client"]["mihomo_work_dir"] = str(work_dir)
        runtime.config["geo_update"] = {"enabled": True}
        runtime.geo_updater = FakeGeoUpdater(work_dir)
        manager = FakeTier3Manager(collect_alive=pool_alive)
        manager.refresh_tier3()
        runtime.manager = manager
        runner = FakeLifecycleCoreRunner()
        runner.start()
        runtime.core_runner = runner
        runtime._set_core_runtime_phase("bootstrap")
        transport = build_runtime_source_transport_generation(
            runtime_generation=1,
            config_generation=1,
            ports=RuntimeSourceListenerPorts(final_exit=61017, direct=61018),
        )
        with runtime._runtime_source_transport_lock:
            runtime._runtime_source_transport = transport
            runtime._runtime_source_loaded_transport = transport
        runtime_config = temp_path / "mihomo-runtime.yaml"
        runtime_config.write_text(
            "rules:\n- GEOSITE,CN,DIRECT\n- MATCH,GENERAL\n",
            encoding="utf-8",
        )
        return runtime, runtime_config, transport

    def test_first_run_geodata_downloads_through_bootstrap_pool_listener(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, runtime_config, transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=True
            )
            final_exit = next(
                item
                for item in transport.listeners
                if item.route_kind is RuntimeSourceRouteKind.FINAL_EXIT
            )

            runtime._ensure_geodata_ready_for_core_start(runtime_config)

            self.assertEqual(runtime.geo_updater.update_calls, 1)
            used = runtime.geo_updater.reconfigure_calls[0]["egress_proxy"]
            self.assertEqual(used["server"], "127.0.0.1")
            self.assertEqual(used["port"], 61017)
            self.assertEqual(used["username"], final_exit.credentials.username)
            self.assertEqual(used["password"], final_exit.credentials.password)
            # The per-run listener credentials do not outlive the preflight.
            self.assertEqual(runtime.geo_updater.reconfigure_calls[-1]["egress_proxy"], {})
            self.assertEqual(runtime._missing_geodata_assets(), [])

    def test_first_run_geodata_waits_for_a_healthy_bootstrap_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, runtime_config, _transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=False
            )

            with self.assertRaisesRegex(RuntimeError, "running Bastion Layer pool"):
                runtime._ensure_geodata_ready_for_core_start(runtime_config)

            self.assertEqual(runtime.geo_updater.update_calls, 0)

            runtime._set_core_runtime_phase("formal_no_capture")
            runtime.manager.collect_alive = True
            runtime.manager.refresh_tier3()
            with self.assertRaisesRegex(RuntimeError, "explicit egress proxy"):
                runtime._ensure_geodata_ready_for_core_start(runtime_config)
            self.assertEqual(runtime.geo_updater.update_calls, 0)

    def test_first_run_geodata_listener_is_one_per_runtime_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _runtime_config, transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=True
            )
            # A reader never allocates: the contract snapshot must not create
            # a listener the bootstrap writer did not render.
            self.assertIsNone(runtime._bootstrap_geodata_listener_spec(allocate=False))
            spec = runtime._bootstrap_geodata_listener_spec(allocate=True)
            self.assertIs(runtime._bootstrap_geodata_listener_spec(allocate=True), spec)
            self.assertIs(runtime._bootstrap_geodata_listener_spec(allocate=False), spec)
            self.assertNotIn(spec["port"], {item.port for item in transport.listeners})
            self.assertNotIn(spec["port"], runtime._runtime_source_excluded_ports())
            self.assertTrue(spec["username"] and spec["password"])

            runtime._runtime_generation += 1
            self.assertIsNone(runtime._bootstrap_geodata_listener_spec(allocate=False))
            renewed = runtime._bootstrap_geodata_listener_spec(allocate=True)
            self.assertNotEqual(renewed["password"], spec["password"])

    def test_first_run_geodata_goes_through_the_rotating_listener(self) -> None:
        from geo_updater import GeoDownloadPolicy

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, runtime_config, _transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=True
            )
            spec = runtime._bootstrap_geodata_listener_spec(allocate=True)
            probed: list[int] = []

            def requires_auth(endpoint) -> bool:
                probed.append(int(endpoint.port))
                return True

            runtime._runtime_source_listener_requires_auth = requires_auth

            runtime._ensure_geodata_ready_for_core_start(runtime_config)

            self.assertEqual(probed, [spec["port"]])
            used = runtime.geo_updater.reconfigure_calls[0]["egress_proxy"]
            self.assertEqual(
                (used["server"], used["port"], used["username"], used["password"]),
                ("127.0.0.1", spec["port"], spec["username"], spec["password"]),
            )
            options = runtime.geo_updater.update_options[0]
            self.assertIsInstance(options["policy"], GeoDownloadPolicy)
            self.assertEqual(options["assets"], ["geoip", "geosite"])
            self.assertFalse(options["cancel"]())
            runtime.stop_event.set()
            self.assertTrue(options["cancel"]())
            self.assertEqual(runtime.geo_updater.reconfigure_calls[-1]["egress_proxy"], {})

    def test_first_run_geodata_keeps_the_source_listener_without_a_rotating_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, runtime_config, _transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=True
            )
            # Rendered for this run, but the port does not answer as the
            # authenticated listener (it failed to bind).
            runtime._bootstrap_geodata_listener_spec(allocate=True)
            runtime._runtime_source_listener_requires_auth = lambda _endpoint: False

            runtime._ensure_geodata_ready_for_core_start(runtime_config)

            self.assertEqual(
                runtime.geo_updater.reconfigure_calls[0]["egress_proxy"]["port"], 61017
            )
            options = runtime.geo_updater.update_options[0]
            # One member carries every connection there: changing attempts
            # would only repeat it.
            self.assertIsNone(options["policy"])
            self.assertEqual(options["assets"], ["geoip", "geosite"])

    def test_stop_during_first_run_geodata_is_a_cancelled_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, runtime_config, _transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=True
            )
            runtime.geo_updater.create_assets = False

            def stopped_update(**_options):
                runtime.stop_event.set()
                return {"errors": {"geoip": "GeoDownloadCancelled: GeoData download cancelled"}}

            runtime.geo_updater.update_now = stopped_update

            with self.assertRaisesRegex(RuntimeStartCancelled, "GeoData preflight"):
                runtime._ensure_geodata_ready_for_core_start(runtime_config)

    def test_failed_first_run_geodata_is_tried_again_without_a_pool_change(self) -> None:
        # 11-10, real network: the download failed with three settled pool
        # members, and nothing tried again for the remaining 150 s of the run.
        from desktop_runtime import GeoDataPreflightError

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, runtime_config, _transport = self._bootstrap_pool_runtime(
                Path(temp_dir), pool_alive=True
            )
            runtime.geo_updater.create_assets = False
            with self.assertRaises(GeoDataPreflightError) as raised:
                runtime._ensure_geodata_ready_for_core_start(runtime_config)
            wrapped = RecoverableCoreStartError("promotion failed")
            wrapped.__cause__ = raised.exception
            self.assertTrue(runtime._caused_by_geodata_preflight(wrapped))
            self.assertFalse(runtime._caused_by_geodata_preflight(RuntimeError("other")))

            timers: list = []

            class FakeTimer:
                def __init__(self, delay, function, args=()):
                    self.delay, self.function, self.args = delay, function, args
                    self.daemon = False
                    self.name = ""
                    self.started = False
                    self.cancelled = False
                    timers.append(self)

                def start(self) -> None:
                    self.started = True

                def cancel(self) -> None:
                    self.cancelled = True

            queued: list[int] = []
            runtime._queue_tier3_core_gate_attempt = lambda: queued.append(1) or True
            runtime.running = True
            runtime._runtime_generation = 4
            runtime._last_failed_tier3_core_sync_marker = ("pool",)

            with patch("desktop_runtime.threading.Timer", FakeTimer):
                runtime._schedule_geodata_preflight_retry()
                # One retry is pending at a time.
                runtime._schedule_geodata_preflight_retry()
                self.assertEqual([(timer.delay, timer.started) for timer in timers], [(15.0, True)])
                timers[0].function(*timers[0].args)
                # The same pool may carry the download now: the failed-marker
                # cooldown must not hold this retry back.
                self.assertIsNone(runtime._last_failed_tier3_core_sync_marker)
                self.assertEqual(queued, [1])
                for _ in range(3):
                    runtime._schedule_geodata_preflight_retry()
                    timers[-1].function(*timers[-1].args)
                self.assertEqual([timer.delay for timer in timers], [15.0, 30.0, 60.0, 60.0])

                # A retry of an earlier run, after a stop, or once the Core is
                # no longer the bootstrap Core, does nothing.
                queued.clear()
                runtime._schedule_geodata_preflight_retry()
                runtime._runtime_generation = 5
                timers[-1].function(*timers[-1].args)
                runtime._runtime_generation = 4
                runtime._schedule_geodata_preflight_retry()
                runtime._set_core_runtime_phase("formal_no_capture")
                timers[-1].function(*timers[-1].args)
                runtime._set_core_runtime_phase("bootstrap")
                runtime._schedule_geodata_preflight_retry()
                runtime.stop_event.set()
                timers[-1].function(*timers[-1].args)
                runtime.stop_event.clear()
                # A retry still pending from an earlier run gives way to this
                # run's own, which starts at the short delay again.
                runtime._schedule_geodata_preflight_retry()
                stale = timers[-1]
                runtime._runtime_generation = 6
                runtime._schedule_geodata_preflight_retry()
                self.assertTrue(stale.cancelled)
                self.assertEqual((timers[-1].delay, timers[-1].args), (15.0, (6,)))
                timers[-1].function(*timers[-1].args)
                self.assertEqual(queued, [1])
                queued.clear()
                runtime.stop_event.set()
                self.assertEqual(queued, [])
                runtime.stop_event.clear()

            # A download that succeeds starts the delay over.
            runtime.geo_updater.create_assets = True
            runtime._ensure_geodata_ready_for_core_start(runtime_config)
            self.assertEqual(runtime._geodata_preflight_retries, 0)

    def test_zero_node_bootstrap_starts_with_six_defaults_when_geodata_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            compatibility_defaults = [
                {"rule_type": "direct", "value": "geosite:CN"},
                {"rule_type": "direct", "value": "geoip:CN"},
                {"rule_type": "direct", "value": "suffix:github.com"},
                {
                    "rule_type": "direct",
                    "value": "suffix:githubusercontent.com",
                },
                {"rule_type": "direct", "value": "suffix:t.me"},
                {"rule_type": "direct", "value": "suffix:telegram.me"},
            ]
            runtime_config = temp_path / "bootstrap.yaml"
            runtime_config.write_text(
                build_bootstrap_clash_config(
                    build_full_clash_config(
                        {
                            "tier1": [],
                            "tier2": [],
                            "tier3": [],
                            "exit": [],
                        },
                        routing_config={
                            "rules": {
                                "compatibility_defaults": compatibility_defaults
                            }
                        },
                    )
                ),
                encoding="utf-8",
            )
            runtime.write_mihomo_bootstrap_runtime_config = lambda: runtime_config
            runtime._runtime_config_uses_geodata = lambda _path: True
            runtime._missing_geodata_assets = lambda: []

            proxy_url = runtime._runtime_source_fetch_proxy_provider("bootstrap")
            parsed = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))

            self.assertEqual(proxy_url, "http://127.0.0.1:7890")
            self.assertTrue(runtime.core_runner.started)
            # The retired hidden compatibility stage is never emitted, even
            # when a stale config still carries it; the bootstrap still starts.
            self.assertNotIn("GEOSITE,CN,DIRECT", parsed["rules"])
            self.assertNotIn("DOMAIN-SUFFIX,github.com,DIRECT", parsed["rules"])
            self.assertNotIn("DOMAIN-SUFFIX,telegram.me,DIRECT", parsed["rules"])
            self.assertEqual(parsed["rules"][-1], "MATCH,GENERAL")
            fallback = next(
                group
                for group in parsed["proxy-groups"]
                if group["name"] == "PROXY-FALLBACK"
            )
            self.assertEqual(fallback["proxies"], ["REJECT"])

    def test_start_core_hot_promotes_running_bootstrap_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")
            runtime._prime_tier3_pool_before_core_start = lambda: None
            runtime._require_tier3_ready_for_core_start = lambda: None
            runtime.write_mihomo_no_capture_runtime_config = (
                lambda: temp_path / "formal-no-capture.yaml"
            )
            queued: list[str] = []
            runtime._queue_tier3_egress_activation = lambda: queued.append(
                "activation"
            )

            status = runtime.start_core()

            self.assertTrue(status["running"])
            self.assertEqual(runner.hot_reloaded, 1)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(queued, [])
            self.assertFalse(runtime._initial_egress_activation_deferred)
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            self.assertEqual(queued, ["activation"])

    def test_reload_during_formal_standby_preserves_no_capture_until_204(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime.write_mihomo_no_capture_runtime_config = (
                lambda: temp_path / "formal-no-capture.yaml"
            )
            queued: list[str] = []
            runtime._queue_tier3_egress_activation = lambda: queued.append(
                "activation"
            )

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertTrue(result["reloaded"])
            self.assertFalse(result["capture_enabled"])
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(runner.hot_reloaded, 1)
            self.assertEqual(queued, ["activation"])

    def test_activation_cannot_overwrite_concurrent_incremental_no_capture_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            manager = FakeTier3Manager()
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime._tier3_egress_activation_generation = 1
            no_capture_path = temp_path / "p2-no-capture.yaml"

            def write_p2_no_capture() -> Path:
                no_capture_path.write_text("allow-lan: false\n", encoding="utf-8")
                with runtime._core_lifecycle_lock:
                    runtime._staged_capture_runtime_config_payload = "p2-capture"
                    runtime._staged_capture_runtime_config_path = (
                        temp_path / "p2-capture.yaml"
                    )
                return no_capture_path

            runtime.write_mihomo_no_capture_runtime_config = write_p2_no_capture
            runtime._ensure_geodata_ready_for_core_start = lambda _path: None
            reload_entered = threading.Event()
            release_reload = threading.Event()
            original_hot_reload = runner.hot_reload

            def blocked_hot_reload(**kwargs):
                reload_entered.set()
                if not release_reload.wait(timeout=2.0):
                    raise TimeoutError("test did not release incremental reload")
                return original_hot_reload(**kwargs)

            runner.hot_reload = blocked_hot_reload

            def supersede_activation() -> None:
                with runtime._tier3_egress_activation_lock:
                    runtime._tier3_egress_activation_generation += 1

            runtime._queue_tier3_egress_activation = supersede_activation
            reload_results: list[dict[str, object]] = []
            reload_thread = threading.Thread(
                target=lambda: reload_results.append(
                    runtime.reload_core_config(
                        mode="incremental_export",
                        reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                    )
                )
            )
            reload_thread.start()
            self.assertTrue(reload_entered.wait(timeout=1.0))

            activation_errors: list[Exception] = []

            def stale_activation() -> None:
                try:
                    runtime._hot_reload_runtime_config_phase(
                        runner,
                        capture_enabled=True,
                        expected_activation_generation=1,
                    )
                except Exception as exc:
                    activation_errors.append(exc)

            activation_thread = threading.Thread(target=stale_activation)
            activation_thread.start()
            time.sleep(0.05)
            self.assertTrue(activation_thread.is_alive())

            release_reload.set()
            reload_thread.join(timeout=2.0)
            activation_thread.join(timeout=2.0)

            self.assertFalse(reload_thread.is_alive())
            self.assertFalse(activation_thread.is_alive())
            self.assertTrue(reload_results[0]["reloaded"])
            self.assertEqual(runner.hot_reloaded, 1)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(len(activation_errors), 1)
            self.assertIn("superseded", str(activation_errors[0]))
            self.assertEqual(
                runtime._staged_capture_runtime_config_payload,
                "p2-capture",
            )

    def test_restart_during_204_probe_invalidates_old_activation_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.running = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            original_runner_stop = runner.stop

            def clean_runner_stop():
                result = original_runner_stop()
                return {**result, "stopped": True, "still_running": False}

            runner.stop = clean_runner_stop
            runtime.core_runner = runner
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime._core_generation = 1
            probe_entered = threading.Event()
            release_probe = threading.Event()

            def blocked_probe() -> dict[str, object]:
                probe_entered.set()
                if not release_probe.wait(timeout=2.0):
                    raise TimeoutError("test did not release 204 probe")
                return {"status": 204}

            runtime._verify_runtime_final_exit = (
                lambda **_kwargs: blocked_probe()
            )
            # system-proxy admission proves the selected member before capture;
            # block the production leaf-probe stage, not the later full-chain
            # verifier.
            runtime._probe_runtime_leaf = (
                lambda *_args, **_kwargs: blocked_probe()
            )
            capture_reloads: list[str] = []
            runtime._hot_reload_runtime_config_phase = (
                lambda *_args, **_kwargs: capture_reloads.append("capture")
            )

            def start_replacement_core() -> dict[str, object]:
                runner.start()
                with runtime._core_lifecycle_lock:
                    runtime._core_generation += 1
                runtime._set_core_runtime_phase("formal_no_capture")
                return runner.status()

            runtime.start_core = start_replacement_core
            runtime._queue_tier3_egress_activation()
            self.assertTrue(probe_entered.wait(timeout=1.0))

            restart_result = runtime.restart_core()
            self.assertTrue(restart_result["running"])
            release_probe.set()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            # The old generation must not attach capture before its 204
            # completes, and restart invalidates it before publication.
            self.assertEqual(capture_reloads, [])
            self.assertEqual(runtime._core_generation, 2)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")

    def test_activation_cannot_interleave_with_blocked_runtime_overlay_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            manager = FakeTier3Manager()
            manager.effective_egress_ok = True
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")
            runtime._tier3_egress_activation_generation = 1
            runtime_path = temp_path / "overlay.yaml"
            runtime.write_mihomo_runtime_config = lambda: runtime_path

            overlay_result = runtime._hot_reload_runtime_audit_overlay()

            self.assertFalse(overlay_result["reloaded"])
            self.assertTrue(overlay_result["reload_blocked"])
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertEqual(runtime._tier3_egress_activation_generation, 1)

            runtime._hot_reload_runtime_config_phase(
                runner,
                capture_enabled=True,
                expected_activation_generation=1,
            )

            self.assertEqual(runner.hot_reloaded, 1)

    def test_terminal_under_floor_supply_stops_only_bootstrap_and_reports_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier3": {"enabled": True, "min_alive_nodes": 2}
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 2
            manager.last_collect_tier3 = 321.0
            manager.get_runtime_snapshot = lambda *, include_nodes=True: {}
            manager.tier3_last_collection_report = {
                "startup_supply_exhausted": True,
                "source_supply_terminal": True,
            }
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")
            with runtime._core_lifecycle_lock:
                runtime._staged_capture_runtime_config_payload = "stale-capture"
                runtime._staged_capture_runtime_config_path = temp_path / "stale.yaml"

            runtime._on_tier3_pool_committed(
                [],
                {
                    "startup_supply_exhausted": True,
                    "source_supply_terminal": True,
                },
            )

            self.assertFalse(runner.is_running())
            self.assertEqual(runtime._current_core_runtime_phase(), "stopped")
            self.assertIsNone(runtime._staged_capture_runtime_config_payload)
            self.assertIsNone(runtime._staged_capture_runtime_config_path)
            self.assertEqual(manager.last_collect_tier3, 321.0)
            self.assertTrue(runtime._tier3_startup_terminal_failure)
            self.assertIn("source supply was exhausted", runtime._last_tier3_startup_error)
            status = runtime.status()
            self.assertEqual(status["startup_phase"], "failed")
            self.assertFalse(status["core_standby"])

    def test_terminal_zero_node_empty_marker_sync_stops_bootstrap_and_reports_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier3": {"enabled": True, "min_alive_nodes": 2}
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 2
            manager.get_runtime_snapshot = lambda *, include_nodes=True: {}
            manager.tier3_last_collection_report = {
                "startup_supply_exhausted": True,
                "source_supply_terminal": True,
            }
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")

            self.assertEqual(manager.runtime_nodes["tier3"], [])
            self.assertFalse(runtime._tier3_core_sync_marker())

            runtime._sync_core_config_after_pool_update()

            self.assertFalse(runner.is_running())
            self.assertEqual(runtime._current_core_runtime_phase(), "stopped")
            self.assertTrue(runtime._tier3_startup_terminal_failure)
            self.assertIn("source supply was exhausted", runtime.last_error)
            status = runtime.status()
            self.assertEqual(status["startup_phase"], "failed")
            self.assertFalse(status["core_standby"])

    def test_terminal_startup_error_reports_only_safe_failure_class_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            for failure_class in (
                "rule_blocked",
                "no_proxy",
                "transport_failed",
            ):
                with self.subTest(failure_class=failure_class):
                    manager = FakeTier3Manager(collect_alive=False)
                    manager.tier3_last_collection_report = {
                        "cache_local_healthy_count": 1,
                        "cache_local_required_count": 3,
                        "failed_sources": 2,
                        "source_supply_terminal_reason": failure_class,
                        "raw_error": "https://secret.example/private?id=credential",
                    }

                    message = runtime._tier3_terminal_startup_error(
                        1,
                        3,
                        manager,
                    )

                    self.assertIn("cache/local=1/3", message)
                    self.assertIn(f"{failure_class}=2", message)
                    self.assertNotIn("secret.example", message)
                    self.assertNotIn("credential", message)

    def test_terminal_under_floor_supply_does_not_stop_formal_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 2
            manager.tier3_last_collection_report = {
                "startup_supply_exhausted": True,
                "source_supply_terminal": True,
            }
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")

            runtime._on_tier3_pool_committed(
                [],
                {"startup_supply_exhausted": True},
            )

            self.assertTrue(runner.is_running())
            self.assertFalse(runner.stopped)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_capture")
            self.assertFalse(runtime._tier3_startup_terminal_failure)

    def test_terminal_bootstrap_stop_serializes_after_inflight_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 2
            manager.last_collect_tier3 = 777.0
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")
            bootstrap_path = temp_path / "bootstrap.yaml"
            bootstrap_path.write_text("rules: []\n", encoding="utf-8")
            runtime.write_mihomo_bootstrap_runtime_config = lambda: bootstrap_path
            runtime._ensure_bootstrap_prerequisites = lambda *_args, **_kwargs: None
            reload_entered = threading.Event()
            release_reload = threading.Event()
            original_hot_reload = runner.hot_reload

            def blocked_hot_reload(**kwargs):
                reload_entered.set()
                if not release_reload.wait(timeout=2.0):
                    raise TimeoutError("test did not release bootstrap reload")
                return original_hot_reload(**kwargs)

            runner.hot_reload = blocked_hot_reload
            reload_results: list[dict[str, object]] = []
            reload_thread = threading.Thread(
                target=lambda: reload_results.append(
                    runtime.reload_core_config(
                        mode="incremental_export",
                        reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                    )
                )
            )
            reload_thread.start()
            self.assertTrue(reload_entered.wait(timeout=1.0))

            terminal_thread = threading.Thread(
                target=lambda: runtime._on_tier3_pool_committed(
                    [],
                    {
                        "startup_supply_exhausted": True,
                        "source_supply_terminal": True,
                    },
                )
            )
            terminal_thread.start()
            time.sleep(0.05)
            self.assertTrue(runner.is_running())
            self.assertTrue(terminal_thread.is_alive())

            release_reload.set()
            reload_thread.join(timeout=2.0)
            terminal_thread.join(timeout=2.0)

            self.assertFalse(reload_thread.is_alive())
            self.assertFalse(terminal_thread.is_alive())
            self.assertTrue(reload_results[0]["reloaded"])
            self.assertFalse(runner.is_running())
            self.assertEqual(runtime._current_core_runtime_phase(), "stopped")
            self.assertEqual(manager.last_collect_tier3, 777.0)

    def test_stop_intent_between_tier3_proxy_capture_and_traffic_start_rolls_back_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.running = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime.traffic_monitor = FakeTrafficMonitor()
            proxy_capture_completed = threading.Event()
            release_activation = threading.Event()
            restore_calls: list[str] = []
            original_apply_system_proxy_capture = (
                runtime._apply_system_proxy_capture_after_core_start
            )
            original_restore_system_proxy = runtime.restore_system_proxy

            def capture_proxy_then_pause(active_runner) -> None:
                original_apply_system_proxy_capture(active_runner)
                proxy_capture_completed.set()
                if not release_activation.wait(timeout=1.0):
                    raise TimeoutError(
                        "test did not release Tier3 activation after proxy capture"
                    )

            def restore_system_proxy() -> dict[str, object]:
                restore_calls.append("restore")
                return original_restore_system_proxy()

            runtime._verify_tier3_final_exit_for_activation = lambda: None
            runtime._probe_runtime_leaf = (
                lambda *_args, **_kwargs: {"delay": 20}
            )
            runtime._hot_reload_runtime_config_phase = (
                lambda _runner, *, capture_enabled, **_kwargs: (
                    runtime._set_core_runtime_phase(
                        "formal_capture"
                        if capture_enabled
                        else "formal_no_capture"
                    )
                    or temp_path / "mihomo-runtime.yaml"
                )
            )
            runtime._apply_system_proxy_capture_after_core_start = (
                capture_proxy_then_pause
            )
            runtime.restore_system_proxy = restore_system_proxy
            runtime._queue_tier3_egress_activation()
            try:
                self.assertTrue(proxy_capture_completed.wait(timeout=1.0))
                self.assertTrue(backend.settings.enabled)
                self.assertEqual(backend.settings.server, "127.0.0.1:7890")

                intent = runtime.request_stop(
                    actor="test",
                    reason="stop Tier3 activation after proxy capture",
                )
            finally:
                release_activation.set()

            self.assertTrue(intent.accepted)
            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertFalse(backend.settings.enabled)
            self.assertEqual(backend.settings.server, "")
            self.assertFalse(runtime.system_proxy.active)
            self.assertEqual(restore_calls, ["restore"])
            self.assertFalse(
                any(ok is True for ok, _reason in manager.effective_states)
            )
            self.assertEqual(manager.core_running_marks, [])
            self.assertFalse(
                manager.tier3_last_collection_report.get(
                    "traffic_capture_active",
                    False,
                )
            )

    def test_start_core_refuses_tier3_direct_fallback_when_startup_collection_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager(collect_alive=False)
            # Simulate a stale timestamp so priming should reset it to 0.
            manager.last_collect_tier3 = 99999.0
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            with self.assertRaisesRegex(RuntimeError, "no validated proxy nodes"):
                runtime.start_core()

            self.assertEqual(manager.check_calls, [])
            self.assertEqual(manager.refreshed, 1)
            self.assertEqual(manager.last_collect_tier3, 0.0)
            self.assertFalse(runtime.core_runner.started)
            self.assertFalse(runtime.core_runner.stopped)

    def test_start_core_collects_tier3_before_writing_config_when_pool_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            status = runtime.start_core()
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )

            self.assertTrue(status["running"])
            self.assertEqual(manager.check_calls, [])
            self.assertEqual(manager.refreshed, 1)
            self.assertTrue(runtime.core_runner.started)
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertTrue(manager.effective_egress_ok)

    def test_start_core_triggers_background_tier3_collect_and_waits_only_for_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 3,
                },
            }

            class BackgroundTier3Manager(FakeTier3Manager):
                def __init__(self) -> None:
                    super().__init__(collect_alive=False)
                    self.min_alive_nodes = 3
                    self.background_target_alive = 25
                    self.trigger_calls: list[bool] = []
                    self.background_actions = {
                        "tier3_collect": {
                            "running": False,
                            "last_started_at": None,
                            "last_completed_at": None,
                            "last_error": None,
                            "last_result": None,
                        }
                    }

                def trigger_tier3_collect(self, *, startup_mode: bool = False):
                    self.trigger_calls.append(bool(startup_mode))
                    self.background_actions["tier3_collect"]["running"] = True
                    self.runtime_nodes["tier3"] = [
                        Node(
                            name=f"streamed-{index}",
                            protocol="ss",
                            uri=f"ss://streamed-{index}",
                            address=f"streamed-{index}.example.com",
                            port=8388,
                            tier="tier3",
                            status=NodeStatus.ALIVE,
                            method="aes-256-gcm",
                            password="pass",
                        )
                        for index in range(3)
                    ]
                    return {"accepted": True, "action": "tier3_collect", "status": "started"}

            manager = BackgroundTier3Manager()
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            status = runtime.start_core()
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )

            self.assertTrue(status["running"])
            self.assertEqual(manager.trigger_calls, [True])
            self.assertEqual(manager.refreshed, 0)
            self.assertTrue(runtime.core_runner.started)
            self.assertFalse(manager.tier3_last_collection_report.get("core_standby", False))
            self.assertEqual(manager.tier3_last_collection_report["background_target_alive"], 25)

    def test_startup_gate_wait_retriggers_collect_when_source_retry_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)

            class RetryDueManager(FakeTier3Manager):
                def __init__(self) -> None:
                    super().__init__(collect_alive=False)
                    self.min_alive_nodes = 3
                    self.trigger_calls: list[bool] = []
                    self.background_actions = {
                        "tier3_collect": {
                            "running": False,
                            "last_started_at": None,
                            "last_completed_at": None,
                            "last_error": None,
                            "last_result": None,
                        }
                    }
                    self.tier3_last_collection_report = {
                        "source_count": 5,
                        "source_fetch_ok": 2,
                        "source_fetch_failed": 3,
                        "source_fetch_pending": 0,
                        "source_retry_due": True,
                        "source_retry_due_count": 3,
                        "startup_deferred_reason": "pre_gate_source_retry_due",
                    }

                def trigger_tier3_collect(self, *, startup_mode: bool = False):
                    self.trigger_calls.append(bool(startup_mode))
                    self.runtime_nodes["tier3"] = [
                        Node(
                            name=f"gate-{index}",
                            protocol="ss",
                            uri=f"ss://gate-{index}",
                            address=f"gate-{index}.example.com",
                            port=8388,
                            tier="tier3",
                            status=NodeStatus.ALIVE,
                            method="aes-256-gcm",
                            password="pass",
                        )
                        for index in range(3)
                    ]
                    self.tier3_last_collection_report = {
                        "source_count": 5,
                        "source_fetch_ok": 5,
                        "source_fetch_failed": 0,
                        "source_retry_due": False,
                    }
                    return {"accepted": True, "action": "tier3_collect", "status": "started"}

            manager = RetryDueManager()
            runtime.manager = manager
            runtime._tier3_startup_gate_timeout_seconds = lambda _manager: 2.0

            healthy = runtime._wait_for_tier3_startup_gate(manager, 3)

            self.assertEqual(healthy, 3)
            self.assertEqual(manager.trigger_calls, [True])

    def test_tier3_ready_reports_deferred_while_source_retry_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 3
            manager.tier3_last_collection_report = {
                "source_count": 5,
                "source_fetch_ok": 2,
                "source_fetch_failed": 3,
                "source_retry_due": True,
                "source_retry_due_count": 3,
                "startup_deferred_reason": "pre_gate_source_retry_due",
            }
            runtime.manager = manager
            runtime._tier3_required_for_start = True

            with self.assertRaisesRegex(RecoverableCoreStartError, "startup deferred.*pre_gate_source_retry_due") as ctx:
                runtime._require_tier3_ready_for_core_start()

            self.assertNotIn("no validated proxy nodes", str(ctx.exception))

    def test_tier3_ready_uses_live_unit_registry_when_report_source_count_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 3
            manager.tier3_source_count = 5
            manager.tier3_last_collection_report = {
                "source_count": 0,
                "configured_source_count": 0,
                "source_fetch_pending": 0,
            }
            manager.tier3_refresh_progress = {
                "configured_source_count": 5,
                "pending_source_units": 6,
            }
            runtime.manager = manager
            runtime._tier3_required_for_start = True

            self.assertEqual(
                runtime._tier3_startup_live_collection_reason(manager),
                "source_unit_coverage_pending",
            )
            with self.assertRaisesRegex(
                RecoverableCoreStartError,
                "startup deferred.*source_unit_coverage_pending",
            ):
                runtime._require_tier3_ready_for_core_start()

    def test_start_core_refuses_single_tier3_node_when_ui_min_requires_more(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 2,
                },
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 2
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            with self.assertRaisesRegex(RuntimeError, "only 1/2 validated proxy nodes"):
                runtime.start_core()

            self.assertEqual(manager.refreshed, 1)
            self.assertEqual(manager.last_collect_tier3, 0.0)
            self.assertFalse(runtime.core_runner.started)

    def test_start_core_refuses_one_core_verified_node_below_startup_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 2,
                },
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 2

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"startup_prelaunch_validation": "tcp"},
                )

            def refresh_tier3(*, suppress_intervention_alert: bool = False, startup_mode: bool = False):
                manager.refreshed += 1
                manager.tier3_last_collection_report = {
                    "startup_mode": bool(startup_mode),
                    "validation": {"prelaunch_validation": "tcp"},
                }
                manager.runtime_nodes["tier3"] = [node("core-good"), node("core-bad")]
                manager.last_collect_tier3 = 0.0
                return list(manager.runtime_nodes["tier3"]), 2

            manager.refresh_tier3 = refresh_tier3
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name == "core-good":
                    return {"delay": 25}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            status = runtime.start_core()
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(status["running"])
            self.assertTrue(runtime.core_runner.started)
            self.assertFalse(runtime.core_runner.stopped)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["core-good", "core-bad"])
            self.assertEqual(manager.runtime_nodes["tier3"][1].status, NodeStatus.ALIVE)
            self.assertEqual([node.name for node in runtime._tier3_nodes_for_core_export()], ["core-good", "core-bad"])
            self.assertFalse(manager.tier3_last_collection_report.get("core_standby", False))
            self.assertEqual(manager.last_collect_tier3, 0.0)

    def test_core_repair_failure_defers_retry_until_pool_marker_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, first_marker = self._core_repair_then_serving_admission(
                Path(temp_dir),
                online_core_growth=False,
            )

            deadline = time.monotonic() + 1
            while (
                runtime._tier3_pool_sync_parked_key is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertTrue(manager.effective_egress_ok)
            self.assertIsNone(runtime.last_error)
            self.assertEqual(runtime._last_tier3_core_sync_marker, first_marker)
            self.assertNotEqual(first_marker, runtime._tier3_core_sync_marker())
            self.assertIsNotNone(runtime._tier3_pool_sync_parked_key)
            runtime._update_running_tier3_core_provider.assert_not_called()

    def test_online_growth_pushes_serving_admission_into_the_provider(self) -> None:
        # tiers.tier3.online_core_growth (default on, 11-01): the same serving
        # admission crosses the online cohort freeze as capacity growth.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, manager, first_marker = self._core_repair_then_serving_admission(
                Path(temp_dir),
                online_core_growth=True,
            )
            provider = runtime._update_running_tier3_core_provider

            deadline = time.monotonic() + 2
            while (
                provider.call_count == 0
                or runtime._tier3_pool_sync_worker_running
            ) and time.monotonic() < deadline:
                time.sleep(0.01)

            grown_marker = runtime._tier3_core_sync_marker()
            self.assertNotEqual(first_marker, grown_marker)
            provider.assert_called_once_with(grown_marker, online_growth=True)
            self.assertEqual(runtime._last_tier3_core_sync_marker, grown_marker)
            self.assertIsNone(runtime._tier3_pool_sync_parked_key)
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertTrue(manager.effective_egress_ok)
            self.assertIsNone(runtime.last_error)

    def _core_repair_then_serving_admission(
        self,
        temp_path: Path,
        *,
        online_core_growth: bool,
    ):
        backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
        runtime = self._runtime(temp_path, backend)
        self._install_injected_runtime_authority(runtime)
        runtime.config["tiers"] = {
            "tier1": {"nodes": []},
            "tier2": {"nodes": [], "subscription_urls": []},
            "tier3": {
                "enabled": True,
                "bootstrap_enabled": True,
                "min_alive_nodes": 3,
                "online_core_growth": online_core_growth,
                # The 8e lane these two tests pin; the verified lane (11-02)
                # is covered by tests/test_tier3_verified_provider_writes_real_core.py.
                "verified_provider_writes": False,
            },
        }

        class DeferredRetryManager(FakeTier3Manager):
            def __init__(self) -> None:
                super().__init__()
                self.min_alive_nodes = 3
                self.trigger_calls: list[bool] = []

            def trigger_tier3_collect(self, *, startup_mode: bool = False):
                self.trigger_calls.append(bool(startup_mode))
                return {"accepted": True, "status": "running"}

        def node(index: int) -> Node:
            return Node(
                name=f"core-{index}",
                protocol="ss",
                uri=f"ss://core-{index}",
                address=f"core-{index}.example.com",
                port=8388 + index,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )

        manager = DeferredRetryManager()
        manager.runtime_nodes["tier3"] = [node(0), node(1), node(2)]
        runtime.manager = manager
        runtime.system_proxy.enabled = False
        runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
        runtime.core_runner = FakeLifecycleCoreRunner()
        runtime.traffic_monitor = FakeTrafficMonitor()
        runtime._probe_mihomo_mixed_port = lambda: {"url": "https://probe.example", "latency_ms": 25}
        runtime._verify_runtime_final_exit = lambda **_kwargs: {
            "url": "https://probe.example",
            "status": 204,
            "latency_ms": 25,
        }
        first_marker = runtime._tier3_core_sync_marker()
        delay_allowed = {"core-0", "core-1"}

        def fake_delay(name, *, url=None, timeout_ms=5000):
            if name in delay_allowed:
                return {"delay": 25}
            raise RuntimeError("timeout")

        runtime.test_proxy_delay = fake_delay

        status = runtime.start_core()
        self.assertTrue(
            self._commit_deferred_initial_egress_admission(runtime)
        )
        runtime._wait_for_tier3_egress_activation()

        self.assertTrue(status["running"])
        self.assertFalse(runtime.core_runner.stopped)
        self.assertEqual(manager.trigger_calls, [])
        self.assertEqual(runtime._tier3_core_sync_marker(), first_marker)
        self.assertIsNone(runtime._last_failed_tier3_core_sync_marker)
        self.assertEqual(runtime.traffic_monitor.started, 0)
        self.assertTrue(manager.effective_egress_ok)

        runtime.running = True
        runtime._attach_manager_runtime_hooks(manager)
        runtime._update_running_tier3_core_provider = MagicMock(
            return_value={
                "updated": True,
                "semantic_digest": "provider-fixture",
            }
        )
        runtime._tier3_pool_sync_stable_hard_limit_seconds = 0.05
        runtime._tier3_pool_sync_max_wait_seconds = 0.05
        runtime._tier3_selected_leaf_qualification_evidence = MagicMock(
            return_value=(
                "core-0",
                "tier3|ss|core-0.example.com|8388|pass||||||",
                ("core-0", "stable-signature"),
            )
        )
        delay_allowed.add("core-2")
        delay_allowed.add("core-3")
        manager.runtime_nodes["tier3"] = [node(0), node(1), node(2), node(3)]
        manager.on_tier3_pool_committed(manager.runtime_nodes["tier3"], {"alive_count": 4, "total_count": 4})
        return runtime, manager, first_marker

    def test_start_core_keeps_multiple_core_verified_nodes_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 2,
                },
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 2

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"startup_prelaunch_validation": "tcp"},
                )

            def refresh_tier3(*, suppress_intervention_alert: bool = False, startup_mode: bool = False):
                manager.refreshed += 1
                manager.tier3_last_collection_report = {
                    "startup_mode": bool(startup_mode),
                    "validation": {"prelaunch_validation": "tcp"},
                }
                manager.runtime_nodes["tier3"] = [node("core-bad"), node("core-good-one"), node("core-good-two")]
                manager.last_collect_tier3 = 0.0
                return list(manager.runtime_nodes["tier3"]), 3

            manager.refresh_tier3 = refresh_tier3
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name == "core-good-one":
                    return {"delay": 25}
                if name == "core-good-two":
                    return {"delay": 35}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            status = runtime.start_core()

            self.assertTrue(status["running"])
            runtime._wait_for_tier3_egress_activation()
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["core-bad", "core-good-one", "core-good-two"])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["core-bad", "core-good-one", "core-good-two"],
            )
            self.assertNotIn("startup_core_repair", manager.tier3_last_collection_report)
            self.assertEqual(len(manager.targeted_rechecks), 0)

    def test_tier3_core_export_preserves_loaded_order_and_appends_new_admissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()

            def node(name: str, revision: int) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"tier3_admission_pool_revision": revision},
                )

            first = node("first", 10)
            second = node("second", 11)
            appended = node("appended", 12)
            # The public manager view may reorder existing nodes as latency and
            # health metadata change.  Core membership must remain append-only.
            manager.runtime_nodes["tier3"] = [appended, second, first]
            runtime.manager = manager
            runtime._last_synced_tier3_nodes = [first.clone(), second.clone()]

            exported = runtime._tier3_nodes_for_core_export()

            self.assertEqual(
                [item.name for item in exported],
                ["first", "second", "appended"],
            )

    def test_tier3_core_export_uses_durable_admission_revision_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()

            def node(name: str, revision: int) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"tier3_admission_pool_revision": revision},
                )

            manager.runtime_nodes["tier3"] = [
                node("third", 13),
                node("first", 7),
                node("second", 9),
            ]
            runtime.manager = manager

            exported = runtime._tier3_nodes_for_core_export()

            self.assertEqual(
                [item.name for item in exported],
                ["first", "second", "third"],
            )

    def test_preserved_tier3_leaf_requires_same_signature_and_controller_presence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            # The controller payload below names leaves positionally (switch off).
            runtime.config.setdefault("tiers", {}).setdefault("tier3", {})["verified_provider_writes"] = False
            manager = FakeTier3Manager()
            manager.tier3_traffic_capture_active = True
            selected = Node(
                name="selected",
                protocol="ss",
                uri="ss://selected",
                address="selected.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [selected]
            runtime.manager = manager
            identity = runtime._node_identity_text(selected)
            signature = tier3_export_node_signature(selected)
            runtime._tier3_runtime_qualified[identity] = {
                "proxy_signature": signature,
            }
            runtime._tier3_runtime_final_exit_ok = True
            runtime.mihomo_proxies = lambda: {
                "proxies": {"selected": {"type": "Shadowsocks", "alive": True}}
            }
            evidence = ("selected", identity, signature)

            self.assertEqual(
                runtime._tier3_preserved_leaf_qualification_evidence(evidence),
                evidence,
            )

            runtime.mihomo_proxies = lambda: {
                "proxies": {"selected": {"type": "Shadowsocks", "alive": False}}
            }
            self.assertEqual(
                runtime._tier3_preserved_leaf_qualification_evidence(evidence),
                evidence,
            )

            runtime.mihomo_proxies = lambda: {"proxies": {}}
            self.assertIsNone(
                runtime._tier3_preserved_leaf_qualification_evidence(evidence)
            )

    def test_start_core_defers_retry_when_core_prunes_below_startup_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 3,
                    "startup_core_recovery_retries": 1,
                },
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            refresh_pools = [
                [node("core-good-one"), node("core-bad-one"), node("core-bad-two")],
                [node("core-good-one"), node("core-good-two"), node("core-good-three"), node("core-bad-three")],
            ]

            def refresh_tier3(*, suppress_intervention_alert: bool = False, startup_mode: bool = False):
                manager.refreshed += 1
                selected = refresh_pools[min(manager.refreshed - 1, len(refresh_pools) - 1)]
                manager.tier3_last_collection_report = {
                    "startup_mode": bool(startup_mode),
                    "validation": {"prelaunch_validation": "full", "target_alive": 5},
                }
                manager.runtime_nodes["tier3"] = list(selected)
                manager.last_collect_tier3 = 0.0
                return list(selected), len(selected)

            manager.refresh_tier3 = refresh_tier3
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            def fake_delay(name, *, url=None, timeout_ms=5000):
                delays = {
                    "core-good-one": 25,
                    "core-good-two": 35,
                    "core-good-three": 45,
                }
                if name in delays:
                    return {"delay": delays[name]}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            status = runtime.start_core()
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(status["running"])
            self.assertFalse(runtime.core_runner.stopped)
            self.assertEqual(manager.refreshed, 1)
            self.assertEqual(
                [node.name for node in manager.runtime_nodes["tier3"]],
                ["core-good-one", "core-bad-one", "core-bad-two"],
            )
            self.assertFalse(manager.tier3_last_collection_report.get("core_standby", False))

    def test_start_core_uses_background_collect_to_retry_when_core_prunes_below_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 3,
                    "startup_core_recovery_retries": 1,
                },
            }

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            class BackgroundRepairManager(FakeTier3Manager):
                def __init__(self) -> None:
                    super().__init__(collect_alive=False)
                    self.min_alive_nodes = 3
                    self.background_target_alive = 3
                    self.trigger_calls: list[bool] = []
                    self.runtime_nodes["tier3"] = [node("core-good-one"), node("core-bad-one"), node("core-bad-two")]
                    self.tier3_last_collection_report = {
                        "startup_mode": True,
                        "target_alive": 3,
                        "background_target_alive": 3,
                        "min_alive_nodes": 3,
                        "validation": {"prelaunch_validation": "full", "target_alive": 3},
                    }
                    self.background_actions = {
                        "tier3_collect": {
                            "running": False,
                            "last_started_at": None,
                            "last_completed_at": None,
                            "last_error": None,
                            "last_result": None,
                        }
                    }

                def trigger_tier3_collect(self, *, startup_mode: bool = False):
                    self.trigger_calls.append(bool(startup_mode))
                    self.background_actions["tier3_collect"]["running"] = True
                    self.runtime_nodes["tier3"] = [
                        node("core-good-one"),
                        node("core-good-two"),
                        node("core-good-three"),
                        node("core-bad-three"),
                    ]
                    return {"accepted": True, "action": "tier3_collect", "status": "started"}

            manager = BackgroundRepairManager()
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            def fake_delay(name, *, url=None, timeout_ms=5000):
                delays = {
                    "core-good-one": 25,
                    "core-good-two": 35,
                    "core-good-three": 45,
                }
                if name in delays:
                    return {"delay": delays[name]}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            status = runtime.start_core()
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(status["running"])
            self.assertFalse(runtime.core_runner.stopped)
            self.assertEqual(manager.trigger_calls, [])
            self.assertEqual(manager.refreshed, 0)
            self.assertEqual(
                [item.name for item in manager.runtime_nodes["tier3"]],
                ["core-good-one", "core-bad-one", "core-bad-two"],
            )

    def test_second_empty_start_recollects_after_core_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 3,
                    "startup_core_recovery_retries": 0,
                },
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            refresh_pools = [
                [node("core-good-one"), node("core-good-two"), node("core-bad-one")],
                [node("core-good-one"), node("core-good-two"), node("core-good-three")],
            ]

            def refresh_tier3(*, suppress_intervention_alert: bool = False, startup_mode: bool = False):
                manager.refreshed += 1
                selected = refresh_pools[min(manager.refreshed - 1, len(refresh_pools) - 1)]
                manager.tier3_last_collection_report = {
                    "startup_mode": bool(startup_mode),
                    "validation": {"prelaunch_validation": "full", "target_alive": 3},
                }
                manager.runtime_nodes["tier3"] = list(selected)
                manager.last_collect_tier3 = 0.0
                return list(selected), len(selected)

            manager.refresh_tier3 = refresh_tier3
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            def fake_delay(name, *, url=None, timeout_ms=5000):
                delays = {
                    "core-good-one": 25,
                    "core-good-two": 35,
                    "core-good-three": 45,
                }
                if name in delays:
                    return {"delay": delays[name]}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            status = runtime.start_core()
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(status["running"])
            self.assertFalse(runtime.core_runner.stopped)
            self.assertEqual(manager.refreshed, 1)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["core-good-one", "core-good-two", "core-bad-one"])
            self.assertEqual(manager.runtime_nodes["tier3"][2].status, NodeStatus.ALIVE)
            self.assertEqual(manager.last_collect_tier3, 0.0)
            self.assertFalse(manager.tier3_last_collection_report.get("core_standby", False))

            second_status = runtime.start_core()

            self.assertTrue(second_status["running"])
            self.assertEqual(manager.refreshed, 1)

    def test_start_core_uses_primary_without_waiting_for_tier3_when_primary_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": [{"name": "primary"}]},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier1"] = [
                Node(
                    name="primary",
                    protocol="ss",
                    uri="ss://primary",
                    address="primary.example.com",
                    port=8388,
                    tier="tier1",
                    status=NodeStatus.ALIVE,
                )
            ]
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            runtime.start_core()

            self.assertEqual(manager.refreshed, 0)
            self.assertEqual(manager.primary_check_calls, [])
            self.assertFalse(runtime._tier3_required_for_start)

    def test_start_core_falls_back_to_tier3_when_primary_is_configured_but_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": [{"name": "primary"}]},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier1"] = [
                Node(
                    name="primary",
                    protocol="ss",
                    uri="ss://primary",
                    address="primary.example.com",
                    port=8388,
                    tier="tier1",
                    status=NodeStatus.DEAD,
                )
            ]
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            runtime.start_core()

            self.assertEqual(manager.primary_check_calls, ["tier1", "tier2"])
            self.assertEqual(manager.refreshed, 1)
            self.assertTrue(runtime._tier3_required_for_start)
            self.assertTrue(runtime.core_runner.started)

    def test_start_core_uses_cached_tier3_when_startup_target_is_met(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True, "min_alive_nodes": 3},
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            manager.runtime_nodes["tier3"] = [
                Node(
                    name=f"cached-{index}",
                    protocol="ss",
                    uri=f"ss://cached-{index}",
                    address=f"cached-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                for index in range(3)
            ]
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            runtime.start_core()

            self.assertEqual(manager.check_calls, [])
            self.assertEqual(manager.refreshed, 0)

    def test_start_core_does_not_revalidate_committed_tier3_through_snapshot_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True, "min_alive_nodes": 3},
            }

            class StreamingStartupManager(FakeTier3Manager):
                def __init__(self) -> None:
                    super().__init__()
                    self.startup_revalidation_targets: list[int | None] = []

                def revalidate_tier3_startup_pool(self, *, target_alive: int | None = None):
                    self.startup_revalidation_targets.append(target_alive)
                    return list(self.runtime_nodes["tier3"])

            manager = StreamingStartupManager()
            manager.min_alive_nodes = 3
            manager.runtime_nodes["tier3"] = [
                Node(
                    name=f"cached-{index}",
                    protocol="ss",
                    uri=f"ss://cached-{index}",
                    address=f"cached-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                for index in range(3)
            ]
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            runtime.start_core()

            self.assertEqual(manager.startup_revalidation_targets, [])
            self.assertEqual(manager.check_calls, [])
            self.assertEqual(manager.refreshed, 0)

    def test_cached_tier3_below_ui_target_stays_due_for_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 3,
                },
            }
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            manager.refresh_tier3()
            manager.last_collect_tier3 = 99999.0
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            with self.assertRaisesRegex(RecoverableCoreStartError, "only 1/3 validated proxy nodes"):
                runtime.start_core()

            self.assertEqual(manager.check_calls, [])
            self.assertEqual(manager.last_collect_tier3, 0.0)
            self.assertTrue(manager.tier3_last_collection_report["background_expansion_due"])


    def test_start_core_marks_cached_tier3_pool_as_recent_when_target_met(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True, "min_alive_nodes": 1},
            }
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            manager.last_collect_tier3 = 0.0
            runtime.manager = manager
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            runtime.start_core()

            self.assertEqual(manager.check_calls, [])
            self.assertGreater(manager.last_collect_tier3, 0.0)

    def test_tier3_startup_background_expansion_record_marks_underfilled_pool_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            manager.last_collect_tier3 = 99999.0

            report = runtime._record_tier3_startup_background_expansion(
                manager,
                1,
                mark_collect_complete_when_full=True,
            )

            self.assertIs(report, manager.tier3_last_collection_report)
            self.assertTrue(report["background_expansion_due"])
            self.assertEqual(manager.last_collect_tier3, 0.0)

    def test_tier3_startup_background_expansion_uses_background_target_after_minimum_is_met(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            manager.background_target_alive = 9
            manager.last_collect_tier3 = 99999.0

            report = runtime._record_tier3_startup_background_expansion(
                manager,
                3,
                mark_collect_complete_when_full=True,
            )

            self.assertTrue(report["background_expansion_due"])
            self.assertEqual(manager.last_collect_tier3, 0.0)

    def test_tier3_startup_background_expansion_record_preserves_fresh_start_collect_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 1
            manager.last_check_monotonic = {"tier3": 222.0}
            manager.last_collect_tier3 = 0.0

            cached_report = runtime._record_tier3_startup_background_expansion(
                manager,
                1,
                mark_collect_complete_when_full=True,
            )
            self.assertEqual(manager.last_collect_tier3, 222.0)
            manager.last_collect_tier3 = 0.0
            fresh_report = runtime._record_tier3_startup_background_expansion(
                manager,
                1,
                mark_collect_complete_when_full=False,
            )

            self.assertFalse(cached_report["background_expansion_due"])
            self.assertIs(fresh_report, manager.tier3_last_collection_report)
            self.assertFalse(fresh_report["background_expansion_due"])
            self.assertEqual(manager.last_collect_tier3, 0.0)

    def test_default_probe_url_ignores_xray_helper_for_mihomo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["testing"] = {"xray_helper": {"probe_url": "https://www.google.com/generate_204"}}

            self.assertEqual(runtime._default_probe_url(), "https://cp.cloudflare.com")

    def test_runtime_config_protects_every_configured_authoritative_probe_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runtime.config["testing"] = {
                "mihomo_helper": {
                    "probe_urls": [
                        "https://probe.example/generate_204",
                        "https://second-probe.example/status/204",
                    ]
                }
            }
            runtime.config["routing"] = {
                "rules": {
                    "direct": [
                        "DOMAIN,probe.example,DIRECT",
                        "DOMAIN,second-probe.example,DIRECT",
                    ]
                }
            }

            base_payload = build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config=runtime.config["routing"],
            )
            with patch(
                "desktop_runtime.render_subscription_route",
                return_value=("application/yaml; charset=utf-8", base_payload),
            ):
                runtime_path = runtime.write_mihomo_runtime_config()
            parsed = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))

            for host in ("probe.example", "second-probe.example"):
                system_rule = f"DOMAIN,{host},FINAL-EXIT"
                direct_rule = f"DOMAIN,{host},DIRECT"
                self.assertIn(system_rule, parsed["rules"])
                self.assertIn(direct_rule, parsed["rules"])
                self.assertLess(parsed["rules"].index(system_rule), parsed["rules"].index(direct_rule))

            bootstrap = yaml.safe_load(
                build_bootstrap_clash_config(runtime_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(bootstrap["rules"], parsed["rules"])

    def test_controller_route_readiness_follows_empty_group_current_selection(self) -> None:
        managed = DesktopRuntime._mihomo_selected_routes_ready(
            {
                "proxies": {
                    "DIRECT": {"type": "Direct"},
                    "COMPATIBLE": {"type": "Compatible"},
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK"],
                    },
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "managed-leaf",
                        "all": ["managed-leaf"],
                    },
                    "managed-leaf": {"type": "Shadowsocks"},
                }
            }
        )
        compatible = DesktopRuntime._mihomo_selected_routes_ready(
            {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK"],
                    },
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "COMPATIBLE",
                        "all": [],
                    },
                }
            }
        )
        rejected = DesktopRuntime._mihomo_selected_routes_ready(
            {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK"],
                    },
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "REJECT",
                        "all": [],
                    },
                }
            }
        )
        gateway_with_unsafe_dialer_chain = (
            DesktopRuntime._mihomo_selected_routes_ready(
                {
                    "proxies": {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "EXIT-SELECT",
                            "all": ["EXIT-SELECT"],
                        },
                        "EXIT-SELECT": {
                            "type": "Selector",
                            "now": "gateway-leaf",
                            "all": ["gateway-leaf"],
                        },
                        "gateway-leaf": {"type": "Shadowsocks"},
                        "PROXY-FALLBACK": {
                            "type": "Fallback",
                            "now": "COMPATIBLE",
                            "all": [],
                        },
                    }
                }
            )
        )

        self.assertTrue(managed["ready"])
        self.assertFalse(compatible["ready"])
        self.assertIn("COMPATIBLE", compatible["reason"])
        self.assertTrue(rejected["ready"])
        self.assertEqual(
            rejected["routes"]["PROXY-FALLBACK"]["terminal"],
            "fail_closed",
        )
        self.assertFalse(gateway_with_unsafe_dialer_chain["ready"])
        self.assertIn(
            "PROXY-FALLBACK",
            gateway_with_unsafe_dialer_chain["reason"],
        )

    def test_bootstrap_provider_waits_out_compatible_window_before_returning_mixed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                ),
            )
            runtime_path = temp_path / "data" / "mihomo-runtime.yaml"
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_path.write_text("{}\n", encoding="utf-8")
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")
            runtime._core_export_sync_marker = lambda: ("stable",)
            runtime._last_core_export_sync_marker = ("stable",)

            class SequencedController:
                def __init__(self) -> None:
                    self.calls = 0

                def proxies(self):
                    self.calls += 1
                    selected = "COMPATIBLE" if self.calls == 1 else "tier3-leaf"
                    entries = {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "PROXY-FALLBACK",
                            "all": ["PROXY-FALLBACK"],
                        },
                        "PROXY-FALLBACK": {
                            "type": "Fallback",
                            "now": selected,
                            "all": [],
                        },
                    }
                    if selected == "tier3-leaf":
                        entries["tier3-leaf"] = {"type": "Shadowsocks"}
                    return {"proxies": entries}

            controller = SequencedController()
            runtime.api_client = controller
            runtime._wait_for_mihomo_route_ready = (
                DesktopRuntime._wait_for_mihomo_route_ready.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )

            proxy_url = runtime._runtime_source_fetch_proxy_provider("bootstrap")

            self.assertEqual(proxy_url, "http://127.0.0.1:7890")
            self.assertGreaterEqual(controller.calls, 2)
            self.assertTrue(runner.is_running())

    def test_route_readiness_waits_for_controller_to_load_written_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config = Path(temp_dir) / "mihomo-runtime.yaml"
            runtime_config.write_text(
                yaml.safe_dump(
                    {
                        "proxies": [
                            {"name": "new-leaf", "type": "ss"},
                        ],
                        "proxy-groups": [
                            {
                                "name": "PROXY-FALLBACK",
                                "type": "fallback",
                                "proxies": ["new-leaf"],
                            },
                            {
                                "name": "FINAL-EXIT",
                                "type": "select",
                                "proxies": ["PROXY-FALLBACK"],
                            },
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            old_graph = {
                "proxies": {
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "REJECT",
                        "all": ["REJECT"],
                    },
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK"],
                    },
                }
            }
            written_graph = {
                "proxies": {
                    "new-leaf": {"type": "Shadowsocks"},
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "new-leaf",
                        "all": ["new-leaf"],
                    },
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK"],
                    },
                }
            }

            old_state = DesktopRuntime._mihomo_runtime_graph_ready(
                old_graph,
                runtime_config,
            )
            written_state = DesktopRuntime._mihomo_runtime_graph_ready(
                written_graph,
                runtime_config,
            )

            self.assertFalse(old_state["ready"])
            self.assertTrue(written_state["ready"])

    def test_bootstrap_provider_stops_core_when_compatible_window_never_closes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                ),
            )
            runtime_path = temp_path / "data" / "mihomo-runtime.yaml"
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_path.write_text("{}\n", encoding="utf-8")
            runtime.manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")
            runtime._core_export_sync_marker = lambda: ("stable",)
            runtime._last_core_export_sync_marker = ("stable",)
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {
                                "type": "Selector",
                                "now": "PROXY-FALLBACK",
                                "all": ["PROXY-FALLBACK"],
                            },
                            "PROXY-FALLBACK": {
                                "type": "Fallback",
                                "now": "COMPATIBLE",
                                "all": [],
                            },
                        }
                    }
                ]
            )
            runtime._startup_probe_timeout_seconds = lambda: 0.02
            runtime._wait_for_mihomo_route_ready = (
                DesktopRuntime._wait_for_mihomo_route_ready.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )

            proxy_url = runtime._runtime_source_fetch_proxy_provider("bootstrap")

            self.assertIsNone(proxy_url)
            self.assertTrue(runner.stopped)
            self.assertEqual(runtime._current_core_runtime_phase(), "stopped")
            self.assertEqual(
                runtime.manager.tier3_last_collection_report[
                    "runtime_source_bootstrap_status"
                ],
                "unavailable",
            )

    def test_authoritative_probe_does_not_start_public_request_in_compatible_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                ),
            )
            runtime_path = temp_path / "data" / "mihomo-runtime.yaml"
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_path.write_text("{}\n", encoding="utf-8")
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {
                                "type": "Selector",
                                "now": "PROXY-FALLBACK",
                                "all": ["PROXY-FALLBACK"],
                            },
                            "PROXY-FALLBACK": {
                                "type": "Fallback",
                                "now": "COMPATIBLE",
                                "all": [],
                            },
                        }
                    }
                ]
            )
            runtime._startup_probe_timeout_seconds = lambda: 0.01
            runtime._wait_for_mihomo_route_ready = (
                DesktopRuntime._wait_for_mihomo_route_ready.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )
            runtime._probe_mihomo_mixed_port = (
                DesktopRuntime._probe_mihomo_mixed_port.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )

            with patch("desktop_runtime.build_opener") as public_opener:
                with self.assertRaisesRegex(RuntimeError, "COMPATIBLE"):
                    runtime._probe_mihomo_mixed_port()

            public_opener.assert_not_called()

    def test_authoritative_mixed_port_probe_rejects_200_and_203(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["testing"] = {
                "mihomo_helper": {
                    "probe_urls": ["https://probe.example/generate_204"],
                    "expected_statuses": [200, 203, 204],
                }
            }
            runtime._probe_mihomo_mixed_port = (
                DesktopRuntime._probe_mihomo_mixed_port.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )

            class ProbeResponse:
                def __init__(self, status: int, body: bytes = b"") -> None:
                    self.status = status
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def getcode(self) -> int:
                    return self.status

                def read(self, _limit: int) -> bytes:
                    return self.body

            class ProbeOpener:
                def __init__(self, status: int, body: bytes = b"") -> None:
                    self.status = status
                    self.body = body

                def open(self, _request, timeout=None):
                    del timeout
                    return ProbeResponse(self.status, self.body)

            for status_code in (200, 203):
                with self.subTest(status=status_code), patch(
                    "desktop_runtime.build_opener",
                    return_value=ProbeOpener(status_code),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        f"unexpected status {status_code}",
                    ):
                        runtime._probe_mihomo_mixed_port()

            with patch(
                "desktop_runtime.build_opener",
                return_value=ProbeOpener(204, b"\n"),
            ):
                with self.assertRaisesRegex(RuntimeError, "unexpected status 204"):
                    runtime._probe_mihomo_mixed_port()

            with patch(
                "desktop_runtime.build_opener",
                return_value=ProbeOpener(204),
            ):
                result = runtime._probe_mihomo_mixed_port()
            self.assertEqual(result["status"], 204)

    def test_authoritative_probe_results_and_failures_redact_url_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            secret_url = (
                "https://user:password@Probe.Example:8443/private/generate_204"
                "?token=super-secret"
            )
            runtime.config["testing"] = {
                "mihomo_helper": {"probe_urls": [secret_url]}
            }
            runtime._probe_mihomo_mixed_port = (
                DesktopRuntime._probe_mihomo_mixed_port.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )

            class ProbeResponse:
                def __init__(self, status: int) -> None:
                    self.status = status

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def getcode(self) -> int:
                    return self.status

                def read(self, _limit: int) -> bytes:
                    return b""

            class ProbeOpener:
                def __init__(self, status: int) -> None:
                    self.status = status

                def open(self, _request, timeout=None):
                    del timeout
                    return ProbeResponse(self.status)

            with patch(
                "desktop_runtime.build_opener",
                return_value=ProbeOpener(204),
            ):
                success = runtime._probe_mihomo_mixed_port()
            self.assertEqual(success["url"], "probe.example:8443")
            self.assertNotIn("password", repr(success))
            self.assertNotIn("super-secret", repr(success))
            self.assertEqual(
                runtime._safe_probe_endpoint_label(
                    "https://user:secret@[2001:0db8:0:0:0:0:0:1]:9443/path?x=y"
                ),
                "[2001:db8::1]:9443",
            )

            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime._warm_tier3_group_after_core_start = (
                DesktopRuntime._warm_tier3_group_after_core_start.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )
            runtime._tier3_is_primary_export = lambda: True
            runtime.config["client"]["mihomo_controller"] = {"enabled": False}
            with patch(
                "desktop_runtime.build_opener",
                return_value=ProbeOpener(203),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    runtime._verify_tier3_after_core_reload()

            failure_text = str(caught.exception)
            manager_reason = manager.effective_states[-1][1]
            combined = f"{failure_text}\n{manager_reason}"
            self.assertIn("probe.example:8443", combined)
            for secret in (
                "user",
                "password",
                "private",
                "token",
                "super-secret",
            ):
                self.assertNotIn(secret, combined)

            with patch(
                "desktop_runtime.build_opener",
                return_value=ProbeOpener(203),
            ), self.assertLogs(runtime.logger, level="WARNING") as logs:
                runtime._warm_tier3_group_after_core_start(strict=False)
            rendered_logs = "\n".join(logs.output)
            self.assertIn("probe.example:8443", rendered_logs)
            for secret in ("password", "private", "token", "super-secret"):
                self.assertNotIn(secret, rendered_logs)

    def test_authoritative_redirect_keeps_initial_activation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["testing"] = {
                "mihomo_helper": {
                    "probe_urls": ["https://probe.example/generate_204"]
                }
            }
            runtime._probe_mihomo_mixed_port = (
                DesktopRuntime._probe_mihomo_mixed_port.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )
            manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.manager = manager
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime._hot_reload_runtime_config_phase = (
                lambda *_args, **_kwargs: runtime._set_core_runtime_phase(
                    "formal_capture"
                )
            )
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._verify_runtime_final_exit = (
                lambda **_kwargs: runtime._probe_mihomo_mixed_port(
                    require_route_readback=False
                )
            )
            runtime._submit_current_route_probe_failure = (
                lambda *_args, **_kwargs: False
            )

            class ProbeResponse:
                status = 204

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def getcode(self) -> int:
                    return 204

                def read(self, _limit: int) -> bytes:
                    return b""

            class CrossHostRedirectOpener:
                def __init__(self, handlers) -> None:
                    self.redirect_handler = next(
                        handler
                        for handler in handlers
                        if handler.__class__.__name__
                        == "_RejectAuthoritativeProbeRedirects"
                    )

                def open(self, request, timeout=None):
                    del timeout
                    redirected = self.redirect_handler.redirect_request(
                        request,
                        None,
                        302,
                        "Found",
                        {},
                        "https://user-direct.example/generate_204",
                    )
                    if redirected is not None:
                        return ProbeResponse()
                    raise RuntimeError(
                        "302 redirect to user-direct.example was refused"
                    )

            with patch(
                "desktop_runtime.build_opener",
                side_effect=lambda *handlers: CrossHostRedirectOpener(handlers),
            ):
                runtime._queue_tier3_egress_activation()
                self.assertTrue(
                    runtime._wait_for_tier3_egress_activation(timeout=30.0)
                )

            self.assertEqual(
                runtime._current_core_runtime_phase(),
                "formal_no_capture",
            )
            self.assertEqual(runtime.traffic_monitor.snapshot()["running"], False)
            self.assertFalse(bool(manager.effective_states[-1][0]))
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.DATA_PLANE_FAILED,
            )

    def test_geodata_preflight_initializes_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            work_dir = temp_path / "data" / "mihomo"
            runtime.config["client"]["mihomo_work_dir"] = str(work_dir)
            runtime.config["geo_update"] = {"enabled": True, "request_timeout": 7}
            runtime.config["egress_proxy"] = {
                "enabled": True,
                "scheme": "http",
                "server": "proxy.example.com",
                "port": 8080,
                "use_for": {"collection": True},
            }
            runtime.geo_updater = FakeGeoUpdater(work_dir)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text(
                "rules:\n- GEOSITE,category-ads-all,REJECT\n- GEOIP,US,GENERAL,no-resolve\n",
                encoding="utf-8",
            )

            runtime._ensure_geodata_ready_for_core_start(runtime_config)

            self.assertEqual(runtime.geo_updater.update_calls, 1)
            self.assertEqual(
                runtime.geo_updater.reconfigure_calls[0]["egress_proxy"]["server"],
                "proxy.example.com",
            )
            self.assertEqual(runtime._missing_geodata_assets(), [])

    def test_missing_geodata_without_explicit_proxy_blocks_before_core_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            work_dir = temp_path / "data" / "mihomo"
            runtime.config["client"]["mihomo_work_dir"] = str(work_dir)
            runtime.config["geo_update"] = {"enabled": True}
            runtime.geo_updater = FakeGeoUpdater(work_dir)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text(
                "rules:\n- GEOSITE,category-ads-all,REJECT\n- GEOIP,US,GENERAL,no-resolve\n",
                encoding="utf-8",
            )
            runtime.manager = FakeTier3Manager()
            runtime.write_mihomo_runtime_config = lambda: runtime_config
            runner = FakeLifecycleCoreRunner()
            runtime.core_runner = runner

            with self.assertRaisesRegex(RuntimeError, "explicit egress proxy"):
                runtime._start_core_once()

            self.assertEqual(runtime.geo_updater.update_calls, 0)
            self.assertFalse(runner.started)

    def test_core_startup_timeout_extends_when_geodata_is_still_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_work_dir"] = str(temp_path / "data" / "mihomo")
            runtime.config["geo_update"] = {"enabled": True, "request_timeout": 10}
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text(
                "rules:\n- GEOSITE,category-ads-all,REJECT\n- GEOIP,US,GENERAL,no-resolve\n",
                encoding="utf-8",
            )

            timeout = runtime._core_startup_timeout({"startup_timeout": 5}, runtime_config)

            self.assertGreaterEqual(timeout, 35)

    def test_start_rolls_back_partial_runtime_when_core_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.app_paths.data_dir.mkdir(parents=True, exist_ok=True)
            runtime.config["killswitch"] = {"enabled": False}
            runtime._start_subscription_server = lambda: None
            runtime.recover_stale_system_proxy = lambda: False
            runtime._warn_if_killswitch_unavailable = lambda: None
            runtime.start_core = lambda: (_ for _ in ()).throw(RuntimeError("bootstrap failed"))

            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                runtime.start(start_core=True)

            self.assertFalse(runtime.running)
            self.assertIsNone(runtime.manager)
            self.assertIsNone(runtime.kill_switch)
            self.assertIsNone(runtime.monitor_thread)
            self.assertEqual(runtime.last_error, "bootstrap failed")

    def test_start_dispatches_tier3_expansion_before_slow_primary_checks(self) -> None:
        events: list[tuple[str, float]] = []

        class ExpansionManager:
            def __init__(self) -> None:
                self.runtime_nodes = {"tier1": [], "tier2": [], "tier3": []}
                self.on_tier3_pool_committed = None
                self.tier3_close_stage_observer = None
                self.tier3_runtime_truth_provider = None

            def _tier3_enabled(self) -> bool:
                return True

            def trigger_tier3_collect(self, *, startup_mode: bool = False):
                events.append(("tier3_collect", time.monotonic()))
                return {
                    "accepted": True,
                    "status": "started",
                    "startup_mode": bool(startup_mode),
                }

            def wait_for_tier3_expansion_ready(self, *, timeout_seconds: float):
                self.assert_timeout = timeout_seconds
                events.append(("tier3_ready", time.monotonic()))
                return {
                    "ready": True,
                    "session_started": True,
                    "source_supervisor_ready": True,
                }

            def trigger_tier3a_health_check(self):
                events.append(("tier3a_health", time.monotonic()))
                return {"accepted": True, "status": "started"}

            def mark_tier3_startup_gate_required(self, required: bool) -> None:
                events.append(
                    (
                        "tier3_gate_required" if required else "tier3_gate_clear",
                        time.monotonic(),
                    )
                )

            def bind_runtime_generation(self, _generation, provider) -> None:
                self.tier3_runtime_truth_provider = provider

            def close(self, *, timeout_seconds: float = 3.0) -> str:
                del timeout_seconds
                return "STOPPED"

        class StartKillSwitch:
            def __init__(self, **_kwargs) -> None:
                pass

            def cleanup_stale_rules(self) -> None:
                pass

            def close(self, *, timeout: float = 1.0) -> None:
                del timeout

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime = self._runtime(
                temp_path,
                MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                ),
            )
            runtime.app_paths.data_dir.mkdir(parents=True, exist_ok=True)
            runtime.config["killswitch"] = {"enabled": False}
            manager = ExpansionManager()
            started_at = time.monotonic()
            runtime._new_proxy_tier_manager = lambda: manager
            runtime._attach_manager_runtime_hooks = lambda _manager: events.append(
                ("hooks", time.monotonic())
            )
            runtime._start_subscription_server = lambda: events.append(
                ("subscription", time.monotonic())
            )
            runtime._ensure_bootstrap_core_proxy = lambda: events.append(
                ("bootstrap_core", time.monotonic())
            )
            runtime.recover_stale_system_proxy = lambda: False
            runtime._warn_if_killswitch_unavailable = lambda: None
            runtime._monitor_loop = lambda: runtime.stop_event.wait()

            def slow_primary_start():
                events.append(("primary_checks", time.monotonic()))
                time.sleep(0.6)
                return {"running": False}

            runtime.start_core = slow_primary_start
            runtime.status = lambda: {
                "running": bool(runtime.running),
                "runtime_service_running": bool(runtime.running),
                "core_running": False,
            }
            try:
                with patch("desktop_runtime.KillSwitchManager", StartKillSwitch):
                    runtime.start(start_core=True)

                names = [name for name, _timestamp in events]
                self.assertLess(
                    names.index("tier3_collect"),
                    names.index("tier3_ready"),
                )
                self.assertLess(
                    names.index("tier3_ready"),
                    names.index("bootstrap_core"),
                )
                self.assertLess(names.index("hooks"), names.index("tier3_collect"))
                self.assertLess(
                    names.index("tier3_gate_required"),
                    names.index("tier3_collect"),
                )
                self.assertLess(
                    names.index("tier3_collect"),
                    names.index("primary_checks"),
                )
                collect_at = next(
                    timestamp
                    for name, timestamp in events
                    if name == "tier3_collect"
                )
                self.assertLessEqual(collect_at - started_at, 0.5)
            finally:
                runtime.stop(actor="test", reason="startup ordering test")

    def test_start_repairs_and_flushes_authoritative_monitor_log(self) -> None:
        class LoggingManager:
            def __init__(self, **_kwargs) -> None:
                self.runtime_nodes = {"tier1": [], "tier2": [], "tier3": []}
                self.on_tier3_pool_committed = None
                self.tier3_close_stage_observer = None
                self.tier3_runtime_truth_provider = None

            def bind_runtime_generation(self, _generation, provider) -> None:
                self.tier3_runtime_truth_provider = provider

            def close(self, *, timeout_seconds: float = 3.0) -> str:
                return "STOPPED"

        class LoggingKillSwitch:
            def __init__(self, **_kwargs) -> None:
                pass

            def cleanup_stale_rules(self) -> None:
                pass

            def close(self, *, timeout: float = 1.0) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["killswitch"] = {"enabled": False}
            runtime.recover_stale_system_proxy = lambda: False
            runtime.restore_system_proxy = lambda: False
            runtime._warn_if_killswitch_unavailable = lambda: None
            runtime._start_subscription_server = lambda: None
            runtime._monitor_loop = lambda: runtime.stop_event.wait()
            runtime.status = lambda: {
                "running": bool(runtime.running),
                "runtime_generation": int(runtime._runtime_generation),
            }
            try:
                with (
                    patch("desktop_runtime.ProxyTierManager", LoggingManager),
                    patch("desktop_runtime.KillSwitchManager", LoggingKillSwitch),
                ):
                    status = runtime.start(start_core=False)
                    self.assertTrue(status["running"])

                    deadline = time.monotonic() + 1.0
                    log_text = ""
                    while time.monotonic() < deadline:
                        if runtime.app_paths.log_file.exists():
                            log_text = runtime.app_paths.log_file.read_text(
                                encoding="utf-8"
                            )
                        if "Desktop runtime started in native app mode" in log_text:
                            break
                        time.sleep(0.01)

                    self.assertIn("Desktop runtime start requested", log_text)
                    self.assertIn("Desktop runtime started in native app mode", log_text)
                    self.assertTrue(
                        runtime._ensure_authoritative_monitor_log_handler()["valid"]
                    )

                    stopped = runtime.stop(actor="test", reason="logging audit")
                    self.assertFalse(stopped["running"])
                    flush_logging_handlers(runtime.logger)
                    stopped_size = runtime.app_paths.log_file.stat().st_size
                    time.sleep(0.1)
                    flush_logging_handlers(runtime.logger)
                    self.assertEqual(
                        runtime.app_paths.log_file.stat().st_size,
                        stopped_size,
                    )
            finally:
                for handler in list(runtime.logger.handlers):
                    handler.close()
                    runtime.logger.removeHandler(handler)

    def test_start_keeps_runtime_running_after_recoverable_tier3_core_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.app_paths.data_dir.mkdir(parents=True, exist_ok=True)
            runtime.config["killswitch"] = {"enabled": False}
            runtime._start_subscription_server = lambda: None
            runtime.recover_stale_system_proxy = lambda: False
            runtime._warn_if_killswitch_unavailable = lambda: None
            runtime._monitor_loop = lambda: None
            runtime._tier3_startup_live_collection_reason = lambda _manager: ""
            runtime.start_core = lambda: (_ for _ in ()).throw(
                RecoverableCoreStartError("Could not verify Bastion Layer after core launch")
            )

            status = runtime.start(start_core=True)

            self.assertTrue(status["running"])
            self.assertTrue(status["runtime_service_running"])
            self.assertFalse(status["core_running"])
            self.assertTrue(status["start_deferred"])
            self.assertFalse(status["core"]["running"])
            self.assertTrue(runtime.running)
            self.assertIsNotNone(runtime.manager)
            self.assertIsNotNone(runtime.kill_switch)
            self.assertIsNotNone(runtime.monitor_thread)
            self.assertEqual(runtime.last_error, "Could not verify Bastion Layer after core launch")
            self.assertFalse(runtime.system_proxy.active)

            runtime.stop()

    def test_start_reports_live_tier3_collection_as_deferred_not_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.app_paths.data_dir.mkdir(parents=True, exist_ok=True)
            runtime.config["killswitch"] = {"enabled": False}
            runtime._start_subscription_server = lambda: None
            runtime.recover_stale_system_proxy = lambda: False
            runtime._warn_if_killswitch_unavailable = lambda: None
            runtime._monitor_loop = lambda: None
            runtime.config["paths"] = {
                "tier3_nodes_file": str(temp_path / "nodes.json"),
                "tier_observation_cache_file": str(temp_path / "observations.json"),
                "sources_file": str(temp_path / "sources.yaml"),
            }
            (temp_path / "sources.yaml").write_text(
                (Path(__file__).resolve().parents[1] / "sources.example.yaml").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 3,
                },
            }

            def defer_while_units_are_active():
                report = runtime.manager.tier3_last_collection_report
                self.assertEqual(report["configured_source_count"], 10)
                self.assertEqual(report["source_count"], 10)
                self.assertEqual(report["source_fetch_pending"], 10)
                self.assertEqual(len(report["sources"]), 10)
                raise RecoverableCoreStartError("Bastion Layer startup gate is waiting for 0/3")

            runtime.start_core = defer_while_units_are_active

            status = runtime.start(start_core=True)

            self.assertTrue(status["runtime_service_running"])
            self.assertFalse(status["core_running"])
            self.assertTrue(status["start_deferred"])
            self.assertEqual(status["startup_phase"], "collecting")
            self.assertEqual(status["runtime_generation"], 1)
            self.assertNotEqual(status["start_deferred_reason"], "source_exhausted")
            self.assertIsNone(status["last_error"])
            self.assertIsNone(runtime.last_error)

            runtime.stop()

    def test_stale_runtime_generation_stop_cannot_stop_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.running = True
            runtime._runtime_generation = 7

            with patch.object(runtime, "_stop_locked") as stop_locked:
                status = runtime.stop(
                    actor="user",
                    reason="stale overview stop",
                    expected_generation=6,
                )

            stop_locked.assert_not_called()
            self.assertTrue(runtime.running)
            self.assertTrue(status["stop_skipped"])
            self.assertEqual(status["stop_skipped_reason"], "stale_runtime_generation")
            self.assertEqual(status["runtime_generation"], 7)

    def test_request_stop_is_immediate_idempotent_and_does_not_wait_for_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)

            class CancelManager:
                def __init__(self) -> None:
                    self.request_stop_calls = 0

                def request_stop(self) -> None:
                    self.request_stop_calls += 1

                @staticmethod
                def get_runtime_snapshot(*, include_nodes: bool = True):
                    return None

            class CancelRunner:
                def __init__(self) -> None:
                    self.request_stop_calls = 0

                def request_stop(self) -> None:
                    self.request_stop_calls += 1

                @staticmethod
                def status() -> dict[str, object]:
                    return {"running": True, "pid": 4321}

            manager = CancelManager()
            runner = CancelRunner()
            runtime.manager = manager
            runtime.core_runner = runner
            runtime.running = True
            runtime._runtime_generation = 4
            runtime._tier3_egress_activation_pending = True
            runtime._tier3_pool_sync_pending = True
            runtime._tier3_core_gate_pending = True
            runtime._runtime_lifecycle_lock.acquire()
            try:
                started = time.monotonic()
                first = runtime.request_stop(actor="user", reason="button")
                second = runtime.request_stop(actor="user", reason="button")
                elapsed = time.monotonic() - started
            finally:
                runtime._runtime_lifecycle_lock.release()

            self.assertLess(elapsed, 0.05)
            self.assertTrue(runtime.stop_event.is_set())
            self.assertTrue(first.accepted)
            self.assertEqual(first.intent_id, second.intent_id)
            self.assertEqual(first.lifecycle_revision, second.lifecycle_revision)
            self.assertEqual(manager.request_stop_calls, 1)
            self.assertEqual(runner.request_stop_calls, 1)
            self.assertTrue(
                runtime._exit_quality_static_refresh_cancel_event.is_set()
            )
            self.assertFalse(runtime._tier3_egress_activation_pending)
            self.assertFalse(runtime._tier3_pool_sync_pending)
            self.assertFalse(runtime._tier3_core_gate_pending)
            self.assertEqual(runtime.status()["startup_phase"], "stopping")

    def test_stop_preempts_proxy_and_core_before_lifecycle_lock_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            core_signalled = threading.Event()
            proxy_restored = threading.Event()

            def request_core_stop():
                core_signalled.set()
                return {
                    "requested": True,
                    "signal_sent": True,
                    "still_running": True,
                    "pid": 1234,
                }

            runner.request_stop = request_core_stop
            runtime.core_runner = runner
            runtime.running = True
            runtime._runtime_generation = 3
            runtime.restore_system_proxy = lambda: proxy_restored.set()
            results: list[dict[str, object]] = []
            runtime._runtime_lifecycle_lock.acquire()
            stop_thread = threading.Thread(
                target=lambda: results.append(
                    runtime.stop(actor="user", reason="preempt start")
                )
            )
            try:
                stop_thread.start()
                self.assertTrue(proxy_restored.wait(0.25))
                self.assertTrue(core_signalled.wait(0.25))
                self.assertTrue(stop_thread.is_alive())
            finally:
                runtime._runtime_lifecycle_lock.release()
            stop_thread.join(timeout=2.0)

            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(results)
            self.assertEqual(results[0]["startup_phase"], "stopped")
            self.assertTrue(runner.stopped)

    def test_stop_intent_cancels_start_before_manager_and_core_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            entered_recovery = threading.Event()
            release_recovery = threading.Event()
            results: list[dict[str, object]] = []

            def blocked_recovery() -> bool:
                entered_recovery.set()
                release_recovery.wait(timeout=2.0)
                return False

            runtime.recover_stale_system_proxy = blocked_recovery
            start_thread = threading.Thread(
                target=lambda: results.append(runtime.start(start_core=False))
            )
            start_thread.start()
            self.assertTrue(entered_recovery.wait(1.0))

            intent = runtime.request_stop(actor="user", reason="cancel startup")
            release_recovery.set()
            start_thread.join(timeout=2.0)

            self.assertFalse(start_thread.is_alive())
            self.assertTrue(intent.accepted)
            self.assertTrue(runtime.stop_event.is_set())
            self.assertFalse(runtime.running)
            self.assertIsNone(runtime.manager)
            self.assertIsNone(runtime.core_runner)
            self.assertEqual(results[0]["startup_phase"], "stopping")
            for handler in list(runtime.logger.handlers):
                handler.close()
                runtime.logger.removeHandler(handler)

    def test_runtime_stop_records_actor_reason_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.running = True
            runtime._runtime_generation = 4

            def fake_stop_locked(*, actor: str, reason: str):
                self.assertEqual(actor, "app_exit")
                self.assertEqual(reason, "application exit")
                runtime.running = False
                return {}

            with patch.object(runtime, "_stop_locked", side_effect=fake_stop_locked):
                status = runtime.stop(
                    actor="app_exit",
                    reason="application exit",
                    expected_generation=4,
                )

            self.assertFalse(status["stop_skipped"])
            self.assertEqual(status["runtime_generation"], 4)
            self.assertEqual(status["last_runtime_stop"]["actor"], "app_exit")
            self.assertEqual(status["last_runtime_stop"]["reason"], "application exit")
            self.assertEqual(status["last_runtime_stop"]["generation"], 4)
            self.assertEqual(status["last_runtime_stop"]["result"], "stopped")
            journal = json.loads(
                runtime._runtime_lifecycle_journal_path.read_text(encoding="utf-8")
            )
            self.assertEqual(journal["event"], "stopped")
            self.assertEqual(journal["actor"], "app_exit")
            self.assertEqual(journal["reason"], "application exit")
            self.assertEqual(journal["runtime_generation"], 4)

    def test_runtime_stop_does_not_wait_for_logging_handler_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.running = True
            runtime._runtime_generation = 5
            handler = logging.NullHandler()
            handler.createLock()
            runtime.logger.addHandler(handler)
            lock_held = threading.Event()
            release_lock = threading.Event()

            def hold_handler_lock() -> None:
                handler.acquire()
                try:
                    lock_held.set()
                    release_lock.wait(3.0)
                finally:
                    handler.release()

            def fake_stop_locked(*, actor: str, reason: str):
                runtime.running = False
                return {"validator_stop_status": "STOPPED"}

            lock_thread = threading.Thread(target=hold_handler_lock)
            lock_thread.start()
            self.assertTrue(lock_held.wait(1.0))
            results: list[dict[str, Any]] = []
            stop_thread = threading.Thread(
                target=lambda: results.append(
                    runtime.stop(
                        actor="user",
                        reason="bounded logging regression",
                        expected_generation=5,
                    )
                )
            )
            try:
                with patch.object(runtime, "_stop_locked", side_effect=fake_stop_locked):
                    stop_thread.start()
                    stop_thread.join(timeout=1.0)
                    blocked = stop_thread.is_alive()
                    release_lock.set()
                    stop_thread.join(timeout=2.0)
                self.assertFalse(blocked)
                self.assertEqual(results[0]["validator_stop_status"], "STOPPED")
                self.assertEqual(results[0]["last_runtime_stop"]["actor"], "user")
                self.assertEqual(
                    results[0]["last_runtime_stop"]["reason"],
                    "bounded logging regression",
                )
            finally:
                release_lock.set()
                lock_thread.join(timeout=2.0)
                runtime.logger.removeHandler(handler)

    def test_stop_progress_journal_does_not_wait_for_journal_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.running = True
            runtime._runtime_lifecycle_journal_lock.acquire()
            try:
                started = time.monotonic()
                runtime._write_runtime_lifecycle_journal(
                    "stop_progress",
                    nonblocking=True,
                    core_process_running=True,
                    stop_stage="bounded-test",
                )
                elapsed = time.monotonic() - started
            finally:
                runtime._runtime_lifecycle_journal_lock.release()

            self.assertLess(elapsed, 0.05)

    def test_app_exit_propagates_validator_quarantine_fail_fast_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)

            class QuarantinedManager:
                close_calls = 0

                @staticmethod
                def get_runtime_snapshot(*, include_nodes: bool = True):
                    return None

                def close(self) -> str:
                    self.close_calls += 1
                    return "STOP_FAILED_QUARANTINED"

            manager = QuarantinedManager()
            runtime.manager = manager
            runtime.running = False
            runtime._runtime_generation = 9
            runtime._detach_manager_runtime_hooks = lambda _manager: None
            runtime.restore_system_proxy = lambda: None
            runtime.stop_core = lambda **_kwargs: None

            result = runtime.stop(
                actor="app_exit",
                reason="application exit",
                expected_generation=9,
            )

            self.assertEqual(result["validator_stop_status"], "STOP_FAILED_QUARANTINED")
            self.assertEqual(result["shutdown_exit_code"], 74)
            self.assertEqual(result["startup_phase"], "failed")
            self.assertEqual(
                result["stop_error_code"],
                "tier3_validation_workers_active",
            )
            self.assertEqual(
                result["last_runtime_stop"]["result"],
                "failed",
            )
            self.assertIs(runtime.manager, manager)
            self.assertFalse(runtime.running)
            self.assertEqual(manager.close_calls, 1)

    def test_runtime_does_not_report_stopped_with_active_validation_reporter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            reporter_drained = threading.Event()

            class ResultReporterManager:
                def __init__(self) -> None:
                    self.close_calls = 0
                    self.tier3_close_stage_observer = None

                @staticmethod
                def get_runtime_snapshot(*, include_nodes: bool = True):
                    return None

                def close(self, *, timeout_seconds: float) -> str:
                    del timeout_seconds
                    self.close_calls += 1
                    if reporter_drained.is_set():
                        return "STOPPED"
                    return "STOP_FAILED_RESULT_REPORTER_ACTIVE"

            manager = ResultReporterManager()
            runtime.manager = manager
            runtime.running = True
            runtime._runtime_generation = 12
            runtime._detach_manager_runtime_hooks = lambda _manager: None
            runtime.restore_system_proxy = lambda: None
            runtime.stop_core = lambda **_kwargs: {
                "running": False,
                "stopped": True,
                "pid": None,
            }

            failed = runtime.stop(
                actor="user",
                reason="result reporter still active",
                expected_generation=12,
            )

            self.assertEqual(
                failed["validator_stop_status"],
                "STOP_FAILED_RESULT_REPORTER_ACTIVE",
            )
            self.assertEqual(failed["startup_phase"], "failed")
            self.assertEqual(
                failed["stop_error_code"],
                "tier3_validation_workers_active",
            )
            self.assertTrue(failed["shutdown_cleanup_pending"])
            self.assertIs(runtime.manager, manager)
            self.assertTrue(runtime.running)

            reporter_drained.set()
            completed = runtime.stop(
                actor="user",
                reason="result reporter drained",
                expected_generation=12,
            )

            self.assertEqual(completed["validator_stop_status"], "STOPPED")
            self.assertEqual(completed["startup_phase"], "stopped")
            self.assertIsNone(runtime.manager)
            self.assertFalse(runtime.running)
            self.assertEqual(manager.close_calls, 2)

    def test_stream_active_stop_stays_stopping_until_same_generation_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            done = threading.Event()

            class StreamActiveManager:
                def __init__(self) -> None:
                    self.close_calls = 0
                    self.tier3_close_stage_observer = None
                    self._tier3_active_stream_lock = threading.Lock()
                    self._tier3_active_stream_sessions = {
                        threading.Event(): done
                    }

                @staticmethod
                def get_runtime_snapshot(*, include_nodes: bool = True):
                    return None

                def close(self, *, timeout_seconds: float) -> str:
                    self.close_calls += 1
                    if not done.is_set():
                        return "STOP_FAILED_STREAM_ACTIVE"
                    with self._tier3_active_stream_lock:
                        self._tier3_active_stream_sessions.clear()
                    return "STOPPED"

            manager = StreamActiveManager()
            runtime.manager = manager
            runtime.running = True
            runtime._runtime_generation = 11
            runtime._detach_manager_runtime_hooks = lambda _manager: None
            runtime.restore_system_proxy = lambda: None
            runtime.stop_core = lambda **_kwargs: None

            result = runtime.stop(
                actor="user",
                reason="stop truth regression",
                expected_generation=11,
            )

            self.assertEqual(
                result["validator_stop_status"],
                "STOP_FAILED_STREAM_ACTIVE",
            )
            self.assertEqual(result["startup_phase"], "stopping")
            self.assertEqual(result["last_runtime_stop"]["result"], "stopping")
            self.assertIs(runtime.manager, manager)
            self.assertTrue(runtime.running)

            done.set()
            deadline = time.monotonic() + 2.0
            final_status = runtime.status()
            while (
                (
                    runtime.manager is not None
                    or final_status["startup_phase"] != "stopped"
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
                final_status = runtime.status()
            self.assertIsNone(runtime.manager)
            self.assertFalse(runtime.running)
            self.assertGreaterEqual(manager.close_calls, 2)
            self.assertEqual(final_status["startup_phase"], "stopped")
            completion_thread = runtime._runtime_stop_completion_thread
            if completion_thread is not None:
                completion_thread.join(timeout=2.0)
                self.assertFalse(
                    completion_thread.is_alive(),
                    "deferred runtime stop did not finish its durable log cleanup",
                )

    def test_stop_tears_down_proxy_and_core_before_waiting_for_manager(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            order: list[str] = []

            class BlockingManager:
                tier3_close_stage_observer = None

                def request_stop(self) -> bool:
                    order.append("manager_request_stop")
                    return True

                def mark_tier3_core_stopped(self, _reason: str) -> None:
                    order.append("manager_core_mark")

                def close(self, *, timeout_seconds: float) -> str:
                    order.append("manager_close")
                    self.assert_core_stopped()
                    return "STOPPED"

                @staticmethod
                def assert_core_stopped() -> None:
                    if not runtime.core_runner or runtime.core_runner.is_running():
                        raise AssertionError("manager cleanup started before Core stopped")

            manager = BlockingManager()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.running = True
            runtime._runtime_generation = 10
            runtime._detach_manager_runtime_hooks = lambda _manager: None
            runtime.restore_system_proxy = lambda: order.append("system_proxy")
            original_core_stop = runtime.core_runner.stop

            def stop_runner():
                order.append("core")
                return original_core_stop()

            runtime.core_runner.stop = stop_runner

            result = runtime.stop(
                actor="user",
                reason="lifecycle ordering regression",
                expected_generation=10,
            )

            self.assertEqual(result["validator_stop_status"], "STOPPED")
            self.assertLess(
                order.index("manager_request_stop"),
                order.index("system_proxy"),
            )
            self.assertLess(order.index("system_proxy"), order.index("core"))
            self.assertLess(order.index("core"), order.index("manager_close"))

    def test_runtime_does_not_report_stopped_while_core_process_is_still_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)

            class StubbornRunner:
                def __init__(self) -> None:
                    self.requested = 0

                @staticmethod
                def is_running() -> bool:
                    return True

                @staticmethod
                def status() -> dict[str, object]:
                    return {
                        "running": True,
                        "process_running": True,
                        "proxy_ready": False,
                        "pid": 4321,
                    }

                def request_stop(self) -> dict[str, object]:
                    self.requested += 1
                    return {
                        "requested": True,
                        "signal_sent": self.requested == 1,
                        "still_running": True,
                        "pid": 4321,
                    }

                @staticmethod
                def stop(*, timeout: float) -> dict[str, object]:
                    return {
                        "running": True,
                        "process_running": True,
                        "stopped": False,
                        "still_running": True,
                        "pid": 4321,
                    }

            class ClosedManager:
                tier3_close_stage_observer = None

                @staticmethod
                def request_stop() -> bool:
                    return True

                @staticmethod
                def close(*, timeout_seconds: float) -> str:
                    return "STOPPED"

                @staticmethod
                def mark_tier3_core_stopped(_reason: str) -> None:
                    raise AssertionError("active core must not be marked stopped")

            runner = StubbornRunner()
            runtime.core_runner = runner
            runtime.manager = ClosedManager()
            runtime.running = False
            runtime._runtime_generation = 6
            runtime.restore_system_proxy = lambda: None

            result = runtime.stop(
                actor="user",
                reason="stubborn core",
                expected_generation=6,
            )

            self.assertEqual(result["core_stop_status"], "STOP_FAILED_CORE_ACTIVE")
            self.assertEqual(result["startup_phase"], "failed")
            self.assertTrue(result["core"]["running"])
            self.assertEqual(
                result["stop_error_code"],
                "mihomo_process_active",
            )
            self.assertEqual(result["stop_error_details"]["pid"], 4321)
            self.assertFalse(runtime.running)
            self.assertIs(runtime.core_runner, runner)
            self.assertEqual(runtime._last_runtime_stop["result"], "failed")
            self.assertIn("4321", str(result["last_error"]))

            blocked_start = runtime.start(start_core=False)
            self.assertTrue(blocked_start["start_blocked_by_cleanup"])
            self.assertEqual(
                blocked_start["start_blocked_reason"],
                "shutdown_cleanup_pending",
            )
            self.assertIn(
                "mihomo_core",
                blocked_start["start_blocked_owners"],
            )
            self.assertIs(runtime.core_runner, runner)

    def test_runtime_does_not_report_stopped_while_monitor_thread_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)

            class ClosedManager:
                tier3_close_stage_observer = None

                @staticmethod
                def request_stop() -> bool:
                    return True

                @staticmethod
                def close(*, timeout_seconds: float) -> str:
                    return "STOPPED"

                @staticmethod
                def get_runtime_snapshot(*, include_nodes: bool = True):
                    return None

            class StuckMonitor:
                def __init__(self) -> None:
                    self.join_timeouts: list[float] = []

                @staticmethod
                def is_alive() -> bool:
                    return True

                def join(self, timeout=None) -> None:
                    self.join_timeouts.append(float(timeout or 0.0))

            monitor = StuckMonitor()
            manager = ClosedManager()
            runtime.manager = manager
            runtime.monitor_thread = monitor
            runtime.running = True
            runtime._runtime_generation = 7
            runtime.restore_system_proxy = lambda: None
            runtime._detach_manager_runtime_hooks = lambda _manager: None

            result = runtime.stop(
                actor="user",
                reason="stuck monitor",
                expected_generation=7,
            )

            self.assertEqual(
                result["runtime_stop_status"],
                "STOP_FAILED_MONITOR_ACTIVE",
            )
            self.assertEqual(result["startup_phase"], "failed")
            self.assertEqual(result["last_runtime_stop"]["result"], "failed")
            self.assertEqual(
                result["stop_error_code"],
                "runtime_monitor_active",
            )
            self.assertIs(runtime.monitor_thread, monitor)
            self.assertIs(runtime.manager, manager)
            self.assertTrue(runtime.running)
            self.assertEqual(len(monitor.join_timeouts), 1)

    def test_runtime_does_not_report_stopped_while_exit_quality_refresh_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            started = threading.Event()
            release = threading.Event()
            cancellation_seen = threading.Event()
            refresh_proxy_urls: list[str | None] = []

            class BlockingExitQualityService:
                @staticmethod
                def refresh_static_sources(
                    *,
                    proxy_url=None,
                    cancel_event=None,
                ) -> dict[str, object]:
                    refresh_proxy_urls.append(proxy_url)
                    started.set()
                    while not release.wait(0.01):
                        if cancel_event is not None and cancel_event.is_set():
                            cancellation_seen.set()
                    return {}

                @staticmethod
                def recent_results() -> list[dict[str, object]]:
                    return []

            runtime.exit_quality_service = BlockingExitQualityService()
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.manager = type(
                "VerifiedEgressManager",
                (),
                {"effective_egress_ok": True},
            )()
            runtime._set_core_runtime_phase("formal_capture")
            runtime._set_effective_egress_state(True, "")
            runtime.running = False
            runtime._runtime_generation = 8
            runtime.restore_system_proxy = lambda: None
            runtime.stop_core = lambda **_kwargs: {
                "running": False,
                "stopped": True,
                "pid": None,
            }

            self.assertTrue(
                runtime.refresh_exit_quality_static_data_async(force=True)
            )
            self.assertTrue(started.wait(0.5))
            self.assertEqual(refresh_proxy_urls, ["http://127.0.0.1:7890"])
            runtime.manager = None
            worker = runtime._exit_quality_static_refresh_thread
            self.assertIsNotNone(worker)

            try:
                result = runtime.stop(
                    actor="user",
                    reason="blocking exit-quality refresh",
                    expected_generation=8,
                )

                self.assertTrue(cancellation_seen.is_set())
                self.assertEqual(
                    result["runtime_stop_status"],
                    "STOP_FAILED_EXIT_QUALITY_ACTIVE",
                )
                self.assertEqual(result["startup_phase"], "failed")
                self.assertEqual(
                    result["stop_error_code"],
                    "exit_quality_refresh_active",
                )
                self.assertTrue(result["shutdown_cleanup_pending"])
                self.assertTrue(result["exit_quality_refresh_active"])
                self.assertIs(
                    runtime._exit_quality_static_refresh_thread,
                    worker,
                )
                self.assertTrue(worker.is_alive())

                blocked_start = runtime.start(start_core=False)
                self.assertTrue(
                    blocked_start["start_blocked_by_cleanup"]
                )
                self.assertIn(
                    "exit_quality_refresh",
                    blocked_start["start_blocked_owners"],
                )
                self.assertTrue(
                    runtime._exit_quality_static_refresh_cancel_event.is_set()
                )
            finally:
                release.set()
                if worker is not None:
                    worker.join(timeout=1.0)

            completed = runtime.stop(
                actor="user",
                reason="exit-quality refresh drained",
                expected_generation=8,
            )
            self.assertEqual(completed["startup_phase"], "stopped")
            self.assertFalse(completed["exit_quality_refresh_active"])

    def test_runtime_stop_fails_truthfully_while_egress_activation_worker_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            started = threading.Event()
            release = threading.Event()

            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime.system_proxy.enabled = False
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime.running = False
            runtime._runtime_generation = 12
            runtime.restore_system_proxy = lambda: None

            def blocking_verification() -> dict[str, object]:
                started.set()
                release.wait(timeout=3.0)
                return {"status": 204}

            runtime._verify_runtime_final_exit = (
                lambda **_kwargs: blocking_verification()
            )
            runtime._queue_tier3_egress_activation()
            self.assertTrue(started.wait(0.5))
            worker = runtime._tier3_egress_activation_thread
            self.assertIsNotNone(worker)

            try:
                result = runtime.stop(
                    actor="user",
                    reason="blocking final-exit verification",
                    expected_generation=12,
                )

                self.assertEqual(
                    result["runtime_stop_status"],
                    "STOP_FAILED_BACKGROUND_ACTIVE",
                )
                self.assertEqual(result["core_stop_status"], "STOPPED")
                self.assertEqual(result["startup_phase"], "failed")
                self.assertEqual(
                    result["stop_error_code"],
                    "runtime_background_workers_active",
                )
                self.assertTrue(
                    result["runtime_background_activity"][
                        "tier3_egress_activation"
                    ]
                )
                self.assertIs(runtime._tier3_egress_activation_thread, worker)
                self.assertTrue(worker.is_alive())
                self.assertFalse(runner.is_running())
            finally:
                release.set()
                if worker is not None:
                    worker.join(timeout=1.0)

            completed = runtime.stop(
                actor="user",
                reason="egress activation drained",
                expected_generation=12,
            )
            self.assertEqual(completed["startup_phase"], "stopped")
            self.assertFalse(
                completed["runtime_background_active"]
            )

    def test_monitor_cycle_preserves_deferred_start_error_while_core_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.running = True
            runtime.core_runner = FakeCoreRunner(running=False, port_ok=False)
            runtime.last_error = "Could not verify Bastion Layer after core launch"

            runtime._clear_last_error_if_runtime_healthy()

            self.assertEqual(runtime.last_error, "Could not verify Bastion Layer after core launch")

            runtime.core_runner = FakeCoreRunner(running=True, port_ok=True)
            runtime._clear_last_error_if_runtime_healthy()

            self.assertIsNone(runtime.last_error)

    def test_tier3_pool_change_hot_reloads_running_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            calls: list[str] = []
            runtime.reload_core_config = lambda **kwargs: calls.append(str(kwargs.get("mode"))) or {"reloaded": True}

            manager.refresh_tier3()
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["incremental_export"])
            self.assertEqual(runtime._last_tier3_core_sync_marker, runtime._tier3_core_sync_marker())

    def test_private_core_export_filters_static_tiers_but_drains_tier3b_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes = {
                "tier1": [
                    Node(name="tier1-alive", protocol="ss", address="tier1-alive.example", port=8388, tier="tier1", status=NodeStatus.ALIVE, method="aes-256-gcm", password="pass"),
                    Node(name="tier1-dead", protocol="ss", address="tier1-dead.example", port=8389, tier="tier1", status=NodeStatus.DEAD, method="aes-256-gcm", password="pass"),
                ],
                "tier2": [
                    Node(name="tier2-slow", protocol="ss", address="tier2-slow.example", port=8390, tier="tier2", status=NodeStatus.SLOW, method="aes-256-gcm", password="pass"),
                    Node(name="tier2-unknown", protocol="ss", address="tier2-unknown.example", port=8391, tier="tier2", status=NodeStatus.UNKNOWN, method="aes-256-gcm", password="pass"),
                ],
                "tier3": [
                    Node(name="tier3-alive", protocol="ss", address="tier3-alive.example", port=8392, tier="tier3", status=NodeStatus.ALIVE, method="aes-256-gcm", password="pass"),
                    Node(name="tier3-suspicious", protocol="ss", address="tier3-suspicious.example", port=8393, tier="tier3", status=NodeStatus.SUSPICIOUS, method="aes-256-gcm", password="pass"),
                ],
                "exit": [],
            }
            runtime.manager = manager

            self.assertEqual(
                [node.name for node in manager.get_nodes_for_subscription("tier1")],
                ["tier1-alive", "tier1-dead"],
            )
            self.assertEqual(
                [node.name for node in manager.get_nodes_for_subscription("tier3")],
                ["tier3-alive", "tier3-suspicious"],
            )

            core_view = runtime._manager_for_mihomo_runtime_config()
            self.assertEqual(
                [node.name for node in core_view.get_nodes_for_subscription("tier1")],
                ["tier1-alive"],
            )
            self.assertEqual(
                [node.name for node in core_view.get_nodes_for_subscription("tier2")],
                ["tier2-slow"],
            )
            self.assertEqual(
                [node.name for node in core_view.get_nodes_for_subscription("tier3")],
                ["tier3-alive", "tier3-suspicious"],
            )
            manager.runtime_nodes["tier3"][0].status = NodeStatus.DEAD
            self.assertEqual(
                [node.name for node in core_view.get_nodes_for_subscription("tier3")],
                ["tier3-alive", "tier3-suspicious"],
            )
            self.assertEqual(
                [node.name for node in manager.runtime_nodes["tier3"]],
                ["tier3-alive", "tier3-suspicious"],
            )

            marker_before = runtime._core_export_sync_marker()
            runtime.running = True
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_core_export_sync_marker = marker_before
            reload_modes: list[str] = []
            runtime.reload_core_config = lambda **kwargs: (
                reload_modes.append(str(kwargs.get("mode")))
                or {"reloaded": True, "mode": "incremental_export"}
            )
            manager.runtime_nodes["tier1"][1].status = NodeStatus.ALIVE
            marker_after = runtime._core_export_sync_marker()
            runtime._sync_core_config_after_pool_update()

            self.assertNotEqual(marker_before, marker_after)
            self.assertEqual(reload_modes, ["incremental_export"])
            self.assertEqual(
                [node.name for node in core_view.get_nodes_for_subscription("tier1")],
                ["tier1-alive"],
            )

    def test_manual_proxy_selection_invalidates_verified_route_and_queues_identity_recheck(self) -> None:
        class EffectiveStateManager:
            def __init__(self) -> None:
                self.effective_egress_ok = True
                self.states: list[tuple[object, str]] = []

            def set_effective_egress_state(self, ok, reason: str = "") -> None:
                self.effective_egress_ok = ok
                self.states.append((ok, reason))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = EffectiveStateManager()
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient([{"proxies": {}}])
            with runtime._exit_quality_route_lock:
                runtime._exit_quality_route_cache = {"binding_marker": "stale"}
                runtime._exit_quality_route_cache_at = time.monotonic()
            queued_rechecks: list[str] = []
            runtime._queue_incremental_egress_recheck = lambda: queued_rechecks.append("identity")

            result = runtime.select_mihomo_proxy("FINAL-EXIT", "PROXY-FALLBACK")

            self.assertEqual(result["selected"], "PROXY-FALLBACK")
            self.assertEqual(runtime._exit_quality_route_cache, {})
            self.assertIsNone(manager.effective_egress_ok)
            self.assertIsNone(manager.states[-1][0])
            self.assertIn("pending", manager.states[-1][1])
            self.assertEqual(queued_rechecks, ["identity"])

    def test_incremental_reload_invalidates_route_and_coalesces_final_exit_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="incremental-egress",
                    protocol="ss",
                    uri="ss://incremental-egress",
                    address="incremental-egress.example.com",
                    port=8388,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )
            ]
            runtime.manager = manager
            manager.effective_egress_ok = True
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._core_generation = 1
            runtime._runtime_initial_activation_generation = 1
            runtime._runtime_egress_readiness = RuntimeEgressReadiness(
                state=RuntimeEgressReadinessState.READY,
                validation_active=False,
            )
            # Exercise revalidation coalescing in a lifecycle-consistent
            # no-capture runtime.  A formal-capture runtime without selected-
            # leaf qualification must revoke capture and upgrade the follow-up
            # request to a full activation; that is a different safety path.
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime._runtime_traffic_capture_wanted = lambda: False
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            runtime.write_mihomo_no_capture_runtime_config = (
                lambda: temp_path / "mihomo-runtime.yaml"
            )
            with runtime._exit_quality_route_lock:
                runtime._exit_quality_route_cache = {"binding_marker": "stale"}
                runtime._exit_quality_route_cache_at = time.monotonic()

            first_probe_started = threading.Event()
            release_first_probe = threading.Event()
            probe_calls: list[str] = []
            probe_lock = threading.Lock()

            def probe_final_exit(**_kwargs):
                with probe_lock:
                    probe_calls.append("probe")
                    call_number = len(probe_calls)
                if call_number == 1:
                    first_probe_started.set()
                    release_first_probe.wait(timeout=3)
                return {"delay": 12, "url": "https://cp.cloudflare.com"}

            runtime._verify_runtime_final_exit = probe_final_exit
            def refresh_fixture_qualification():
                runtime._tier3_runtime_controller_snapshot = (
                    int(runtime._tier3_runtime_config_generation),
                    str(runtime._tier3_runtime_config_digest),
                    {"proxies": {}},
                )
                return Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=1,
                    exported_count=1,
                    qualified_count=1,
                    qualified_identities=("fixture",),
                    final_exit_ok=False,
                    traffic_capture_active=True,
                    observed_at_monotonic=time.monotonic(),
                )
            runtime._refresh_tier3_runtime_qualification = (
                refresh_fixture_qualification
            )

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertTrue(result["reloaded"])
            self.assertEqual(runtime._exit_quality_route_cache, {})
            self.assertTrue(manager.effective_states)
            self.assertIsNone(manager.effective_states[-1][0])
            self.assertIn("pending", manager.effective_states[-1][1])
            # A committed provider/config generation is an explicit new
            # evidence event, so it may start one bounded admission proof.
            # It must not, however, fan repeated notifications out into
            # parallel or periodic verifiers.
            self.assertTrue(first_probe_started.wait(timeout=1.0))

            for _ in range(4):
                runtime._queue_incremental_egress_recheck()
            release_first_probe.set()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            # The provider event owns exactly one event-driven proof; later
            # notifications coalesce into that transaction and do not create
            # a periodic verification loop.
            self.assertEqual(probe_calls, ["probe"])
            self.assertTrue(manager.effective_states[-1][0])

    def test_append_only_reload_does_not_recheck_qualified_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="incremental-failure",
                    protocol="ss",
                    uri="ss://incremental-failure",
                    address="incremental-failure.example.com",
                    port=8388,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )
            ]
            runtime.manager = manager
            manager.tier3_core_running = True
            manager.tier3_traffic_capture_active = True
            manager.effective_egress_ok = True
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._core_generation = 1
            runtime._runtime_initial_activation_generation = 1
            runtime._runtime_egress_readiness = RuntimeEgressReadiness(
                state=RuntimeEgressReadinessState.READY,
                validation_active=False,
            )
            runtime._set_core_runtime_phase("formal_capture")
            runtime._incremental_egress_recheck_retry_delay_seconds = 0.0
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            probe_calls: list[str] = []
            runtime._probe_mihomo_mixed_port = lambda: (
                probe_calls.append("probe")
                or (_ for _ in ()).throw(RuntimeError("neutral 204 failed"))
            )
            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=1,
                    exported_count=1,
                    qualified_count=1,
                    qualified_identities=("fixture",),
                    final_exit_ok=False,
                    traffic_capture_active=True,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            runtime._tier3_runtime_final_exit_ok = True
            runtime._tier3_selected_leaf_qualification_evidence = MagicMock(
                return_value=(
                    "incremental-failure",
                    "tier3|ss|incremental-failure.example.com|8388|pass||||||",
                    ("incremental-failure", "stable-signature"),
                )
            )

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )
            self.assertTrue(result["reloaded"])
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            self.assertTrue(manager.effective_states[-1][0])
            self.assertTrue(manager.tier3_traffic_capture_active)
            self.assertEqual(manager.effective_states[-1][1], "")
            self.assertEqual(probe_calls, [])

    def test_formal_capture_activation_request_is_downgraded_to_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="capture-stable",
                    protocol="ss",
                    uri="ss://capture-stable",
                    address="capture-stable.example.com",
                    port=8388,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )
            ]
            manager.tier3_traffic_capture_active = True
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runner.status_payload["pid"] = 4242
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")
            runtime._core_generation = 7
            monitor = FakeTrafficMonitor()
            monitor.started = 1
            runtime.traffic_monitor = monitor
            probes: list[str] = []
            verified_snapshots: list[CoreLoadedRouteSnapshot | None] = []
            identities: list[str] = []
            runtime._probe_mihomo_mixed_port = (
                lambda: probes.append("204") or {"status": 204}
            )

            def verify_runtime_final_exit(**kwargs):
                verified_snapshots.append(kwargs.get("expected_snapshot"))
                probes.append("204")
                return {"status": 204}

            runtime._verify_runtime_final_exit = verify_runtime_final_exit
            runtime._verify_final_exit_identity = (
                lambda **_kwargs: identities.append("identity") or {"state": "complete"}
            )
            runtime._hot_reload_runtime_config_phase = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("incremental recheck must not reload capture state")
                )
            )
            runtime._apply_system_proxy_capture_after_core_start = lambda *_args: (
                (_ for _ in ()).throw(
                    AssertionError("incremental recheck must not apply the system proxy")
                )
            )

            runtime._queue_tier3_egress_activation()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            # The rollback profile has no steady-state automatic verifier.
            # A live capture route is left untouched until an explicit user
            # validation or a new startup admission owns the operation.
            self.assertEqual(probes, [])
            self.assertEqual(verified_snapshots, [])
            # Startup proves connectivity only. Exit identity is a manual,
            # read-only user action and must not be launched by activation.
            self.assertEqual(identities, [])
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertTrue(runner.is_running())
            self.assertEqual(runner.status()["pid"], 4242)
            self.assertEqual(runtime._core_generation, 7)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_capture")
            self.assertTrue(manager.tier3_traffic_capture_active)
            self.assertEqual((monitor.started, monitor.stopped), (1, 0))
            self.assertIn(
                "requested=activation mode=incremental",
                manager.tier3_last_collection_report[
                    "core_revalidation_mode_correction"
                ],
            )

    def test_formal_no_capture_without_capture_request_verifies_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.tier3_startup_gate_satisfied = True
            runtime.manager = manager
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = False
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._set_core_runtime_phase("formal_no_capture")
            self._install_injected_runtime_authority(runtime)
            probes: list[str] = []
            runtime._verify_tier3_final_exit_for_activation = (
                lambda **_kwargs: probes.append("204")
            )
            runtime._probe_mihomo_mixed_port = lambda: probes.append("204")
            runtime._verify_runtime_final_exit = (
                lambda **_kwargs: probes.append("204") or {"status": 204}
            )
            runtime._verify_final_exit_identity = lambda **_kwargs: {
                "state": "complete"
            }
            runtime._hot_reload_runtime_config_phase = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("no-capture verification must not load capture config")
                )
            )
            runtime._apply_system_proxy_capture_after_core_start = lambda *_args: (
                (_ for _ in ()).throw(
                    AssertionError("no-capture verification must not change system proxy")
                )
            )

            runtime._queue_tier3_egress_activation()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            # The rollback profile performs one activation contract. It does
            # not run the later qualification/rebaseline duplicate.
            self.assertEqual(probes, ["204"])
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertTrue(manager.effective_states[-1][0])

            queued: list[str] = []
            runtime._queue_tier3_egress_activation = lambda: queued.append("activation")
            runtime._tier3_runtime_final_exit_ok = True
            manager.effective_egress_ok = True
            runtime._tier3_runtime_qualification_retry_at = 0.0
            runtime._maybe_retry_tier3_standby_activation()
            self.assertEqual(queued, [])

    def test_formal_no_capture_initial_activation_recovers_same_incident_before_ready(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.tier3_startup_gate_satisfied = True
            runtime.manager = manager
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = False
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._set_core_runtime_phase("formal_no_capture")
            self._install_injected_runtime_authority(runtime)
            snapshot_a, payload_a, binding_a = (
                runtime._refresh_loaded_runtime_authority()
            )

            ref_b = RuntimeNodeRef(
                tier="tier3",
                pool_kind="tier3b",
                identity="recovered-b-id",
                proxy_name="recovered-b",
            )
            payload_b = json.loads(
                json.dumps(payload_a).replace(
                    binding_a.node.proxy_name,
                    ref_b.proxy_name,
                )
            )
            proxy_map = payload_b.get("proxy_map")
            if isinstance(proxy_map, dict):
                proxy_map[ref_b.proxy_name] = {
                    "tier": ref_b.tier,
                    "pool_kind": ref_b.pool_kind,
                    "identity": ref_b.identity,
                }
            snapshot_b = CoreLoadedRouteSnapshot(
                process_pid=snapshot_a.process_pid,
                process_generation=snapshot_a.process_generation,
                core_generation=snapshot_a.core_generation,
                semantic_digest=snapshot_a.semantic_digest,
                route_epoch=snapshot_a.route_epoch + 1,
                network_context_epoch=snapshot_a.network_context_epoch,
                group_graph=tuple(
                    (
                        group,
                        tuple(
                            ref_b.proxy_name
                            if member == binding_a.node.proxy_name
                            else member
                            for member in members
                        ),
                    )
                    for group, members in snapshot_a.group_graph
                ),
                group_states=tuple(
                    CoreLoadedGroupState(
                        name=state.name,
                        type_name=state.type_name,
                        members=tuple(
                            ref_b.proxy_name
                            if member == binding_a.node.proxy_name
                            else member
                            for member in state.members
                        ),
                        now=(
                            ref_b.proxy_name
                            if state.now == binding_a.node.proxy_name
                            else state.now
                        ),
                    )
                    for state in snapshot_a.group_states
                ),
                proxy_nodes=(ref_b,),
                loaded_at_monotonic=time.monotonic(),
            )
            binding_b = runtime._runtime_route_binding_for_outbound(
                snapshot_b,
                payload_b,
                "FINAL-EXIT",
            )
            self.assertIsNotNone(binding_b)

            def publish_b(**_kwargs):
                with runtime._runtime_route_snapshot_lock:
                    runtime._runtime_route_epoch = snapshot_b.route_epoch
                    runtime._runtime_route_snapshot = snapshot_b
                    runtime._runtime_route_snapshot_payload = payload_b
                runtime._runtime_egress_coordinator.set_current_snapshot(snapshot_b)
                return snapshot_b, payload_b, binding_b

            first_probes: list[dict[str, object]] = []

            def fail_first_leaf(**kwargs):
                first_probes.append(dict(kwargs))
                raise RuntimeError("A FINAL-EXIT failed")

            runtime._verify_runtime_final_exit = fail_first_leaf
            runtime._refresh_loaded_runtime_authority = publish_b
            submissions: list[dict[str, object]] = []
            waited: list[_RuntimeRecoveryTicket] = []

            def submit_same_incident(reason: str, **kwargs):
                submission = {"reason": reason, **kwargs}
                submissions.append(submission)
                activation_owner = kwargs["activation_owner"]
                return _RuntimeRecoveryTicket(
                    generation=1,
                    incident_id="initial-no-capture-incident",
                    observed_at_monotonic=time.monotonic(),
                    deadline_monotonic=activation_owner.deadline_monotonic,
                    snapshot_key=tuple(snapshot_a.snapshot_key),
                    route_epoch=snapshot_a.route_epoch,
                    facility_epoch=activation_owner.facility_epoch,
                    selector_intent_revision=(
                        activation_owner.selector_intent_revision
                    ),
                    worker_token=tuple(activation_owner.worker_token),
                    from_ref=binding_a.node,
                )

            ticket: _RuntimeRecoveryTicket | None = None

            def wait_same_incident(value: _RuntimeRecoveryTicket):
                nonlocal ticket
                ticket = value
                waited.append(value)
                runtime._runtime_last_verified_route = {
                    "incident_id": value.incident_id,
                    "leaf_proxy": ref_b.proxy_name,
                    "result": {"status": 204, "url": "local-204"},
                }
                return RuntimeRecoveryOutcome(
                    status=RuntimeRecoveryStatus.RECOVERED,
                    incident_id=value.incident_id,
                    route_epoch=snapshot_b.route_epoch,
                    budget_ms=900,
                    reason="verified_route",
                    from_ref=binding_a.node,
                    to_ref=ref_b,
                )

            runtime._submit_current_route_probe_failure = submit_same_incident
            runtime._wait_runtime_recovery_outcome = wait_same_incident

            runtime._queue_tier3_egress_activation()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            # A startup failure is terminal when the loaded graph contains no
            # independently verified alternative.  Do not retry the same bad
            # route or consume a replacement absent from that loaded graph.
            self.assertEqual(len(first_probes), 1)
            self.assertTrue(
                all(
                    probe.get("require_controller_ready") is True
                    for probe in first_probes
                )
            )
            self.assertEqual(submissions, [])
            self.assertEqual(waited, [])
            self.assertIsNone(ticket)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertFalse(manager.effective_states[-1][0])
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.DATA_PLANE_FAILED,
            )

    def test_initial_capture_activation_recovers_before_capture_and_ready(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.tier3_startup_gate_satisfied = True
            manager.tier3_traffic_capture_active = False
            manager.effective_egress_ok = False
            capture_markers: list[bool] = []

            def mark_capture_active(active: bool) -> None:
                manager.tier3_traffic_capture_active = bool(active)
                capture_markers.append(bool(active))

            manager.mark_tier3_traffic_capture_active = mark_capture_active
            runtime.manager = manager
            runtime.network_mode = "tun"
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._set_core_runtime_phase("formal_no_capture")
            self._install_injected_runtime_authority(runtime)
            snapshot_a, payload_a, binding_a = (
                runtime._refresh_loaded_runtime_authority()
            )

            ref_b = RuntimeNodeRef(
                tier="tier3",
                pool_kind="tier3b",
                identity="capture-recovered-b-id",
                proxy_name="capture-recovered-b",
            )
            payload_b = json.loads(
                json.dumps(payload_a).replace(
                    binding_a.node.proxy_name,
                    ref_b.proxy_name,
                )
            )
            proxy_map = payload_b.get("proxy_map")
            if isinstance(proxy_map, dict):
                proxy_map[ref_b.proxy_name] = {
                    "tier": ref_b.tier,
                    "pool_kind": ref_b.pool_kind,
                    "identity": ref_b.identity,
                }
            snapshot_b = CoreLoadedRouteSnapshot(
                process_pid=snapshot_a.process_pid,
                process_generation=snapshot_a.process_generation,
                core_generation=snapshot_a.core_generation,
                semantic_digest=snapshot_a.semantic_digest,
                route_epoch=snapshot_a.route_epoch + 1,
                network_context_epoch=snapshot_a.network_context_epoch,
                group_graph=tuple(
                    (
                        group,
                        tuple(
                            ref_b.proxy_name
                            if member == binding_a.node.proxy_name
                            else member
                            for member in members
                        ),
                    )
                    for group, members in snapshot_a.group_graph
                ),
                group_states=tuple(
                    CoreLoadedGroupState(
                        name=state.name,
                        type_name=state.type_name,
                        members=tuple(
                            ref_b.proxy_name
                            if member == binding_a.node.proxy_name
                            else member
                            for member in state.members
                        ),
                        now=(
                            ref_b.proxy_name
                            if state.now == binding_a.node.proxy_name
                            else state.now
                        ),
                    )
                    for state in snapshot_a.group_states
                ),
                proxy_nodes=(ref_b,),
                loaded_at_monotonic=time.monotonic(),
            )
            binding_b = runtime._runtime_route_binding_for_outbound(
                snapshot_b,
                payload_b,
                "FINAL-EXIT",
            )
            self.assertIsNotNone(binding_b)

            def publish_b(**_kwargs):
                with runtime._runtime_route_snapshot_lock:
                    runtime._runtime_route_epoch = snapshot_b.route_epoch
                    runtime._runtime_route_snapshot = snapshot_b
                    runtime._runtime_route_snapshot_payload = payload_b
                runtime._runtime_egress_coordinator.set_current_snapshot(snapshot_b)
                return snapshot_b, payload_b, binding_b

            failed_leaf_probes: list[dict[str, object]] = []

            def verify_native_leaf(**kwargs):
                failed_leaf_probes.append(dict(kwargs))
                self.assertEqual(
                    kwargs["expected_snapshot"].snapshot_key,
                    snapshot_b.snapshot_key,
                )
                return {"status": 204, "url": "local-no-capture-204"}

            runtime._verify_runtime_final_exit = verify_native_leaf
            runtime._refresh_loaded_runtime_authority = publish_b
            submissions: list[dict[str, object]] = []
            waited: list[_RuntimeRecoveryTicket] = []

            def submit_same_incident(reason: str, **kwargs):
                submission = {"reason": reason, **kwargs}
                submissions.append(submission)
                activation_owner = kwargs["activation_owner"]
                return _RuntimeRecoveryTicket(
                    generation=1,
                    incident_id="initial-capture-incident",
                    observed_at_monotonic=time.monotonic(),
                    deadline_monotonic=activation_owner.deadline_monotonic,
                    snapshot_key=tuple(snapshot_a.snapshot_key),
                    route_epoch=snapshot_a.route_epoch,
                    facility_epoch=activation_owner.facility_epoch,
                    selector_intent_revision=(
                        activation_owner.selector_intent_revision
                    ),
                    worker_token=tuple(activation_owner.worker_token),
                    from_ref=binding_a.node,
                )

            def wait_same_incident(value: _RuntimeRecoveryTicket):
                waited.append(value)
                runtime._runtime_last_verified_route = {
                    "incident_id": value.incident_id,
                    "leaf_proxy": ref_b.proxy_name,
                    "result": {"status": 204, "url": "local-204"},
                }
                return RuntimeRecoveryOutcome(
                    status=RuntimeRecoveryStatus.RECOVERED,
                    incident_id=value.incident_id,
                    route_epoch=snapshot_b.route_epoch,
                    budget_ms=900,
                    reason="verified_route",
                    from_ref=binding_a.node,
                    to_ref=ref_b,
                )

            runtime._submit_current_route_probe_failure = submit_same_incident
            runtime._wait_runtime_recovery_outcome = wait_same_incident
            capture_reloads: list[dict[str, object]] = []

            def enable_capture(_runner, *, capture_enabled: bool, **kwargs):
                capture_reloads.append(
                    {"capture_enabled": capture_enabled, **kwargs}
                )
                runtime._set_core_runtime_phase("formal_capture")
                return temp_path / "data" / "mihomo-runtime.yaml"

            runtime._hot_reload_runtime_config_phase = enable_capture
            capture_verifications: list[dict[str, object]] = []

            def verify_capture(**kwargs):
                capture_verifications.append(dict(kwargs))
                current = (
                    runtime._runtime_egress_coordinator.current_route_snapshot()
                )
                self.assertIsNotNone(current)
                self.assertEqual(current.snapshot_key, snapshot_b.snapshot_key)
                return {"status": 204, "url": "local-capture-204"}

            runtime._verify_capture_route_after_reload = verify_capture
            capture_publications: list[str] = []
            runtime._apply_system_proxy_capture_after_core_start = (
                lambda _runner: capture_publications.append("capture")
            )

            runtime._queue_tier3_egress_activation()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            # The refreshed immutable authority already points at B. Startup
            # verifies that native-fallback leaf directly; it never opens the
            # retired pre-READY incident/recovery machine for A.
            self.assertEqual(len(failed_leaf_probes), 1)
            self.assertEqual(submissions, [])
            self.assertEqual(waited, [])
            self.assertEqual(len(capture_reloads), 1)
            self.assertTrue(capture_reloads[0]["capture_enabled"])
            self.assertEqual(len(capture_verifications), 1)
            self.assertIsNotNone(
                capture_verifications[0].get("route_intent")
            )
            self.assertEqual(capture_publications, ["capture"])
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_capture")
            self.assertEqual(
                (runtime.traffic_monitor.started, runtime.traffic_monitor.stopped),
                (1, 0),
            )
            self.assertEqual(capture_markers, [True])
            self.assertTrue(manager.tier3_traffic_capture_active)
            self.assertTrue(manager.effective_egress_ok)
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.READY,
            )

    def test_formal_capture_uses_one_simplified_recovery_without_old_qualification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.tier3_traffic_capture_active = True
            manager.effective_egress_ok = True
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime._runtime_initial_activation_generation = 1
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime.traffic_monitor.started = 1
            runtime._set_core_runtime_phase("formal_capture")
            self._install_injected_runtime_authority(runtime)
            snapshot_a, payload_a, binding_a = (
                runtime._refresh_loaded_runtime_authority()
            )
            runtime._tier3_runtime_final_exit_ok = True
            old_qualification = MagicMock(
                return_value=(
                    binding_a.node.proxy_name,
                    binding_a.node.identity,
                    ("old", "qualification"),
                )
            )
            runtime._tier3_selected_leaf_qualification_evidence = old_qualification
            failed_leaf_probes: list[str] = []
            runtime._verify_runtime_final_exit = lambda **_kwargs: (
                failed_leaf_probes.append("A-204")
                or (_ for _ in ()).throw(
                    RuntimeError("formal capture A FINAL-EXIT failed")
                )
            )
            legacy_recovery = MagicMock(
                side_effect=AssertionError(
                    "the rollback profile must not invoke the legacy "
                    "activation recovery state machine"
                )
            )
            runtime._verify_tier3_final_exit_for_activation = legacy_recovery
            recovery_calls: list[dict[str, object]] = []

            def simplified_recovery(reason, **kwargs):
                recovery_calls.append(
                    {"reason": str(reason), **dict(kwargs)}
                )
                return False

            runtime._submit_current_route_probe_failure = simplified_recovery
            runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None

            runtime._queue_incremental_egress_recheck()
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            self.assertEqual(failed_leaf_probes, [])
            self.assertEqual(recovery_calls, [])
            legacy_recovery.assert_not_called()
            old_qualification.assert_not_called()
            self.assertIsNone(manager.effective_egress_ok)
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.CONTROLLER_HOLD,
            )
            self.assertNotEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.READY,
            )
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_capture")
            self.assertTrue(manager.tier3_traffic_capture_active)
            self.assertEqual(
                (runtime.traffic_monitor.started, runtime.traffic_monitor.stopped),
                (1, 0),
            )

    def test_no_capture_append_only_reload_preserves_qualified_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.network_mode = "system_proxy"
            runtime.system_proxy.enabled = False
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime._runtime_initial_activation_generation = 1
            manager.effective_egress_ok = True
            runtime._runtime_egress_readiness = RuntimeEgressReadiness(
                state=RuntimeEgressReadinessState.READY,
                validation_active=False,
            )
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            runtime.write_mihomo_no_capture_runtime_config = (
                lambda: temp_path / "mihomo-runtime.yaml"
            )
            evidence = ("stable-leaf", "stable-identity", ("stable",))
            runtime._tier3_selected_leaf_qualification_evidence = MagicMock(
                return_value=evidence
            )
            runtime._tier3_preserved_leaf_qualification_evidence = MagicMock(
                return_value=evidence
            )
            queued: list[str] = []
            runtime._queue_tier3_egress_activation = lambda: queued.append("activation")

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertTrue(result["reloaded"])
            self.assertTrue(result["egress_available"])
            self.assertEqual(queued, [])
            self.assertTrue(manager.effective_states[-1][0])
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_no_capture")

    def test_semantically_equal_core_marker_does_not_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            old_marker = (
                ("diagnostic", "old"),
                ("core_semantic_digest", "same-rendered-config"),
            )
            new_marker = (
                ("diagnostic", "new"),
                ("core_semantic_digest", "same-rendered-config"),
            )
            runtime._last_core_export_sync_marker = old_marker
            runtime.reload_core_config = MagicMock(
                side_effect=AssertionError("semantic equality must skip reload")
            )

            self.assertFalse(
                runtime._reload_running_core_config_for_export_marker(new_marker)
            )
            self.assertEqual(runtime._last_core_export_sync_marker, new_marker)
            runtime.reload_core_config.assert_not_called()

    def test_phase_capture_mismatch_fails_closed_without_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="mismatched-capture",
                    protocol="ss",
                    uri="ss://mismatched-capture",
                    address="mismatched.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            manager.tier3_traffic_capture_active = False
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._core_generation = 1
            runtime._runtime_initial_activation_generation = 1
            runtime._set_core_runtime_phase("formal_capture")
            probes: list[str] = []
            runtime._probe_mihomo_mixed_port = lambda: probes.append("unexpected")

            runtime._queue_incremental_egress_recheck()

            self.assertEqual(probes, [])
            self.assertTrue(runner.is_running())
            self.assertFalse(manager.effective_states[-1][0])
            self.assertIn(
                "phase=formal_capture capture_active=False",
                manager.tier3_last_collection_report[
                    "core_revalidation_invariant_error"
                ],
            )

    def test_incremental_export_reload_never_enters_repair_or_strict_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="incremental-export",
                    protocol="ss",
                    uri="ss://incremental-export",
                    address="incremental-export.example.com",
                    port=8388,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )
            ]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime._reload_pruned_primary_tier3_export = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("incremental export must not run Tier3 repair")
            )
            runtime._verify_tier3_after_core_reload = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("incremental export must not run strict verification")
            )
            runtime._repair_and_retry_tier3_reload_after_verification_failure = (
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("incremental export must not enter repair retry")
                )
            )

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertTrue(result["reloaded"])
            self.assertEqual(result["mode"], "incremental_export")
            self.assertEqual(runtime.core_runner.hot_reloaded, 1)
            self.assertEqual(len(manager.runtime_nodes["tier3"]), 1)

    def test_incremental_export_failure_rolls_back_without_final_exit_or_pool_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            node = Node(
                name="incremental-rollback",
                protocol="ss",
                uri="ss://incremental-rollback",
                address="incremental-rollback.example.com",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)

            def write_new_config():
                runtime_config.write_text("new-config", encoding="utf-8")
                return runtime_config

            reload_results = iter(
                [
                    {"reloaded": False, "reason": "synthetic reload failure"},
                    {"reloaded": True},
                ]
            )
            runtime.write_mihomo_runtime_config = write_new_config
            runtime.core_runner.hot_reload = lambda **_kwargs: next(reload_results)
            runtime._probe_final_exit_delay = lambda: (_ for _ in ()).throw(
                AssertionError("incremental rollback must not probe FINAL-EXIT")
            )
            runtime._restore_tier3_pool_after_failed_reload = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("incremental rollback must not mutate the Tier3 pool")
                )
            )

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["kept_existing_core"])
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), "old-config")
            self.assertEqual(manager.runtime_nodes["tier3"], [node])

    def test_tier3_pool_commit_burst_coalesces_incremental_export_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 1
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._tier3_pool_sync_debounce_seconds = 0.05
            runtime._tier3_pool_sync_max_wait_seconds = 0.05
            calls: list[str] = []
            calls_lock = threading.Lock()

            def record_reload(**kwargs):
                with calls_lock:
                    calls.append(str(kwargs.get("mode")))
                time.sleep(0.02)
                return {"reloaded": True}

            runtime.reload_core_config = record_reload
            nodes: list[Node] = []
            for index in range(5):
                nodes.append(
                    Node(
                        name=f"burst-{index}",
                        protocol="ss",
                        uri=f"ss://burst-{index}",
                        address=f"burst-{index}.example.com",
                        port=8400 + index,
                        tier="tier3",
                        method="aes-256-gcm",
                        password="pass",
                        status=NodeStatus.ALIVE,
                    )
                )
                manager.runtime_nodes["tier3"] = list(nodes)
                runtime._on_tier3_pool_committed(
                    list(nodes),
                    {"alive_count": len(nodes), "total_count": len(nodes)},
                )

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with runtime._tier3_pool_sync_lock:
                    worker_running = runtime._tier3_pool_sync_worker_running
                if calls and not worker_running:
                    break
                time.sleep(0.01)

            self.assertEqual(calls, ["incremental_export"])

    def test_core_export_marker_ignores_metadata_but_tracks_effective_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            candidate = Node(
                name="semantic-marker",
                protocol="ss",
                uri="ss://semantic-marker",
                address="semantic-marker.example.com",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
                extra={"tier3_probe_attempt": 1},
            )
            manager.runtime_nodes["tier3"] = [candidate]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()

            initial = runtime._core_export_sync_marker()
            runtime._last_core_export_sync_marker = initial
            candidate.extra["tier3_probe_attempt"] = 200
            candidate.extra["tier3_source_route"] = "runtime-private"
            self.assertEqual(runtime._core_export_sync_marker(), initial)
            self.assertFalse(
                runtime._reload_running_core_config_for_export_marker(
                    runtime._core_export_sync_marker()
                )
            )
            self.assertEqual(runtime.core_runner.hot_reloaded, 0)

            candidate.status = NodeStatus.DEAD
            dead_marker = runtime._core_export_sync_marker()
            self.assertEqual(dead_marker, initial)
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["semantic-marker"],
            )
            membership = runtime._tier3_core_membership_snapshot()
            self.assertEqual(
                membership.identities,
                (runtime._node_identity_text(candidate),),
            )
            self.assertEqual(
                [
                    node.name
                    for node in runtime._manager_for_mihomo_runtime_config().get_nodes_for_subscription(
                        "tier3"
                    )
                ],
                ["semantic-marker"],
            )

            candidate.status = NodeStatus.ALIVE
            self.assertEqual(runtime._core_export_sync_marker(), initial)

            candidate.password = "changed-password"
            self.assertNotEqual(runtime._core_export_sync_marker(), initial)

    def test_runtime_qualification_survives_unrelated_semantic_generation_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            retained = Node(
                name="retained",
                protocol="ss",
                uri="ss://retained",
                address="retained.example.com",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [retained]
            runtime.manager = manager
            initial = runtime._core_export_sync_marker()
            runtime._advance_tier3_runtime_config_generation(initial)
            identity = manager._node_identity(retained)
            signature = runtime._tier3_runtime_export_semantics()[identity]
            with runtime._tier3_runtime_qualification_lock:
                runtime._tier3_runtime_qualified[identity] = {
                    "proxy_signature": tuple(signature),
                    "qualified_at_monotonic": time.monotonic(),
                    "expires_at_monotonic": time.monotonic() + 60,
                }
                runtime._tier3_runtime_final_exit_ok = True

            retained.extra["tier3_probe_attempt"] = 99
            runtime._advance_tier3_runtime_config_generation(
                runtime._core_export_sync_marker()
            )
            self.assertIn(identity, runtime._tier3_runtime_qualified)

            manager.runtime_nodes["tier3"].append(
                Node(
                    name="new-leaf",
                    protocol="ss",
                    uri="ss://new-leaf",
                    address="new-leaf.example.com",
                    port=8389,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="new-pass",
                    status=NodeStatus.ALIVE,
                )
            )
            runtime._advance_tier3_runtime_config_generation(
                runtime._core_export_sync_marker(),
                preserve_final_exit=True,
            )
            self.assertIn(identity, runtime._tier3_runtime_qualified)
            self.assertTrue(runtime._tier3_runtime_final_exit_ok)

            retained.password = "changed-password"
            runtime._advance_tier3_runtime_config_generation(
                runtime._core_export_sync_marker()
            )
            self.assertNotIn(identity, runtime._tier3_runtime_qualified)
            self.assertFalse(runtime._tier3_runtime_final_exit_ok)

    def test_tier3_activation_requires_runtime_qualified_safety_quorum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            manager.runtime_nodes["tier3"] = [
                Node(
                    name=f"admitted-{index}",
                    protocol="ss",
                    uri=f"ss://admitted-{index}",
                    address=f"admitted-{index}.example.com",
                    port=8400 + index,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )
                for index in range(14)
            ]
            runtime.manager = manager
            final_exit_calls: list[str] = []
            runtime._probe_mihomo_mixed_port = lambda: (
                final_exit_calls.append("probe") or {"url": "fixture", "latency_ms": 1}
            )

            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=14,
                    exported_count=3,
                    qualified_count=2,
                    qualified_identities=("a", "b"),
                    final_exit_ok=False,
                    traffic_capture_active=False,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            with self.assertRaisesRegex(RuntimeError, "below the safety floor"):
                runtime._verify_tier3_final_exit_for_activation()
            self.assertEqual(final_exit_calls, [])

            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=14,
                    exported_count=3,
                    qualified_count=3,
                    qualified_identities=("a", "b", "c"),
                    final_exit_ok=False,
                    traffic_capture_active=False,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            result = runtime._verify_tier3_final_exit_for_activation()

            self.assertEqual(result["url"], "fixture")
            self.assertEqual(final_exit_calls, ["probe"])

    def test_tier3_activation_failure_enters_canonical_leaf_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 1
            runtime.manager = manager
            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=2,
                    exported_count=2,
                    qualified_count=1,
                    qualified_identities=("healthy-alternative",),
                    final_exit_ok=False,
                    traffic_capture_active=False,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            runtime._probe_mihomo_mixed_port = lambda: (
                (_ for _ in ()).throw(
                    AssertionError("known 204 failure must not be probed twice")
                )
            )
            submissions: list[tuple[str, str]] = []
            runtime._submit_current_route_probe_failure = (
                lambda reason, *, source: submissions.append((reason, source))
                or True
            )

            with self.assertRaisesRegex(RuntimeError, "selected leaf is dead"):
                runtime._verify_tier3_final_exit_for_activation(
                    known_probe_failure=RuntimeError("selected leaf is dead")
                )

            self.assertEqual(len(submissions), 1)
            self.assertEqual(submissions[0][1], "activation_final_exit")
            self.assertIn("RuntimeError", submissions[0][0])
            self.assertFalse(runtime._tier3_runtime_final_exit_ok)

    def test_tier3_activation_waits_for_same_incident_recovery_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 1
            runtime.manager = manager
            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=2,
                    exported_count=2,
                    qualified_count=1,
                    qualified_identities=("healthy-alternative",),
                    final_exit_ok=False,
                    traffic_capture_active=False,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            failed = RuntimeNodeRef(
                "tier3", "tier3b", "failed-id", "failed-leaf"
            )
            recovered = RuntimeNodeRef(
                "tier3", "tier3b", "recovered-id", "recovered-leaf"
            )
            before = CoreLoadedRouteSnapshot(
                process_pid=4312,
                process_generation=7,
                core_generation=3,
                semantic_digest="loaded",
                route_epoch=20,
                proxy_nodes=(failed, recovered),
            )
            after = CoreLoadedRouteSnapshot(
                process_pid=4312,
                process_generation=7,
                core_generation=3,
                semantic_digest="loaded",
                route_epoch=21,
                proxy_nodes=(failed, recovered),
            )
            rebound = RuntimeRouteBinding(
                process_pid=after.process_pid,
                process_generation=after.process_generation,
                core_generation=after.core_generation,
                semantic_digest=after.semantic_digest,
                route_epoch=after.route_epoch,
                observed_outbound="FINAL-EXIT",
                route_chain=("FINAL-EXIT", "TIER3-AUTO", "recovered-leaf"),
                node=recovered,
            )
            observed = time.monotonic()
            ticket = _RuntimeRecoveryTicket(
                generation=1,
                incident_id="opaque-incident",
                observed_at_monotonic=observed,
                deadline_monotonic=observed + 6.0,
                snapshot_key=before.snapshot_key,
                route_epoch=before.route_epoch,
                facility_epoch=runtime._runtime_facility_epoch,
                selector_intent_revision=(
                    runtime._runtime_selector_intent_revision
                ),
                worker_token=runtime._runtime_worker_token(),
                from_ref=failed,
            )
            outcome = RuntimeRecoveryOutcome(
                status=RuntimeRecoveryStatus.RECOVERED,
                incident_id=ticket.incident_id,
                route_epoch=after.route_epoch,
                budget_ms=900,
                reason="verified_route",
                from_ref=failed,
                to_ref=recovered,
            )
            runtime._probe_mihomo_mixed_port = lambda: (
                (_ for _ in ()).throw(RuntimeError("selected leaf is dead"))
            )
            runtime._submit_current_route_probe_failure = (
                lambda *_args, **_kwargs: ticket
            )
            waited: list[str] = []
            runtime._wait_runtime_recovery_outcome = lambda value: (
                waited.append(value.incident_id) or outcome
            )
            runtime._current_runtime_failure_binding_state = (
                lambda **_kwargs: (rebound, after, {"proxies": {}})
            )
            runtime._runtime_last_verified_route = {
                "incident_id": ticket.incident_id,
                "result": {"status": 204, "url": "local-204"}
            }
            qualified: list[str] = []
            runtime._queue_selected_tier3_leaf_qualification = (
                lambda *, probe_endpoint: qualified.append(probe_endpoint)
            )

            result = runtime._verify_tier3_final_exit_for_activation(
                known_probe_failure=RuntimeError("selected leaf is dead")
            )

            self.assertEqual(waited, [ticket.incident_id])
            self.assertEqual(result["status"], 204)
            self.assertEqual(
                result["runtime_recovery_outcome"]["status"],
                "recovered",
            )
            self.assertEqual(qualified, ["local-204"])
            self.assertTrue(runtime._tier3_runtime_final_exit_ok)

    def test_tier3_activation_accepts_fixed_gateway_underlay_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 1
            runtime.manager = manager
            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=2,
                    exported_count=2,
                    qualified_count=1,
                    qualified_identities=("healthy-underlay",),
                    final_exit_ok=False,
                    traffic_capture_active=False,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            gateway = RuntimeNodeRef(
                "exit", "gateway", "gateway-id", "fixed-gateway"
            )
            underlay_a = RuntimeNodeRef(
                "tier1", "tier1_variant", "underlay-a-id", "underlay-a"
            )
            underlay_b = RuntimeNodeRef(
                "tier1", "tier1_variant", "underlay-b-id", "underlay-b"
            )
            before = CoreLoadedRouteSnapshot(
                process_pid=4312,
                process_generation=7,
                core_generation=3,
                semantic_digest="loaded",
                route_epoch=20,
                proxy_nodes=(gateway, underlay_a, underlay_b),
            )
            after = CoreLoadedRouteSnapshot(
                process_pid=4312,
                process_generation=7,
                core_generation=3,
                semantic_digest="loaded",
                route_epoch=21,
                proxy_nodes=(gateway, underlay_a, underlay_b),
            )

            def payload(selected_underlay: str) -> dict[str, object]:
                return {
                    "proxies": {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "EXIT-SELECT",
                            "all": ["EXIT-SELECT", "REJECT"],
                        },
                        "EXIT-SELECT": {
                            "type": "Selector",
                            "now": gateway.proxy_name,
                            "all": [gateway.proxy_name],
                        },
                        gateway.proxy_name: {"type": "Shadowsocks"},
                        "PROXY-FALLBACK": {
                            "type": "Fallback",
                            "now": selected_underlay,
                            "all": [
                                underlay_a.proxy_name,
                                underlay_b.proxy_name,
                            ],
                        },
                        underlay_a.proxy_name: {"type": "Shadowsocks"},
                        underlay_b.proxy_name: {"type": "Shadowsocks"},
                    },
                    "proxy_map": {
                        gateway.proxy_name: {
                            "tier": gateway.tier,
                            "pool_kind": gateway.pool_kind,
                            "identity": gateway.identity,
                            "proxy": {
                                "name": gateway.proxy_name,
                                "dialer-proxy": "PROXY-FALLBACK",
                            },
                        },
                        underlay_a.proxy_name: {
                            "tier": underlay_a.tier,
                            "pool_kind": underlay_a.pool_kind,
                            "identity": underlay_a.identity,
                        },
                        underlay_b.proxy_name: {
                            "tier": underlay_b.tier,
                            "pool_kind": underlay_b.pool_kind,
                            "identity": underlay_b.identity,
                        },
                    },
                }

            before_payload = payload(underlay_a.proxy_name)
            after_payload = payload(underlay_b.proxy_name)
            before_binding = RuntimeRouteBinding(
                process_pid=before.process_pid,
                process_generation=before.process_generation,
                core_generation=before.core_generation,
                semantic_digest=before.semantic_digest,
                route_epoch=before.route_epoch,
                observed_outbound="FINAL-EXIT",
                route_chain=(
                    "FINAL-EXIT",
                    "EXIT-SELECT",
                    gateway.proxy_name,
                ),
                node=gateway,
            )
            rebound_binding = RuntimeRouteBinding(
                process_pid=after.process_pid,
                process_generation=after.process_generation,
                core_generation=after.core_generation,
                semantic_digest=after.semantic_digest,
                route_epoch=after.route_epoch,
                observed_outbound="FINAL-EXIT",
                route_chain=(
                    "FINAL-EXIT",
                    "EXIT-SELECT",
                    gateway.proxy_name,
                ),
                node=gateway,
            )
            observed = time.monotonic()
            ticket = _RuntimeRecoveryTicket(
                generation=1,
                incident_id="gateway-activation-incident",
                observed_at_monotonic=observed,
                deadline_monotonic=observed + 6.0,
                snapshot_key=before.snapshot_key,
                route_epoch=before.route_epoch,
                facility_epoch=runtime._runtime_facility_epoch,
                selector_intent_revision=(
                    runtime._runtime_selector_intent_revision
                ),
                worker_token=runtime._runtime_worker_token(),
                from_ref=gateway,
            )
            outcome = RuntimeRecoveryOutcome(
                status=RuntimeRecoveryStatus.RECOVERED,
                incident_id=ticket.incident_id,
                route_epoch=after.route_epoch,
                budget_ms=800,
                reason="verified_route",
                from_ref=gateway,
                to_ref=underlay_b,
            )
            runtime._submit_current_route_probe_failure = (
                lambda *_args, **_kwargs: ticket
            )
            runtime._wait_runtime_recovery_outcome = lambda _ticket: outcome
            runtime._current_runtime_failure_binding_state = (
                lambda **_kwargs: (
                    rebound_binding,
                    after,
                    after_payload,
                )
            )
            runtime._runtime_last_verified_route = {
                "incident_id": ticket.incident_id,
                "result": {"status": 204, "url": "local-204"},
            }

            result = runtime._verify_tier3_final_exit_for_activation(
                known_probe_failure=RuntimeError("Gateway underlay A failed"),
                binding_state=(before_binding, before, before_payload),
            )

            self.assertEqual(result["status"], 204)
            self.assertEqual(
                result["runtime_recovery_outcome"]["status"],
                "recovered",
            )
            self.assertTrue(runtime._tier3_runtime_final_exit_ok)

    def test_final_exit_pass_qualifies_the_selected_tier3_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            # The controller payload below names leaves positionally (switch off).
            runtime.config.setdefault("tiers", {}).setdefault("tier3", {})["verified_provider_writes"] = False
            manager = FakeTier3Manager()
            selected = Node(
                name="selected-final-leaf",
                protocol="ss",
                uri="ss://selected-final-leaf",
                address="selected.example.com",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [selected]
            runtime.manager = manager
            proxies = {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "PROXY-FALLBACK",
                        "all": ["PROXY-FALLBACK"],
                    },
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "TIER3-AUTO",
                        "all": ["TIER3-AUTO"],
                    },
                    "TIER3-AUTO": {
                        "type": "Fallback",
                        "now": "selected-final-leaf",
                        "all": ["selected-final-leaf"],
                    },
                    "selected-final-leaf": {
                        "type": "Shadowsocks",
                        # /delay may be stale even though the authoritative
                        # mixed-port FINAL-EXIT request below succeeds.
                        "alive": False,
                    },
                }
            }
            runtime.mihomo_proxies = lambda: proxies
            # The asynchronous bookkeeping callback must bind only to the
            # controller view frozen before the successful FINAL-EXIT probe.
            runtime._tier3_runtime_controller_snapshot = (
                int(runtime._tier3_runtime_config_generation),
                str(runtime._tier3_runtime_config_digest),
                proxies,
            )
            runtime._refresh_tier3_runtime_qualification = lambda: (
                Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=1,
                    exported_count=1,
                    qualified_count=1,
                    qualified_identities=("other-qualified-leaf",),
                    final_exit_ok=False,
                    traffic_capture_active=False,
                    observed_at_monotonic=time.monotonic(),
                )
            )
            runtime._probe_mihomo_mixed_port = lambda: {
                "url": "https://probe.example/generate_204",
                "latency_ms": 10,
            }
            runtime._core_generation = 1

            runtime._verify_tier3_final_exit_for_activation()

            identity = manager._node_identity(selected)
            self.assertTrue(
                self._wait_until(
                    lambda: identity in runtime._tier3_runtime_qualified,
                    timeout=0.5,
                )
            )
            self.assertIn(identity, runtime._tier3_runtime_qualified)
            self.assertEqual(
                runtime._tier3_runtime_qualified[identity]["evidence"],
                "final_exit_mixed_port",
            )
            self.assertIsNone(
                runtime._tier3_selected_leaf_qualification_evidence()
            )
            runtime._runtime_initial_activation_generation = 1
            manager.effective_egress_ok = True
            runtime._runtime_egress_readiness = RuntimeEgressReadiness(
                state=RuntimeEgressReadinessState.READY,
                validation_active=False,
            )
            self.assertEqual(
                runtime._tier3_selected_leaf_evidence,
                runtime._tier3_selected_leaf_qualification_evidence(),
            )
            manager.tier3_traffic_capture_active = True
            evidence = runtime._tier3_selected_leaf_qualification_evidence()
            self.assertEqual(evidence[0], "selected-final-leaf")
            self.assertEqual(
                runtime._tier3_preserved_leaf_qualification_evidence(evidence),
                evidence,
            )

    def test_delayed_leaf_qualification_cannot_follow_a_new_route_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            # The controller payload below names leaves positionally (switch off).
            runtime.config.setdefault("tiers", {}).setdefault("tier3", {})["verified_provider_writes"] = False
            manager = FakeTier3Manager()
            old_leaf = Node(
                name="old-leaf",
                protocol="ss",
                uri="ss://old-leaf",
                address="old.example.com",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="old-pass",
                status=NodeStatus.ALIVE,
            )
            new_leaf = Node(
                name="new-leaf",
                protocol="ss",
                uri="ss://new-leaf",
                address="new.example.com",
                port=8389,
                tier="tier3",
                method="aes-256-gcm",
                password="new-pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [old_leaf, new_leaf]
            runtime.manager = manager

            def controller_payload(selected: str) -> dict[str, object]:
                return {
                    "proxies": {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "TIER3-AUTO",
                            "all": ["TIER3-AUTO"],
                        },
                        "TIER3-AUTO": {
                            "type": "Fallback",
                            "now": selected,
                            "all": ["old-leaf", "new-leaf"],
                        },
                        "old-leaf": {"type": "Shadowsocks", "alive": True},
                        "new-leaf": {"type": "Shadowsocks", "alive": True},
                    }
                }

            runtime._tier3_runtime_controller_snapshot = (
                int(runtime._tier3_runtime_config_generation),
                str(runtime._tier3_runtime_config_digest),
                controller_payload("old-leaf"),
            )
            runtime._runtime_route_epoch = 7
            runtime._runtime_worker_token_is_current = lambda _token: True
            with patch("desktop_runtime.threading.Thread") as thread_type:
                runtime._queue_selected_tier3_leaf_qualification(
                    probe_endpoint="cp.cloudflare.com",
                )
                deferred_worker = thread_type.call_args.kwargs["target"]

            runtime._runtime_route_epoch = 8
            runtime._tier3_runtime_controller_snapshot = (
                int(runtime._tier3_runtime_config_generation),
                str(runtime._tier3_runtime_config_digest),
                controller_payload("new-leaf"),
            )
            deferred_worker()

            self.assertIsNone(runtime._tier3_selected_leaf_evidence)
            self.assertNotIn(
                manager._node_identity(old_leaf),
                runtime._tier3_runtime_qualified,
            )
            self.assertNotIn(
                manager._node_identity(new_leaf),
                runtime._tier3_runtime_qualified,
            )

    def test_tier3_final_exit_has_no_periodic_ttl_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")
            runtime._tier3_is_primary_export = lambda: True
            runtime._egress_revalidation_active = lambda: False
            runtime._runtime_traffic_capture_wanted = lambda: True
            runtime._core_generation = 7
            runtime._tier3_runtime_config_digest = "stable-digest"
            runtime._tier3_selected_leaf_evidence = (
                "leaf",
                "identity",
                ("stable-signature",),
            )
            runtime._tier3_runtime_qualification_monitor_at = 60.0
            queued: list[float] = []
            runtime._queue_incremental_egress_recheck = (
                lambda: queued.append(current_time[0])
            )

            current_time = [0.0]
            with patch(
                "desktop_runtime.time.monotonic",
                side_effect=lambda: current_time[0],
            ):
                for second in range(301):
                    current_time[0] = float(second)
                    runtime._maybe_revalidate_tier3_runtime_qualification()
            self.assertEqual(queued, [])

    def test_twenty_pool_sync_requests_batch_without_append_only_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-runtime", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="committed",
                    protocol="ss",
                    uri="ss://committed",
                    address="committed.example.com",
                    port=8388,
                    tier="tier3",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )
            ]
            manager.tier3_core_running = True
            manager.tier3_traffic_capture_active = True

            def set_effective_state(ok, reason: str = "") -> None:
                manager.effective_egress_ok = ok
                manager.effective_states.append((ok, reason))

            manager.set_effective_egress_state = set_effective_state
            manager.effective_egress_ok = True
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runner.status_payload["pid"] = 5151
            runtime.core_runner = runner
            runtime.running = True
            runtime._set_core_runtime_phase("formal_capture")
            runtime._core_generation = 11
            monitor = FakeTrafficMonitor()
            monitor.started = 1
            runtime.traffic_monitor = monitor
            runtime._tier3_pool_sync_debounce_seconds = 0.05
            runtime.write_mihomo_runtime_config = lambda: runtime_config
            runtime._sync_core_config_after_pool_update = lambda: (
                runtime.reload_core_config(
                    mode="incremental_export",
                    reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                )
            )
            probes: list[str] = []
            identities: list[str] = []
            runtime._probe_mihomo_mixed_port = lambda: (
                probes.append("204")
                or (_ for _ in ()).throw(
                    AssertionError("append-only sync must preserve FINAL-EXIT")
                )
            )
            def refresh_fixture_qualification():
                runtime._tier3_runtime_controller_snapshot = (
                    int(runtime._tier3_runtime_config_generation),
                    str(runtime._tier3_runtime_config_digest),
                    {"proxies": {}},
                )
                return Tier3RuntimeQualificationSnapshot(
                    config_generation=1,
                    config_digest="fixture",
                    admitted_count=3,
                    exported_count=3,
                    qualified_count=3,
                    qualified_identities=("a", "b", "c"),
                    final_exit_ok=False,
                    traffic_capture_active=True,
                    observed_at_monotonic=time.monotonic(),
                )
            runtime._refresh_tier3_runtime_qualification = (
                refresh_fixture_qualification
            )
            runtime._verify_final_exit_identity = (
                lambda **_kwargs: identities.append("identity") or {"state": "complete"}
            )
            activation_requests: list[str] = []
            runtime._queue_tier3_egress_activation = (
                lambda: activation_requests.append("activation")
            )
            incremental_requests: list[str] = []
            runtime._queue_incremental_egress_recheck = (
                lambda: incremental_requests.append("incremental")
            )
            selected_leaf_evidence = (
                "tier3-leaf",
                "tier3|ss|committed.example.com|8388|pass||||||",
                ("tier3-leaf", "stable-signature"),
            )
            runtime._tier3_selected_leaf_qualification_evidence = MagicMock(
                return_value=selected_leaf_evidence
            )
            runtime._apply_system_proxy_capture_after_core_start = lambda *_args: (
                (_ for _ in ()).throw(
                    AssertionError("incremental recheck must not apply the system proxy")
                )
            )
            runtime._hot_reload_runtime_config_phase = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("incremental recheck must not reload capture state")
                )
            )

            for _ in range(20):
                runtime._queue_tier3_pool_sync_after_commit()

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with runtime._tier3_pool_sync_lock:
                    pool_sync_running = runtime._tier3_pool_sync_worker_running
                if not pool_sync_running:
                    break
                time.sleep(0.01)

            self.assertEqual(manager.effective_states, [])
            self.assertEqual(activation_requests, [])
            self.assertEqual(incremental_requests, [])
            self.assertEqual(
                runtime._tier3_selected_leaf_qualification_evidence.call_count,
                0,
            )
            self.assertIsNotNone(runtime._tier3_pool_sync_parked_key)
            self.assertFalse(runtime._tier3_pool_sync_worker_running)

            self.assertEqual(probes, [])
            self.assertEqual(identities, [])
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertTrue(runner.is_running())
            self.assertEqual(runner.status()["pid"], 5151)
            self.assertEqual(runtime._core_generation, 11)
            self.assertEqual(runtime._current_core_runtime_phase(), "formal_capture")
            self.assertTrue(manager.tier3_traffic_capture_active)
            self.assertEqual((monitor.started, monitor.stopped), (1, 0))

    def test_pool_sync_parks_once_until_revalidation_owner_changes_causal_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime._tier3_pool_sync_debounce_seconds = 0.01
            runtime._tier3_pool_sync_stable_hard_limit_seconds = 0.01
            runtime._tier3_pool_sync_max_wait_seconds = 0.01
            revalidation_active = threading.Event()
            revalidation_active.set()
            sync_calls: list[str] = []
            runtime._egress_revalidation_active = revalidation_active.is_set

            def sync_latest_membership(*, expected_key=None):
                del expected_key
                sync_calls.append("parked" if revalidation_active.is_set() else "resumed")
                return (
                    ProviderSyncDisposition.PARKED
                    if revalidation_active.is_set()
                    else ProviderSyncDisposition.NOOP
                )

            runtime._sync_core_config_after_pool_update = sync_latest_membership

            for _index in range(5):
                runtime._queue_tier3_pool_sync_after_commit()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with runtime._tier3_pool_sync_lock:
                    running = runtime._tier3_pool_sync_worker_running
                if not running:
                    break
                time.sleep(0.01)
            self.assertEqual(sync_calls, ["parked"])
            self.assertFalse(runtime._tier3_pool_sync_pending)
            self.assertIsNotNone(runtime._tier3_pool_sync_parked_key)

            # Wall-clock passage and clearing the busy flag are not causal
            # wakeups. The owner that completes revalidation advances an epoch
            # and then invokes the idempotent wake hook.
            time.sleep(0.08)
            self.assertEqual(sync_calls, ["parked"])

            revalidation_active.clear()
            self.assertFalse(
                runtime._wake_parked_tier3_pool_sync_if_causal_key_changed()
            )
            runtime._runtime_facility_epoch += 1
            self.assertTrue(
                runtime._wake_parked_tier3_pool_sync_if_causal_key_changed()
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with runtime._tier3_pool_sync_lock:
                    running = runtime._tier3_pool_sync_worker_running
                if len(sync_calls) == 2 and not running:
                    break
                time.sleep(0.01)

            self.assertEqual(sync_calls, ["parked", "resumed"])

    def test_formal_pool_sync_keeps_append_only_updates_batched_while_qualification_is_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.running = True
            runtime._set_core_runtime_phase("formal_no_capture")
            runtime._tier3_pool_sync_batch_size = 4
            runtime._tier3_pool_sync_debounce_seconds = 0.01
            runtime._tier3_pool_sync_max_wait_seconds = 0.01
            runtime._tier3_pool_sync_stable_hard_limit_seconds = 0.5
            runtime._tier3_runtime_qualification_stable = lambda: False
            runtime._mark_tier3_pool_sync_formal_baseline_loaded()
            sync_event = threading.Event()
            sync_calls: list[str] = []

            def sync_latest_membership() -> None:
                sync_calls.append("latest-membership")
                sync_event.set()

            runtime._sync_core_config_after_pool_update = sync_latest_membership

            runtime._queue_tier3_pool_sync_after_commit()
            self.assertFalse(sync_event.wait(timeout=0.08))
            for _index in range(3):
                runtime._queue_tier3_pool_sync_after_commit()

            self.assertTrue(sync_event.wait(timeout=0.5))
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with runtime._tier3_pool_sync_lock:
                    running = runtime._tier3_pool_sync_worker_running
                if not running:
                    break
                time.sleep(0.01)
            self.assertEqual(sync_calls, ["latest-membership"])
            self.assertIsNone(runtime._tier3_pool_sync_parked_key)
            self.assertFalse(runtime._tier3_pool_sync_worker_running)

    def test_tier3_pool_commit_callback_defers_running_core_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            node = Node(
                name="deferred-sync",
                protocol="ss",
                uri="ss://deferred-sync",
                address="deferred-sync.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            sync_entered = threading.Event()
            sync_can_finish = threading.Event()

            def slow_sync():
                sync_entered.set()
                sync_can_finish.wait(timeout=5)

            runtime._sync_core_config_after_pool_update = slow_sync

            started = time.monotonic()
            runtime._on_tier3_pool_committed([node], {"alive_count": 1, "total_count": 1})
            elapsed_ms = int(round((time.monotonic() - started) * 1000))

            self.assertLess(elapsed_ms, 200)
            self.assertFalse(sync_entered.wait(timeout=0.1))
            self.assertFalse(sync_entered.wait(timeout=1.0))
            self.assertTrue(sync_entered.wait(timeout=1.5))
            sync_can_finish.set()

    def test_ready_tier3_commit_only_queues_core_gate_when_gate_starts_core_synchronously(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 1
            node = Node(
                name="ready-at-entry",
                protocol="ss",
                uri="ss://ready-at-entry",
                address="ready-at-entry.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [node]
            runner = FakeLifecycleCoreRunner()
            runtime.manager = manager
            runtime.running = True
            runtime.core_runner = runner
            queued: list[str] = []

            def queue_core_gate() -> bool:
                queued.append("core_gate")
                runner.start()
                return True

            runtime._queue_tier3_core_gate_attempt = queue_core_gate
            runtime._queue_tier3_pool_sync_after_commit = (
                lambda: queued.append("pool_sync")
            )

            runtime._on_tier3_pool_committed(
                [node],
                {"alive_count": 1, "total_count": 1},
            )

            self.assertTrue(runner.is_running())
            self.assertEqual(queued, ["core_gate"])

    def test_failed_tier3_pool_reload_is_not_marked_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {"tier3": {"min_collect_interval": 1}}
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_tier3_core_sync_marker = ("old",)
            calls = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {
                "reloaded": False,
                "kept_existing_core": True,
                "core": {"running": True},
            }

            manager.refresh_tier3()
            current_marker = runtime._tier3_core_sync_marker()
            runtime._sync_core_config_after_pool_update()
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["reload"])
            self.assertEqual(runtime._last_tier3_core_sync_marker, ("old",))
            self.assertEqual(runtime._last_failed_tier3_core_sync_marker, current_marker)

            runtime._last_failed_tier3_core_sync_at -= 2
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["reload", "reload"])

    def test_running_tier3_core_reload_helper_skips_unchanged_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            marker = runtime._tier3_core_sync_marker()
            runtime._last_tier3_core_sync_marker = marker
            calls: list[str] = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}

            reloaded = runtime._reload_running_tier3_core_config_for_marker(marker)

            self.assertFalse(reloaded)
            self.assertEqual(calls, [])
            self.assertEqual(runtime._last_tier3_core_sync_marker, marker)

    def test_running_tier3_core_reload_helper_reports_failed_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {"tier3": {"min_collect_interval": 1}}
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_tier3_core_sync_marker = ("old",)
            marker = runtime._tier3_core_sync_marker()
            runtime.reload_core_config = lambda **_kwargs: {
                "reloaded": False,
                "kept_existing_core": True,
                "core": {"running": True},
            }

            reloaded = runtime._reload_running_tier3_core_config_for_marker(marker)

            self.assertFalse(reloaded)
            self.assertEqual(runtime._last_tier3_core_sync_marker, ("old",))
            self.assertEqual(runtime._last_failed_tier3_core_sync_marker, marker)

    def test_tier3_pool_change_starts_core_when_service_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.running = True
            runtime._desired_running = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            manager.refresh_tier3()
            runtime._sync_core_config_after_pool_update()

            self.assertTrue(runtime.core_runner.started)
            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertEqual(
                runtime._current_core_runtime_phase(),
                "formal_no_capture",
            )
            self.assertIs(
                manager.core_running_marks[-1]["traffic_capture_active"],
                False,
            )
            self.assertTrue(manager.effective_states[-1][0])
            self.assertEqual(runtime._last_tier3_core_sync_marker, runtime._tier3_core_sync_marker())

    def test_initial_ready_projection_never_takes_manager_lock_under_runtime_locks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.running = True
            runtime._desired_running = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = (
                lambda: temp_path / "mihomo-runtime.yaml"
            )
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()

            class GuardedManagerLock:
                def __init__(self) -> None:
                    self._lock = threading.RLock()
                    self.enter_count = 0

                def __enter__(self):
                    self.enter_count += 1
                    self.assert_runtime_locks_released()
                    self._lock.acquire()
                    return self

                def __exit__(self, exc_type, exc, tb) -> None:
                    self._lock.release()

                @staticmethod
                def assert_runtime_locks_released() -> None:
                    for lock_name in (
                        "_runtime_intent_lock",
                        "_runtime_failure_monitor_lock",
                    ):
                        lock = getattr(runtime, lock_name)
                        is_owned = getattr(lock, "_is_owned", None)
                        if callable(is_owned) and is_owned():
                            raise AssertionError(
                                f"Manager.lock requested while {lock_name} is held"
                            )

            guarded_lock = GuardedManagerLock()
            manager.lock = guarded_lock

            manager.refresh_tier3()
            runtime._sync_core_config_after_pool_update()

            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )
            self.assertGreater(guarded_lock.enter_count, 0)
            self.assertTrue(manager.effective_states[-1][0])

    def test_tier3_commit_callback_starts_core_from_committed_pool_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "min_alive_nodes": 3},
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 3
            runtime.manager = manager
            runtime.running = True
            runtime._desired_running = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._prime_tier3_pool_before_core_start = lambda: self.fail("commit gate must not re-enter Tier3 recovery")
            runtime._attach_manager_runtime_hooks(manager)
            nodes = [
                Node(
                    name=f"committed-{index}",
                    protocol="ss",
                    uri=f"ss://committed-{index}",
                    address=f"committed-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                for index in range(17)
            ]
            manager.runtime_nodes["tier3"] = nodes
            manager.on_tier3_pool_committed(nodes, {"alive_count": 17, "total_count": 17})

            deadline = time.monotonic() + 2
            while (
                (
                    not runtime.core_runner.started
                    or not manager.core_running_marks
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertTrue(runtime.core_runner.started)
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertEqual(
                runtime._current_core_runtime_phase(),
                "formal_no_capture",
            )
            self.assertIs(
                manager.core_running_marks[-1]["traffic_capture_active"],
                False,
            )
            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )
            self.assertTrue(manager.effective_states[-1][0])
            self.assertIsNone(runtime.last_error)
            self.assertEqual(runtime._last_tier3_core_sync_marker, runtime._tier3_core_sync_marker())
            self.assertTrue(manager.core_running_marks)
            self.assertTrue(manager.tier3_last_collection_report["startup_gate_satisfied"])
            self.assertFalse(manager.tier3_last_collection_report["startup_mode"])
            self.assertTrue(
                any(mark["core_gate_latency_ms"] is not None for mark in manager.core_running_marks)
            )

    def test_tier3_commit_callback_starts_core_during_startup_before_runtime_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "min_alive_nodes": 3},
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 3
            runtime.manager = manager
            runtime.running = False
            runtime._desired_running = True
            runtime._tier3_required_for_start = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._prime_tier3_pool_before_core_start = lambda: self.fail("startup commit gate must not re-enter Tier3 recovery")
            runtime._attach_manager_runtime_hooks(manager)
            nodes = [
                Node(
                    name=f"startup-committed-{index}",
                    protocol="ss",
                    uri=f"ss://startup-committed-{index}",
                    address=f"startup-committed-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                for index in range(3)
            ]
            manager.runtime_nodes["tier3"] = nodes
            committed_at = time.monotonic()
            manager.on_tier3_pool_committed(nodes, {"alive_count": 3, "total_count": 3})

            deadline = time.monotonic() + 2
            while (
                (
                    not runtime.core_runner.started
                    or not manager.core_running_marks
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            # The gate bounds standby synchronization, not the subsequent
            # explicitly committed FINAL-EXIT admission/readback transaction.
            standby_synchronized_at = time.monotonic()
            self.assertLessEqual((standby_synchronized_at - committed_at) * 1000.0, 250.0)
            self.assertTrue(runtime.core_runner.started)
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertEqual(
                runtime._current_core_runtime_phase(),
                "formal_no_capture",
            )
            self.assertIs(
                manager.core_running_marks[-1]["traffic_capture_active"],
                False,
            )
            self.assertIsNone(manager.effective_states[-1][0])
            self.assertFalse(runtime._initial_egress_activation_deferred)
            self.assertTrue(
                self._commit_deferred_initial_egress_admission(runtime)
            )
            self.assertTrue(
                runtime._wait_for_tier3_egress_activation(timeout=30.0)
            )
            self.assertTrue(manager.effective_states[-1][0])
            self.assertEqual(runtime._last_tier3_core_sync_marker, runtime._tier3_core_sync_marker())
            self.assertTrue(manager.core_running_marks)
            self.assertTrue(
                any(mark["core_gate_latency_ms"] is not None for mark in manager.core_running_marks)
            )

    def test_tier3_commit_callback_ignores_partial_startup_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "min_alive_nodes": 3},
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 3
            runtime.manager = manager
            runtime.running = False
            runtime._tier3_required_for_start = True
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._attach_manager_runtime_hooks(manager)
            nodes = [
                Node(
                    name=f"partial-{index}",
                    protocol="ss",
                    uri=f"ss://partial-{index}",
                    address=f"partial-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                for index in range(2)
            ]
            manager.runtime_nodes["tier3"] = nodes
            manager.on_tier3_pool_committed(nodes, {"alive_count": 2, "total_count": 2})

            time.sleep(0.05)

            self.assertFalse(runtime.core_runner.started)
            self.assertEqual(runtime.traffic_monitor.started, 0)
            self.assertEqual(manager.core_running_marks, [])
            self.assertIsNone(runtime._last_tier3_core_sync_marker)

    def test_tier3_commit_callback_retries_when_new_marker_replaces_failed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "min_alive_nodes": 3},
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 3
            runtime.manager = manager
            runtime.running = True
            runtime.system_proxy.enabled = False
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._attach_manager_runtime_hooks(manager)

            def node(index: int) -> Node:
                return Node(
                    name=f"committed-{index}",
                    protocol="ss",
                    uri=f"ss://committed-{index}",
                    address=f"committed-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            manager.runtime_nodes["tier3"] = [node(index) for index in range(3)]
            failed_marker = runtime._tier3_core_sync_marker()
            runtime._mark_tier3_core_sync_failure(failed_marker)
            nodes = [node(index) for index in range(17)]
            manager.runtime_nodes["tier3"] = nodes
            manager.on_tier3_pool_committed(nodes, {"alive_count": 17, "total_count": 17})

            deadline = time.monotonic() + 2
            while not runtime.core_runner.started and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertTrue(runtime.core_runner.started)
            self.assertNotEqual(runtime._tier3_core_sync_marker(), failed_marker)

    def test_failed_deferred_core_start_is_not_marked_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {"tier3": {"min_collect_interval": 1}}
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.running = True
            runtime.core_runner = FakeLifecycleCoreRunner()
            calls = []

            def fail_start_core():
                calls.append("start")
                raise RuntimeError("transient start failure")

            runtime.start_core = fail_start_core
            manager.refresh_tier3()
            current_marker = runtime._tier3_core_sync_marker()
            runtime._sync_core_config_after_pool_update()
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["start"])
            self.assertIsNone(runtime._last_tier3_core_sync_marker)
            self.assertEqual(runtime._last_failed_tier3_core_sync_marker, current_marker)

            runtime._last_failed_tier3_core_sync_at -= 2
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["start", "start"])

    def test_deferred_tier3_core_start_helper_requires_running_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.running = False
            calls: list[str] = []
            runtime.start_core = lambda: calls.append("start")

            started = runtime._attempt_deferred_tier3_core_start(runtime._tier3_core_sync_marker())

            self.assertFalse(started)
            self.assertEqual(calls, [])
            self.assertIsNone(runtime._last_tier3_core_sync_marker)

    def test_deferred_tier3_core_start_helper_respects_failure_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {"tier3": {"min_collect_interval": 60}}
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.running = True
            marker = runtime._tier3_core_sync_marker()
            runtime._mark_tier3_core_sync_failure(marker)
            calls: list[str] = []
            runtime.start_core = lambda: calls.append("start")

            started = runtime._attempt_deferred_tier3_core_start(marker)

            self.assertFalse(started)
            self.assertEqual(calls, [])
            self.assertIsNone(runtime._last_tier3_core_sync_marker)
            self.assertEqual(runtime._last_failed_tier3_core_sync_marker, marker)

    def test_pool_sync_does_not_reload_while_runtime_is_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_tier3_core_sync_marker = None
            runtime.stop_event.set()
            calls = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}

            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, [])

    def test_tier3_only_warmup_verifies_mixed_port_before_group_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["testing"] = {
                "mihomo_helper": {
                    "probe_url": "https://probe.example/generate_204",
                    "probe_timeout": 4,
                }
            }
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            route_calls = []
            group_calls = []
            runtime._probe_mihomo_mixed_port = lambda: route_calls.append("mixed") or {
                "url": "https://probe.example/generate_204",
                "latency_ms": 12,
            }
            runtime.test_group_delay = lambda group, *, url=None, timeout_ms=5000: group_calls.append((group, url, timeout_ms)) or {"delay": 1}

            runtime._warm_tier3_group_after_core_start()

            self.assertEqual(route_calls, ["mixed"])
            self.assertEqual(group_calls, [])

    def test_tier3_only_warmup_falls_back_to_group_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["testing"] = {
                "mihomo_helper": {
                    "probe_url": "https://probe.example/generate_204",
                    "probe_timeout": 4,
                }
            }
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            calls = []
            runtime._probe_mihomo_mixed_port = lambda: (_ for _ in ()).throw(RuntimeError("route failed"))
            runtime._verify_runtime_final_exit = lambda **_kwargs: (
                (_ for _ in ()).throw(RuntimeError("route failed"))
            )
            runtime.test_group_delay = lambda group, *, url=None, timeout_ms=5000: calls.append((group, url, timeout_ms)) or {"delay": 1}

            runtime._warm_tier3_group_after_core_start()

            self.assertEqual(calls, [("TIER3-AUTO", "https://probe.example/generate_204", 8000)])

    def test_tier3_live_core_repair_reorders_export_without_pruning_main_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            good = Node(
                name="good-tier3",
                protocol="ss",
                uri="ss://good",
                address="good.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            bad = Node(
                name="bad-tier3",
                protocol="ss",
                uri="ss://bad",
                address="bad.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [bad, good]
            runtime.manager = manager
            # The probes carry the names the Core loaded (stable names by default).
            core_name = {node.name: name for name, node in runtime._tier3_proxy_name_pairs()}

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name == core_name["good-tier3"]:
                    return {"delay": 25}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            self.assertTrue(runtime._repair_tier3_export_order_from_core_delays(prune_to_successful=True))

            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["bad-tier3", "good-tier3"])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
            self.assertNotIn("tier3_consecutive_failures", manager.runtime_nodes["tier3"][0].extra)
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["good-tier3", "bad-tier3"],
            )
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["pruned_count"], 0)
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["export_pruned_count"], 0)
            self.assertTrue(
                manager.tier3_last_collection_report["startup_core_repair"][
                    "durable_membership_preserved"
                ]
            )
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["core_failed_count"], 1)
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["retained_failed_count"], 0)
            self.assertEqual(manager.targeted_rechecks[0]["identity"], manager._node_identity(bad))
            self.assertTrue(manager.tier3_last_collection_report["startup_core_repair"]["pruned_to_successful"])

    def test_tier3_tcp_startup_core_probe_export_is_not_capped_by_subscription_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["subscription_server"] = {"tier3_limit": 1}
            runtime.config["testing"] = {"mihomo_helper": {"startup_core_probe_limit": 3}}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.tier3_last_collection_report = {
                "startup_mode": True,
                "validation": {"prelaunch_validation": "tcp"},
            }

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"startup_prelaunch_validation": "tcp"},
                )

            manager.runtime_nodes["tier3"] = [node("one"), node("two"), node("three")]
            runtime.manager = manager

            self.assertEqual(runtime._mihomo_tier3_export_limit(), 3)
            self.assertEqual([node.name for _name, node in runtime._tier3_proxy_name_pairs()], ["one", "two", "three"])

    def test_tier3_live_core_repair_honors_requested_verified_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.background_target_alive = 5
            manager.last_collect_tier3 = 99.0

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            manager.runtime_nodes["tier3"] = [node("bad-tier3"), node("good-one"), node("good-two")]
            runtime.manager = manager
            # The probes carry the names the Core loaded (stable names by default).
            core_name = {node.name: name for name, node in runtime._tier3_proxy_name_pairs()}

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name in {core_name["good-one"], core_name["good-two"]}:
                    return {"delay": 25 if name == core_name["good-one"] else 30}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            self.assertTrue(
                runtime._repair_tier3_export_order_from_core_delays(
                    prune_to_successful=True,
                    target_alive=2,
                )
            )

            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["bad-tier3", "good-one", "good-two"])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["good-one", "good-two", "bad-tier3"],
            )
            report = manager.tier3_last_collection_report["startup_core_repair"]
            self.assertEqual(report["target_verified_count"], 2)
            self.assertEqual(report["working_count"], 2)
            self.assertEqual(report["core_failed_count"], 1)
            self.assertEqual(report["retained_failed_count"], 0)
            self.assertEqual(report["pruned_count"], 0)
            self.assertEqual(report["export_pruned_count"], 0)
            self.assertTrue(report["durable_membership_preserved"])
            self.assertEqual(len(manager.targeted_rechecks), 1)
            self.assertTrue(manager.tier3_last_collection_report["background_expansion_due"])
            self.assertEqual(manager.last_collect_tier3, 0.0)

    def test_tier3_live_core_repair_accepts_minimum_when_soft_target_not_met(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            manager.runtime_nodes["tier3"] = [node("good-one"), node("bad-two")]
            runtime.manager = manager
            # The probes carry the names the Core loaded (stable names by default).
            core_name = {node.name: name for name, node in runtime._tier3_proxy_name_pairs()}

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name == core_name["good-one"]:
                    return {"delay": 25}
                raise RuntimeError("timeout")

            runtime.test_proxy_delay = fake_delay

            self.assertTrue(
                runtime._repair_tier3_export_order_from_core_delays(
                    prune_to_successful=True,
                    target_alive=2,
                    minimum_alive=1,
                )
            )

            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["good-one", "bad-two"])
            self.assertEqual(manager.runtime_nodes["tier3"][1].status, NodeStatus.ALIVE)
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["good-one", "bad-two"],
            )
            report = manager.tier3_last_collection_report["startup_core_repair"]
            self.assertEqual(report["target_verified_count"], 2)
            self.assertEqual(report["minimum_verified_count"], 1)
            self.assertEqual(report["working_count"], 1)
            self.assertEqual(report["core_failed_count"], 1)
            self.assertEqual(report["retained_failed_count"], 0)
            self.assertEqual(report["targeted_recheck_queued_count"], 1)

    def test_tier3_live_core_repair_does_not_apply_pool_eviction_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()

            good = Node(
                name="good-one",
                protocol="ss",
                uri="ss://good-one",
                address="good-one.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            bad = Node(
                name="bad-two",
                protocol="ss",
                uri="ss://bad-two",
                address="bad-two.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
                extra={"tier3_consecutive_failures": 2},
            )
            manager.runtime_nodes["tier3"] = [good, bad]
            runtime.manager = manager
            # The probes carry the names the Core loaded (stable names by default).
            core_name = {node.name: name for name, node in runtime._tier3_proxy_name_pairs()}
            runtime.test_proxy_delay = lambda name, *, url=None, timeout_ms=5000: {"delay": 25} if name == core_name["good-one"] else (_ for _ in ()).throw(RuntimeError("timeout"))

            self.assertTrue(
                runtime._repair_tier3_export_order_from_core_delays(
                    prune_to_successful=True,
                    target_alive=1,
                    minimum_alive=1,
                )
            )

            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["good-one", "bad-two"])
            self.assertEqual(manager.runtime_nodes["tier3"][1].status, NodeStatus.ALIVE)
            self.assertEqual(manager.runtime_nodes["tier3"][1].extra["tier3_consecutive_failures"], 2)
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["good-one", "bad-two"],
            )
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["pruned_count"], 0)
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["export_pruned_count"], 0)
            self.assertEqual(manager.tier3_last_collection_report["startup_core_repair"]["evicted_failed_count"], 0)
            self.assertEqual(len(manager.targeted_rechecks), 1)

    def test_tier3_only_warmup_probe_failure_keeps_initial_core_standby(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.running = True
            runtime._desired_running = True
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._probe_mihomo_mixed_port = lambda: (_ for _ in ()).throw(RuntimeError("route failed"))
            runtime._verify_runtime_final_exit = lambda **_kwargs: (
                (_ for _ in ()).throw(RuntimeError("route failed"))
            )
            runtime.test_group_delay = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("504"))

            status = runtime.start_core()
            runtime._wait_for_tier3_egress_activation()

            self.assertTrue(status["running"])
            self.assertFalse(runtime.core_runner.stopped)
            self.assertFalse(runtime.system_proxy.active)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["bootstrap"])
            self.assertFalse(manager.tier3_last_collection_report["background_expansion_due"])
            self.assertTrue(manager.tier3_last_collection_report["core_standby"])
            self.assertFalse(bool(manager.effective_states[-1][0]))
            self.assertEqual(
                runtime._runtime_egress_readiness.state,
                RuntimeEgressReadinessState.DATA_PLANE_FAILED,
            )

    def test_tier3_same_export_signature_does_not_hot_reload_running_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_tier3_core_sync_marker = runtime._tier3_core_sync_marker()
            calls = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}

            manager.tier3_last_collection_at = "collect-without-export-change"
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, [])

    def test_tier3_same_count_different_export_hot_reloads_running_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.refresh_tier3()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_tier3_core_sync_marker = runtime._tier3_core_sync_marker()
            calls = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="replacement",
                    protocol="ss",
                    uri="ss://replacement",
                    address="replacement.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            manager.tier3_last_collection_at = "collect-2"

            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["reload"])
            self.assertEqual(runtime._last_tier3_core_sync_marker, runtime._tier3_core_sync_marker())

    def test_tier2_export_change_hot_reloads_running_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier2": {"subscription_urls": ["https://tier2.example/sub"]},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier2"] = [
                Node(
                    name="old-tier2",
                    protocol="ss",
                    uri="ss://old-tier2",
                    address="old-tier2.example.com",
                    port=8388,
                    tier="tier2",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="old",
                )
            ]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime._last_tier3_core_sync_marker = runtime._tier3_core_sync_marker()
            runtime._last_core_export_sync_marker = runtime._core_export_sync_marker()
            calls = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}
            manager.runtime_nodes["tier2"] = [
                Node(
                    name="new-tier2",
                    protocol="ss",
                    uri="ss://new-tier2",
                    address="new-tier2.example.com",
                    port=8388,
                    tier="tier2",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="new",
                )
            ]

            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["reload"])
            self.assertEqual(runtime._last_core_export_sync_marker, runtime._core_export_sync_marker())

    def test_full_tier3_collection_report_ends_tcp_prelaunch_export_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.tier3_last_collection_report = {
                "startup_mode": False,
                "validation": {"prelaunch_validation": "full"},
            }
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="cached-startup",
                    protocol="ss",
                    uri="ss://cached",
                    address="cached.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"startup_prelaunch_validation": "tcp"},
                )
            ]
            runtime.manager = manager

            self.assertFalse(runtime._tier3_export_uses_tcp_prelaunch_validation())

    def test_reload_core_config_primes_bootstrap_tier3_before_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"

            result = runtime.reload_core_config(
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertTrue(result["reloaded"])
            self.assertEqual(runner.hot_reloaded, 2)
            self.assertEqual(manager.refreshed, 1)
            self.assertTrue(manager.tier3_last_collection_report["startup_core_repair"]["pruned_to_successful"])
            self.assertEqual(manager.last_collect_tier3, 0.0)
            self.assertEqual(runtime._last_tier3_core_sync_marker, runtime._tier3_core_sync_marker())

    def test_reload_core_config_does_not_reprime_running_verified_tier3_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            expanded = Node(
                name="expanded-good",
                protocol="ss",
                uri="ss://expanded-good",
                address="expanded.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            previous = Node(
                name="previous-good",
                protocol="ss",
                uri="ss://previous-good",
                address="previous.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [expanded]
            runtime.manager = manager
            runtime._last_synced_tier3_nodes = [previous]
            runtime._prime_tier3_pool_before_core_start = lambda: (_ for _ in ()).throw(
                AssertionError("startup priming should not run for an already verified running pool")
            )
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"

            result = runtime.reload_core_config(
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertTrue(result["reloaded"])
            self.assertEqual(runner.hot_reloaded, 2)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["expanded-good"])
            self.assertEqual([node.name for node in runtime._last_synced_tier3_nodes], ["expanded-good"])

    def test_core_reload_context_uses_last_synced_nodes_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            manager = FakeTier3Manager()
            expanded = Node(
                name="expanded-good",
                protocol="ss",
                uri="ss://expanded-good",
                address="expanded.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            previous = Node(
                name="previous-good",
                protocol="ss",
                uri="ss://previous-good",
                address="previous.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [expanded]
            runtime.manager = manager
            runtime._last_synced_tier3_nodes = [previous]

            context = runtime._capture_core_reload_context()

            self.assertEqual(context.runtime_config_path, runtime_config)
            self.assertEqual(context.previous_config_text, "old-config")
            self.assertTrue(context.controller["enabled"])
            self.assertEqual([node.name for node in context.current_tier3_nodes], ["expanded-good"])
            self.assertEqual([node.name for node in context.rollback_tier3_nodes], ["previous-good"])
            self.assertIsNot(context.rollback_tier3_nodes, runtime._last_synced_tier3_nodes)

    def test_core_reload_context_falls_back_to_current_tier3_nodes_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "missing-runtime.yaml"
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            manager = FakeTier3Manager()
            expanded = Node(
                name="expanded-good",
                protocol="ss",
                uri="ss://expanded-good",
                address="expanded.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [expanded]
            runtime.manager = manager

            context = runtime._capture_core_reload_context()

            self.assertEqual(context.runtime_config_path, runtime_config)
            self.assertIsNone(context.previous_config_text)
            self.assertEqual([node.name for node in context.current_tier3_nodes], ["expanded-good"])
            self.assertEqual([node.name for node in context.rollback_tier3_nodes], ["expanded-good"])
            self.assertIsNot(context.rollback_tier3_nodes, context.current_tier3_nodes)

    def test_pruned_primary_tier3_export_helper_rewrites_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="good-tier3",
                    protocol="ss",
                    uri="ss://good-tier3",
                    address="good.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            writes: list[str] = []
            runtime.test_proxy_delay = lambda name, *, url=None, timeout_ms=5000: {"delay": 18}
            runtime.write_mihomo_runtime_config = lambda: writes.append("write") or temp_path / "mihomo-runtime.yaml"

            result = runtime._reload_pruned_primary_tier3_export(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertTrue(result["reloaded"])
            self.assertEqual(runner.hot_reloaded, 1)
            self.assertEqual(writes, ["write"])
            self.assertIn("_tier3_core_repair_plan", result)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["good-tier3"])

    def test_pruned_primary_tier3_export_helper_stops_when_repair_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.manager = FakeTier3Manager()
            runner = FakeLifecycleCoreRunner()
            runner.start()
            writes: list[str] = []
            runtime._repair_tier3_export_order_from_core_delays = lambda **_kwargs: False
            runtime.write_mihomo_runtime_config = lambda: writes.append("write") or temp_path / "mihomo-runtime.yaml"

            result = runtime._reload_pruned_primary_tier3_export(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertFalse(result["stop_sensitive"])
            self.assertEqual(result["reason"], "could not verify exported Bastion Layer node through Mihomo")
            self.assertEqual(writes, [])
            self.assertEqual(runner.hot_reloaded, 0)

    def test_pruned_primary_tier3_export_helper_reports_reload_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="good-tier3",
                    protocol="ss",
                    uri="ss://good-tier3",
                    address="good.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            reload_calls: list[str] = []
            runtime.test_proxy_delay = lambda name, *, url=None, timeout_ms=5000: {"delay": 18}
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runner.hot_reload = lambda **_kwargs: reload_calls.append("reload") or {
                "reloaded": False,
                "reason": "HTTP Error 400: Bad Request",
            }

            result = runtime._reload_pruned_primary_tier3_export(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["stop_sensitive"])
            self.assertEqual(
                result["reason"],
                "hot reload after applying Bastion Layer core verification order failed: HTTP Error 400: Bad Request",
            )
            self.assertEqual(reload_calls, ["reload"])

    def test_pruned_primary_tier3_export_reload_failure_does_not_commit_repair_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            manager.runtime_nodes["tier3"] = [
                node("bad-one"),
                node("bad-two"),
                node("good-one"),
                node("good-two"),
                node("good-three"),
                node("good-four"),
            ]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if str(name).startswith("good-"):
                    return {"delay": 18}
                raise RuntimeError("core probe failed")

            runtime.test_proxy_delay = fake_delay
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runner.hot_reload = lambda **_kwargs: {"reloaded": False, "reason": "HTTP Error 400: Bad Request"}

            result = runtime._reload_pruned_primary_tier3_export(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertEqual(
                [(node.name, node.status) for node in manager.runtime_nodes["tier3"]],
                [
                    ("bad-one", NodeStatus.ALIVE),
                    ("bad-two", NodeStatus.ALIVE),
                    ("good-one", NodeStatus.ALIVE),
                    ("good-two", NodeStatus.ALIVE),
                    ("good-three", NodeStatus.ALIVE),
                    ("good-four", NodeStatus.ALIVE),
                ],
            )
            self.assertEqual(runtime._tier3_core_export_override_snapshot(), [])
            self.assertEqual(manager.tier3_last_collection_report["core_audit"]["core_verified_count"], 4)

    def test_pruned_primary_tier3_export_write_exception_clears_temporary_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="good-tier3",
                    protocol="ss",
                    uri="ss://good-tier3",
                    address="good.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.test_proxy_delay = lambda name, *, url=None, timeout_ms=5000: {"delay": 18}
            runtime.write_mihomo_runtime_config = lambda: (_ for _ in ()).throw(RuntimeError("write failed"))

            result = runtime._reload_pruned_primary_tier3_export(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["stop_sensitive"])
            self.assertEqual(runtime._tier3_core_export_override_snapshot(), [])

    def test_runtime_tier3_egress_audit_delegates_without_reload_or_inventory_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.background_target_alive = 25
            good = Node(
                name="good-next",
                protocol="ss",
                uri="ss://good-next",
                address="good-next.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            bad = Node(
                name="bad-current",
                protocol="ss",
                uri="ss://bad-current",
                address="bad-current.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [bad, good]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")
            manager.tier3_traffic_capture_active = True
            writes: list[str] = []
            runtime.write_mihomo_runtime_config = lambda: writes.append("write") or temp_path / "mihomo-runtime.yaml"
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                            "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER3-AUTO", "all": ["TIER3-AUTO"]},
                            "TIER3-AUTO": {"type": "Fallback", "now": "bad-current", "all": ["bad-current", "good-next"]},
                            "bad-current": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )
            original_proxy_delay = runtime.api_client.proxy_delay

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name == "bad-current":
                    raise RuntimeError("bad current failed")
                return original_proxy_delay(name, url=url, timeout_ms=timeout_ms)

            runtime.api_client.proxy_delay = fake_delay

            bad_identity = manager._node_identity(bad)
            self.assertFalse(
                runtime._record_runtime_audit_export_failure(
                    bad_identity,
                    definitive=True,
                    reason="tier3_runtime_leaf_failure",
                )
            )
            submitted: list[tuple[str, str]] = []
            runtime._submit_current_route_probe_failure = (
                lambda reason, *, source="runtime_probe": submitted.append(
                    (str(reason), str(source))
                )
                or True
            )

            result = runtime.audit_tier3_runtime_egress(reason="overview exit check failed")

            self.assertTrue(result["attempted"])
            self.assertFalse(result["failover"])
            self.assertFalse(result["connectivity_recovered"])
            self.assertTrue(result["verification_pending"])
            self.assertFalse(result["reloaded"])
            self.assertEqual(
                submitted,
                [("overview exit check failed", "tier3_runtime_egress")],
            )
            self.assertEqual(writes, [])
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["bad-current", "good-next"])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
            self.assertNotIn(
                bad_identity,
                runtime._runtime_audit_export_excluded_snapshot(),
            )
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["bad-current", "good-next"],
            )

    def test_final_exit_delay_failure_submits_global_async_confirmation(self) -> None:
        class FakeDelayClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def proxies(self):
                return {
                    "proxies": {
                        "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                        "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER3-AUTO", "all": ["TIER3-AUTO"]},
                        "TIER3-AUTO": {"type": "Fallback", "now": "bad-current", "all": ["bad-current", "good-next"]},
                        "bad-current": {"type": "Shadowsocks"},
                    }
                }

            def proxy_delay(self, name, *, url=None, timeout_ms=5000):
                self.calls.append(str(name))
                if name == "FINAL-EXIT" and self.calls.count("FINAL-EXIT") <= 2:
                    raise RuntimeError("current final exit failed")
                if name == "bad-current":
                    raise RuntimeError("bad node failed")
                return {"delay": 21, "name": name}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            del runtime.test_proxy_delay
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True},
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="bad-current",
                    protocol="ss",
                    uri="ss://bad-current",
                    address="bad-current.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                ),
                Node(
                    name="good-next",
                    protocol="ss",
                    uri="ss://good-next",
                    address="good-next.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                ),
            ]
            runtime.manager = manager
            runtime.api_client = FakeDelayClient()
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("formal_capture")
            manager.tier3_traffic_capture_active = True
            submissions: list[tuple[str, str]] = []
            runtime._submit_current_route_probe_failure = (
                lambda reason, *, source="runtime_probe", **_authority: (
                    submissions.append((str(reason), str(source))) or True
                )
            )

            with self.assertRaisesRegex(RuntimeError, "current final exit failed"):
                runtime.test_proxy_delay("FINAL-EXIT", timeout_ms=5000)
            with self.assertRaisesRegex(RuntimeError, "current final exit failed"):
                runtime.test_proxy_delay("FINAL-EXIT", timeout_ms=5000)

            self.assertEqual(runtime.api_client.calls.count("FINAL-EXIT"), 2)
            self.assertEqual(
                [source for _reason, source in submissions],
                ["proxy_delay", "proxy_delay"],
            )
            # The caller gets the original controller failure immediately;
            # the single global coordinator owns any later probe/reload.
            self.assertEqual([node.name for node in runtime._tier3_nodes_for_core_export()], ["bad-current", "good-next"])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
            self.assertEqual(runner.hot_reloaded, 0)

    def _legacy_runtime_audit_replacement_failure_submits_frozen_binding_after_route_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeRuntimeAuditManager()

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example",
                    port=8388,
                    tier="tier1",
                    method="aes-256-gcm",
                    password="pass",
                    status=NodeStatus.ALIVE,
                )

            node_a = node("audit-a")
            node_b = node("audit-b")
            node_c = node("audit-c")
            manager.runtime_nodes["tier1"] = [node_a, node_b, node_c]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner

            def authority_for(
                managed_node: Node,
                route_epoch: int,
            ) -> tuple[
                CoreLoadedRouteSnapshot,
                dict[str, object],
                RuntimeRouteBinding,
                dict[str, object],
            ]:
                leaf = managed_node.name
                ref = RuntimeNodeRef(
                    tier="tier1",
                    pool_kind="tier1",
                    identity=manager._node_identity(managed_node),
                    proxy_name=leaf,
                )
                controller_payload: dict[str, object] = {
                    "proxies": {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "PROXY-FALLBACK",
                            "all": ["PROXY-FALLBACK", "REJECT"],
                        },
                        "PROXY-FALLBACK": {
                            "type": "Fallback",
                            "now": "TIER1-AUTO",
                            "all": ["TIER1-AUTO", "REJECT"],
                        },
                        "TIER1-AUTO": {
                            "type": "Fallback",
                            "now": leaf,
                            "all": [leaf, "REJECT"],
                        },
                        leaf: {"type": "Shadowsocks"},
                        "REJECT": {"type": "Reject"},
                    }
                }
                snapshot_payload: dict[str, object] = {
                    "proxies": controller_payload,
                    "proxy_map": {
                        leaf: {
                            "tier": ref.tier,
                            "pool_kind": ref.pool_kind,
                            "identity": ref.identity,
                        }
                    },
                }
                snapshot = CoreLoadedRouteSnapshot(
                    process_pid=4242,
                    process_generation=1,
                    core_generation=1,
                    semantic_digest=f"audit-authority-{leaf}",
                    route_epoch=route_epoch,
                    network_context_epoch=0,
                    group_graph=(
                        ("FINAL-EXIT", ("PROXY-FALLBACK", "REJECT")),
                        ("PROXY-FALLBACK", ("TIER1-AUTO", "REJECT")),
                        ("TIER1-AUTO", (leaf, "REJECT")),
                    ),
                    group_states=(
                        CoreLoadedGroupState(
                            name="FINAL-EXIT",
                            type_name="Selector",
                            members=("PROXY-FALLBACK", "REJECT"),
                            now="PROXY-FALLBACK",
                        ),
                        CoreLoadedGroupState(
                            name="PROXY-FALLBACK",
                            type_name="Fallback",
                            members=("TIER1-AUTO", "REJECT"),
                            now="TIER1-AUTO",
                        ),
                        CoreLoadedGroupState(
                            name="TIER1-AUTO",
                            type_name="Fallback",
                            members=(leaf, "REJECT"),
                            now=leaf,
                        ),
                    ),
                    proxy_nodes=(ref,),
                    loaded_at_monotonic=time.monotonic(),
                )
                binding = runtime._runtime_route_binding_for_outbound(
                    snapshot,
                    snapshot_payload,
                    "FINAL-EXIT",
                )
                if binding is None:
                    raise AssertionError(f"fixture route did not bind {leaf}")
                return snapshot, snapshot_payload, binding, controller_payload

            snapshot_a, _payload_a, binding_a, controller_a = authority_for(
                node_a,
                10,
            )
            snapshot_b, payload_b, binding_b, _controller_b = authority_for(
                node_b,
                11,
            )
            snapshot_c, payload_c, binding_c, controller_c = authority_for(
                node_c,
                12,
            )
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_epoch = snapshot_b.route_epoch
                runtime._runtime_route_snapshot = snapshot_b
                runtime._runtime_route_snapshot_payload = payload_b
            runtime._runtime_egress_coordinator.set_current_snapshot(snapshot_b)
            runtime.api_client = FakeRuntimeAuditClient(
                [controller_a, controller_c]
            )
            runtime._hot_reload_runtime_audit_overlay = lambda **_kwargs: {
                "reloaded": True,
            }
            authority_reads: list[dict[str, object]] = []

            def read_replacement_authority(**kwargs):
                authority_reads.append(dict(kwargs))
                return binding_b, snapshot_b, payload_b

            runtime._current_runtime_failure_binding_state = (
                read_replacement_authority
            )
            probe_entered = threading.Event()
            release_probe = threading.Event()

            def fail_replacement_probe(**_kwargs):
                probe_entered.set()
                if not release_probe.wait(timeout=2.0):
                    raise AssertionError("test did not release replacement probe")
                raise RuntimeError("replacement B 204 failed")

            runtime._probe_mihomo_mixed_port_once = fail_replacement_probe
            submissions: list[dict[str, object]] = []

            def submit_failure(
                reason,
                *,
                source="runtime_probe",
                binding_state=None,
                binding_captured=False,
            ):
                current_snapshot = (
                    runtime._runtime_egress_coordinator.current_route_snapshot()
                )
                submissions.append(
                    {
                        "reason": str(reason),
                        "source": str(source),
                        "binding_state": binding_state,
                        "binding_captured": bool(binding_captured),
                        "current_snapshot_key": (
                            tuple(current_snapshot.snapshot_key)
                            if current_snapshot is not None
                            else None
                        ),
                    }
                )
                return True

            runtime._submit_current_route_probe_failure = submit_failure
            audit_results: list[dict[str, object]] = []
            audit_errors: list[BaseException] = []

            def run_audit() -> None:
                try:
                    audit_results.append(
                        runtime.audit_runtime_egress(
                            "canonical A failed",
                            confirmed_transport_failure=True,
                            canonical_health_applied=True,
                            canonical_failed_binding=binding_a,
                            bounded_probe_seconds=1.0,
                        )
                    )
                except BaseException as exc:  # pragma: no cover - assertion aid
                    audit_errors.append(exc)

            audit_thread = threading.Thread(target=run_audit, daemon=True)
            audit_thread.start()
            self.assertTrue(probe_entered.wait(timeout=1.0))

            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_epoch = snapshot_c.route_epoch
                runtime._runtime_route_snapshot = snapshot_c
                runtime._runtime_route_snapshot_payload = payload_c
            runtime._runtime_egress_coordinator.set_current_snapshot(snapshot_c)
            release_probe.set()
            audit_thread.join(timeout=2.0)

            self.assertFalse(audit_thread.is_alive())
            self.assertEqual(audit_errors, [])
            self.assertEqual(len(audit_results), 1)
            self.assertEqual(len(authority_reads), 1)
            self.assertTrue(authority_reads[0]["refresh"])
            self.assertEqual(len(submissions), 1)
            submission = submissions[0]
            self.assertEqual(submission["source"], "replacement_verification")
            self.assertTrue(submission["binding_captured"])
            captured_binding, captured_snapshot, captured_payload = submission[
                "binding_state"
            ]
            self.assertIs(captured_binding, binding_b)
            self.assertIs(captured_snapshot, snapshot_b)
            self.assertIs(captured_payload, payload_b)
            self.assertEqual(captured_binding.node.key, binding_b.node.key)
            self.assertNotEqual(captured_binding.node.key, binding_c.node.key)
            self.assertEqual(
                submission["current_snapshot_key"],
                tuple(snapshot_c.snapshot_key),
            )
            self.assertIn(
                "independent binding confirmation",
                str(audit_results[0]["reason"]),
            )

    def _legacy_runtime_audit_switches_pinned_select_and_verifies_final_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            bad = Node(name="bad-entry", protocol="ss", uri="ss://bad", address="bad.example", port=8388, tier="tier1", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
            good = Node(name="good-entry", protocol="ss", uri="ss://good", address="good.example", port=8388, tier="tier1", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier1"] = [bad, good]
            manager.fail_identities.add(manager._node_identity(bad))
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                            "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER1-SELECT", "all": ["TIER1-SELECT"]},
                            "TIER1-SELECT": {"type": "Selector", "now": "bad-entry", "all": ["TIER1-AUTO", "bad-entry", "good-entry", "DIRECT"]},
                            "bad-entry": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )
            queued_rechecks: list[str] = []
            runtime._queue_incremental_egress_recheck = lambda: queued_rechecks.append("identity")

            result = runtime.audit_runtime_egress("final failed")

            self.assertTrue(result["post_switch_verified"])
            self.assertIn("post_switch_connectivity_verified", result, result)
            self.assertTrue(result["post_switch_connectivity_verified"])
            self.assertTrue(result["effective_egress_ok"])
            self.assertEqual(runtime.api_client.select_calls, [("TIER1-SELECT", "TIER1-AUTO")])
            self.assertEqual(queued_rechecks, [])
            self.assertNotIn("DIRECT", runtime.api_client.delay_calls)
            self.assertEqual(manager.marked_failures, [])
            self.assertEqual(manager.runtime_nodes["tier1"][0].status, NodeStatus.ALIVE)

    def _legacy_runtime_audit_skips_bad_auto_leaf_and_selects_next_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            bad = Node(name="edge-ss", protocol="ss", uri="ss://bad", address="bad.example", port=8388, tier="tier1", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
            good = Node(name="edge-vmess", protocol="vmess", uri="vmess://good", address="good.example", port=443, tier="tier1", uuid="11111111-1111-1111-1111-111111111111", status=NodeStatus.ALIVE)
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier1"] = [bad, good]
            manager.fail_identities.add(manager._node_identity(bad))
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                            "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER1-SELECT", "all": ["TIER1-SELECT"]},
                            "TIER1-SELECT": {"type": "Selector", "now": "edge-select", "all": ["TIER1-AUTO", "edge-select"]},
                            "edge-select": {"type": "Selector", "now": "edge-auto", "all": ["edge-auto", "edge-ss", "edge-vmess", "DIRECT"]},
                            "edge-auto": {"type": "Fallback", "now": "edge-ss", "all": ["edge-ss", "edge-vmess"]},
                            "edge-ss": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )

            result = runtime.audit_runtime_egress("final failed")

            self.assertTrue(result["post_switch_verified"])
            self.assertTrue(result["post_switch_connectivity_verified"])
            self.assertEqual(runtime.api_client.select_calls, [("edge-select", "edge-vmess")])

    def _legacy_runtime_audit_overlay_never_claims_recovery_when_reload_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(name="only-entry", protocol="ss", uri="ss://bad", address="bad.example", port=8388, tier="tier2", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier2"] = [node]
            def deterministic_failure(*_args, **_kwargs):
                tested = node.clone()
                tested.status = NodeStatus.DEAD
                tested.suspicious_reason = "deterministic targeted failure"
                return tested

            manager._test_runtime_node_by_identity = deterministic_failure
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                            "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER2-AUTO", "all": ["TIER2-AUTO"]},
                            "TIER2-AUTO": {"type": "Fallback", "now": "only-entry", "all": ["only-entry"]},
                            "only-entry": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )

            first = runtime.audit_runtime_egress("final failed")

            identity = manager._node_identity(node)
            self.assertFalse(first["post_switch_verified"])
            self.assertFalse(first["confirmed_failure"])
            self.assertIn("remains pending", first["reason"])
            self.assertEqual(runtime.core_runner.hot_reloaded, 0)
            self.assertNotIn(
                identity,
                runtime._runtime_audit_export_excluded_snapshot(),
            )
            self.assertEqual(
                runtime._runtime_audit_deterministic_failures[identity]["count"],
                1,
            )
            runtime._runtime_route_audit_cooldowns.clear()

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["post_switch_verified"])
            self.assertFalse(result.get("post_switch_connectivity_verified", False))
            self.assertIn("reload was blocked", result["reason"])
            self.assertEqual(runtime.core_runner.hot_reloaded, 0)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier2"]], ["only-entry"])
            self.assertIn(identity, runtime._runtime_audit_export_excluded_snapshot())

    def test_identity_transport_rotation_excludes_leaf_without_demoting_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(
                name="transport-failed",
                protocol="ss",
                uri="ss://transport-failed",
                address="transport-failed.example",
                port=8388,
                tier="tier1",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier1"] = [node]
            manager.effective_egress_ok = True
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.write_mihomo_runtime_config = lambda: temp_path / "mihomo-runtime.yaml"
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {
                                "type": "Selector",
                                "now": "PROXY-FALLBACK",
                                "all": ["PROXY-FALLBACK"],
                            },
                            "PROXY-FALLBACK": {
                                "type": "Fallback",
                                "now": "TIER1-AUTO",
                                "all": ["TIER1-AUTO"],
                            },
                            "TIER1-AUTO": {
                                "type": "Fallback",
                                "now": "transport-failed",
                                "all": ["transport-failed"],
                            },
                            "transport-failed": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )

            first = runtime.audit_runtime_egress(
                "identity TLS EOF",
                trigger="exit_identity_transport",
                confirmed_transport_failure=True,
                max_steps=1,
                exclusion_ttl_seconds=120,
            )

            identity = manager._node_identity(node)
            self.assertFalse(first["attempted"])
            self.assertTrue(first["identity_diagnostic_only"])
            self.assertNotIn(
                identity,
                runtime._runtime_audit_export_excluded_snapshot(),
            )
            runtime._runtime_route_audit_cooldowns.clear()

            result = runtime.audit_runtime_egress(
                "identity TLS EOF",
                trigger="exit_identity_transport",
                confirmed_transport_failure=True,
                max_steps=1,
                exclusion_ttl_seconds=120,
            )

            self.assertFalse(result["attempted"])
            self.assertTrue(result["identity_diagnostic_only"])
            self.assertTrue(result["effective_egress_ok"])
            self.assertEqual(manager.runtime_nodes["tier1"][0].status, NodeStatus.ALIVE)
            self.assertEqual(manager.marked_failures, [])
            self.assertNotIn(
                identity,
                runtime._runtime_audit_export_excluded_snapshot(),
            )
            self.assertEqual(runtime.core_runner.hot_reloaded, 0)

    def test_tier3_runtime_readiness_failure_does_not_mutate_pool_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            node = Node(
                name="tier3-runtime-leaf",
                protocol="ss",
                uri="ss://tier3-runtime-leaf",
                address="tier3-runtime-leaf.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime._probe_proxy_delay_with_urls = lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(RuntimeError("deterministic leaf failure"))
            )
            leaf_info = {
                "tier": "tier3",
                "identity": manager._node_identity(node),
                "proxy_name": node.name,
                "node": node,
            }

            healthy, tested = runtime._confirm_runtime_leaf(
                leaf_info,
                reason="runtime readiness failed",
                route_marker="fixture",
            )

            self.assertFalse(healthy)
            self.assertIsNone(tested)
            self.assertEqual(manager.marked_failures, [])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)

    def test_identity_failure_is_diagnostic_only_when_neutral_204_is_ok(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.system_proxy.enabled = False
            manager = FakeRuntimeAuditManager()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            manager.effective_egress_ok = True
            runtime._set_core_runtime_phase("formal_capture")
            runtime._set_effective_egress_state(True, "")
            self._install_manual_validation_authority(runtime)
            runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
            unavailable = {
                "state": "partial",
                "identity": {
                    family: {
                        "state": "unavailable",
                        "ip": None,
                        "evidence": [
                            {
                                "attempts": [
                                    {
                                        "status": "error",
                                        "error": "TLS unexpected EOF",
                                    }
                                ]
                            }
                        ],
                    }
                    for family in ("ipv4", "ipv6")
                },
                "route": {
                    "confirmed_before": True,
                    "confirmed_after": True,
                    "stable_during_check": True,
                },
                "diagnostics": {},
            }

            class IdentityService:
                def check(self, **_kwargs):
                    return copy.deepcopy(unavailable)

            runtime.exit_quality_service = IdentityService()
            audits: list[dict[str, object]] = []

            def recover_route(**kwargs):
                audits.append(dict(kwargs))
                return {
                    "attempted": True,
                    "switch_actions": [{"type": "select_proxy"}],
                    "post_switch_connectivity_verified": True,
                    "effective_egress_ok": True,
                    "reason": "FINAL-EXIT 204 connectivity recovered",
                }

            runtime.audit_runtime_egress = recover_route
            runtime._runtime_worker_token = lambda: None
            runtime._manual_current_exit_quality_contracts = lambda _session: (
                ManualDataPlaneProbeResult.https_204("neutral-a"),
                ManualDataPlaneProbeResult.https_204("neutral-b"),
            )

            result = runtime.check_exit_quality(
                proxy_url=None,
                request_id="identity-exhausted-204-ok",
            )

            self.assertEqual(audits, [])
            self.assertNotIn("egress_recovery", result["diagnostics"])
            self.assertEqual(
                result["diagnostics"]["neutral_connectivity"]["state"],
                "ok",
            )
            self.assertTrue(result["diagnostics"]["identity_pending"])
            self.assertTrue(manager.effective_states[-1][0])

    def test_neutral_204_failure_without_backup_does_not_start_identity_or_demote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            runtime.config["client"]["egress_recovery"] = {"max_rotations": 0}
            manager = FakeRuntimeAuditManager()
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            manager.effective_egress_ok = True
            runtime._set_core_runtime_phase("formal_capture")
            runtime._set_effective_egress_state(True, "")
            self._install_manual_validation_authority(runtime)
            runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
            unavailable = {
                "state": "partial",
                "identity": {
                    "ipv4": {"state": "unavailable", "ip": None},
                    "ipv6": {"state": "unavailable", "ip": None},
                },
                "route": {
                    "confirmed_before": True,
                    "confirmed_after": True,
                    "stable_during_check": True,
                },
                "diagnostics": {},
            }

            identity_calls: list[str] = []

            class IdentityService:
                def check(self, **_kwargs):
                    identity_calls.append("identity")
                    return copy.deepcopy(unavailable)

            runtime.exit_quality_service = IdentityService()
            runtime._probe_mihomo_mixed_port = lambda: (_ for _ in ()).throw(
                RuntimeError("neutral 204 failed")
            )
            runtime._probe_mihomo_mixed_port_once = lambda **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("neutral 204 failed"))
            runtime._runtime_worker_token = lambda: None
            runtime._manual_current_exit_quality_contracts = lambda _session: (
                ManualDataPlaneProbeResult.transport_failure(
                    "neutral-a",
                    ManualProbeFailureKind.TIMEOUT,
                ),
                ManualDataPlaneProbeResult.transport_failure(
                    "neutral-b",
                    ManualProbeFailureKind.TIMEOUT,
                ),
            )
            submitted: list[tuple[str, str]] = []
            runtime._submit_current_route_probe_failure = (
                lambda reason, *, source="runtime_probe": submitted.append(
                    (str(reason), str(source))
                )
                or True
            )

            result = runtime.check_exit_quality(
                proxy_url=None,
                request_id="identity-partial-204-failed",
            )

            self.assertEqual(
                result["diagnostics"]["neutral_connectivity"]["state"],
                "failed",
            )
            self.assertFalse(result["diagnostics"]["identity_pending"])
            self.assertEqual(submitted, [])
            self.assertEqual(identity_calls, [])
            self.assertTrue(manager.effective_states[-1][0])

    def test_runtime_source_transport_failure_queues_global_coordinator_not_identity_or_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeRuntimeAuditManager()
            manager.effective_egress_ok = True
            runtime.manager = manager
            identity_calls: list[str] = []
            runtime._verify_final_exit_identity = lambda **_kwargs: identity_calls.append(
                "identity"
            )
            audits: list[dict[str, object]] = []

            def recover_route(**kwargs):
                audits.append(dict(kwargs))
                return {
                    "post_switch_connectivity_verified": True,
                    "effective_egress_ok": True,
                    "reason": "FINAL-EXIT 204 connectivity recovered",
                }

            runtime.audit_runtime_egress = recover_route
            submissions: list[tuple[str, str]] = []
            runtime._submit_current_route_probe_failure = (
                lambda reason, *, source="runtime_probe": (
                    submissions.append((str(reason), str(source))) or True
                )
            )

            result = runtime._recover_runtime_source_transport_failure(
                {"reason": "source TLS EOF"}
            )

            self.assertFalse(result["recovered"])
            self.assertTrue(result["queued"])
            self.assertEqual(identity_calls, [])
            self.assertEqual(audits, [])
            self.assertEqual(submissions, [("source TLS EOF", "runtime_source_transport")])

    def test_runtime_audit_export_overlay_releases_when_node_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(name="only-entry", protocol="ss", uri="ss://bad", address="bad.example", port=8388, tier="tier2", method="aes-256-gcm", password="pass")
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier2"] = [node]
            runtime.manager = manager
            identity = manager._node_identity(node)
            runtime._exclude_runtime_audit_export_identity(identity)
            recovered = node.clone()
            recovered.status = NodeStatus.ALIVE
            recovered.latency_ms = 9
            recovered.last_checked_at = "2026-08-12T00:00:01Z"
            manager.runtime_nodes["tier2"] = [recovered]
            with runtime._runtime_audit_export_overlay_lock:
                runtime._runtime_audit_export_excluded_identities[identity]["excluded_at_monotonic"] = time.monotonic() - 10

            self.assertNotIn(identity, runtime._runtime_audit_export_excluded_snapshot())

    def test_confirmed_runtime_overlay_does_not_recover_when_ttl_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(name="only-entry", protocol="ss", uri="ss://bad", address="bad.example", port=8388, tier="tier2", method="aes-256-gcm", password="pass", status=NodeStatus.SUSPICIOUS)
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier2"] = [node]
            runtime.manager = manager
            identity = manager._node_identity(node)
            runtime._exclude_runtime_audit_export_identity(
                identity,
                reason="confirmed_runtime_failure",
                release_on_healthy=False,
                require_health_recheck=True,
            )
            with runtime._runtime_audit_export_overlay_lock:
                runtime._runtime_audit_export_excluded_identities[identity]["expires_at_monotonic"] = time.monotonic() - 1

            self.assertIn(identity, runtime._runtime_audit_export_excluded_snapshot())

    def test_runtime_audit_export_overlay_recovery_syncs_running_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(
                name="only-entry",
                protocol="ss",
                uri="ss://bad",
                address="bad.example",
                port=8388,
                tier="tier2",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.SUSPICIOUS,
            )
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier2"] = [node]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            identity = manager._node_identity(node)
            runtime._exclude_runtime_audit_export_identity(identity)
            with runtime._runtime_audit_export_overlay_lock:
                runtime._runtime_audit_export_excluded_identities[identity]["excluded_at_monotonic"] = time.monotonic() - 10
            runtime._last_core_export_sync_marker = runtime._core_export_sync_marker()
            recovered = node.clone()
            recovered.status = NodeStatus.ALIVE
            recovered.latency_ms = 7
            recovered.last_checked_at = "2026-08-12T00:00:02Z"
            manager.runtime_nodes["tier2"] = [recovered]
            calls: list[str] = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}

            runtime._sync_core_config_after_pool_update()

            self.assertEqual(calls, ["reload"])
            self.assertNotIn(identity, runtime._runtime_audit_export_excluded_snapshot())

    def test_pool_metadata_cannot_clear_incident_bound_runtime_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(
                name="tier3-entry",
                protocol="ss",
                uri="ss://tier3-entry",
                address="tier3.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            identity = manager._node_identity(node)
            runtime._exclude_runtime_audit_export_identity(
                identity,
                reason="core_repair_pending_recheck",
                release_on_healthy=False,
            )
            with runtime._runtime_audit_export_overlay_lock:
                runtime._runtime_audit_export_excluded_identities[identity]["excluded_at_monotonic"] = time.monotonic() - 10
            self.assertIn(identity, runtime._runtime_audit_export_excluded_snapshot())
            calls: list[str] = []
            runtime.reload_core_config = lambda **_kwargs: calls.append("reload") or {"reloaded": True}
            runtime._last_core_export_sync_marker = runtime._core_export_sync_marker()

            runtime._on_tier3_pool_committed(
                manager.runtime_nodes["tier3"],
                {
                    "alive_count": 1,
                    "total_count": 1,
                    "targeted_recheck_recovered_identities": [identity],
                },
            )

            self.assertIn(identity, runtime._runtime_audit_export_excluded_snapshot())
            time.sleep(0.05)
            self.assertEqual(calls, [])

    def test_private_tier3_membership_retains_unhealthy_node_for_core_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)

            class PublicPoolManager(FakeTier3Manager):
                def get_nodes_for_subscription(
                    self,
                    tier_id: str,
                    limit: int | None = None,
                ):
                    nodes = list(self.runtime_nodes.get(tier_id, []))
                    return nodes[:limit] if limit else nodes

            manager = PublicPoolManager(collect_alive=False)
            retained = Node(
                name="retained-a",
                protocol="ss",
                uri="ss://retained-a",
                address="retained-a.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.DEAD,
            )
            manager.runtime_nodes["tier3"] = [retained]
            runtime.manager = manager

            dead_marker = runtime._tier3_core_sync_marker()
            self.assertTrue(dead_marker)
            self.assertEqual(
                [node.name for node in manager.get_nodes_for_subscription("tier3")],
                ["retained-a"],
            )
            self.assertEqual(
                [node.name for node in runtime._tier3_nodes_for_core_export()],
                ["retained-a"],
            )
            self.assertEqual(
                [
                    node.name
                    for node in runtime._manager_for_mihomo_runtime_config().get_nodes_for_subscription(
                        "tier3"
                    )
                ],
                ["retained-a"],
            )

            recovered = retained.clone()
            recovered.status = NodeStatus.ALIVE
            manager.runtime_nodes["tier3"] = [recovered]
            recovered_marker = runtime._tier3_core_sync_marker()

            self.assertEqual(dead_marker, recovered_marker)
            self.assertEqual(
                [node.name for node in manager.get_nodes_for_subscription("tier3")],
                ["retained-a"],
            )
            self.assertEqual(
                [
                    node.name
                    for node in runtime._manager_for_mihomo_runtime_config().get_nodes_for_subscription(
                        "tier3"
                    )
                ],
                ["retained-a"],
            )

    def test_stopped_core_restarts_when_same_retained_identity_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 1
            node = Node(
                name="same-a",
                protocol="ss",
                uri="ss://same-a",
                address="same-a.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.traffic_monitor = FakeTrafficMonitor()
            runtime._set_core_runtime_phase("formal_capture")
            loaded_marker = runtime._tier3_core_sync_marker()
            runtime._last_tier3_core_sync_marker = loaded_marker

            runtime.stop_core(actor="health", reason="all candidates dead")
            self.assertIsNone(runtime._last_tier3_core_sync_marker)

            dead = node.clone()
            dead.status = NodeStatus.DEAD
            manager.runtime_nodes["tier3"] = [dead]
            self.assertEqual(runtime._tier3_core_sync_marker(), loaded_marker)

            recovered = dead.clone()
            recovered.status = NodeStatus.ALIVE
            manager.runtime_nodes["tier3"] = [recovered]
            recovered_marker = runtime._tier3_core_sync_marker()
            self.assertEqual(recovered_marker, loaded_marker)
            # Preserve the historical loaded marker to exercise the exact
            # same-identity recovery that previously suppressed restart.
            runtime._last_tier3_core_sync_marker = loaded_marker
            starts: list[str] = []
            runtime._start_core_from_committed_tier3_pool = (
                lambda: starts.append("start") or {"running": True}
            )

            started = runtime._attempt_deferred_tier3_core_start(
                recovered_marker,
                committed_pool_only=True,
            )

            self.assertTrue(started)
            self.assertEqual(starts, ["start"])

    def test_bootstrap_reloads_first_direct_source_node_before_proxy_source_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            manager.min_alive_nodes = 2
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime._set_core_runtime_phase("bootstrap")
            runtime._tier3_pool_sync_debounce_seconds = 0.2
            runtime._last_core_export_sync_marker = runtime._core_export_sync_marker()
            reloads: list[str] = []
            runtime.reload_core_config = lambda **_kwargs: reloads.append(
                "bootstrap-reload"
            ) or {"reloaded": True}

            first = Node(
                name="direct-source-node",
                protocol="ss",
                uri="ss://direct-source-node",
                address="direct-source-node.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [first]
            runtime._on_tier3_pool_committed(
                [first],
                {"admission_identity": "direct-source"},
            )
            bootstrap_proxy = runtime._runtime_source_fetch_proxy_provider(
                "bootstrap"
            )
            self.assertEqual(bootstrap_proxy, "http://127.0.0.1:7890")
            # Provider acknowledgement is synchronous: the next source wave
            # cannot leave until the first node is in Mihomo's bootstrap view.
            self.assertEqual(reloads, ["bootstrap-reload"])
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with runtime._tier3_pool_sync_lock:
                    running = runtime._tier3_pool_sync_worker_running
                if not running:
                    break
                time.sleep(0.01)
            self.assertEqual(reloads, ["bootstrap-reload"])

            second = Node(
                name="proxy-source-node",
                protocol="ss",
                uri="ss://proxy-source-node",
                address="proxy-source-node.example",
                port=8389,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager.runtime_nodes["tier3"] = [first, second]
            gates: list[str] = []
            runtime._queue_tier3_core_gate_attempt = lambda: gates.append(
                "promote"
            ) or True
            runtime._on_tier3_pool_committed(
                [first, second],
                {"admission_identity": "proxy-source"},
            )

            self.assertEqual(gates, ["promote"])

    def test_terminal_tier3_startup_recovers_when_primary_node_becomes_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager(collect_alive=False)
            manager.tier3_last_collection_report = {
                "startup_supply_exhausted": True,
                "source_supply_terminal": True,
            }
            primary = Node(
                name="recovered-primary",
                protocol="ss",
                uri="ss://recovered-primary",
                address="recovered-primary.example",
                port=8388,
                tier="tier1",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.DEAD,
            )
            manager.runtime_nodes["tier1"] = [primary]
            runtime.manager = manager
            runtime.running = True
            runtime._tier3_required_for_start = True
            runtime._tier3_startup_terminal_failure = True
            runtime._last_tier3_startup_error = "terminal"
            runtime.last_error = "terminal"
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime._set_core_runtime_phase("stopped")
            starts: list[tuple[bool, bool]] = []
            runtime.start_core = lambda: starts.append(
                (
                    runtime._tier3_required_for_start,
                    runtime._tier3_startup_terminal_failure,
                )
            ) or {"running": True}

            runtime._sync_core_config_after_pool_update()
            self.assertEqual(starts, [])

            recovered = primary.clone()
            recovered.status = NodeStatus.ALIVE
            manager.runtime_nodes["tier1"] = [recovered]
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(starts, [(False, False)])
            self.assertFalse(runtime._tier3_required_for_start)
            self.assertFalse(runtime._tier3_startup_terminal_failure)
            self.assertIsNone(runtime._last_tier3_startup_error)
            self.assertIsNone(runtime.last_error)

    def test_tier3_core_gate_queues_exactly_at_min_alive_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            manager.min_alive_nodes = 3
            runtime.manager = manager
            runtime._tier3_required_for_start = True
            queued: list[str] = []
            runtime._queue_tier3_core_gate_attempt = lambda: queued.append("gate") or True

            two_nodes = [
                Node(name=f"tier3-{index}", protocol="ss", uri=f"ss://tier3-{index}", address=f"tier3-{index}.example", port=8388, tier="tier3", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
                for index in range(2)
            ]
            three_nodes = [
                *two_nodes,
                Node(name="tier3-2", protocol="ss", uri="ss://tier3-2", address="tier3-2.example", port=8388, tier="tier3", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE),
            ]

            manager.runtime_nodes["tier3"] = two_nodes
            runtime._on_tier3_pool_committed(two_nodes, {"admission_identity": "tier3-1"})
            self.assertEqual(queued, [])

            manager.runtime_nodes["tier3"] = three_nodes
            runtime._on_tier3_pool_committed(three_nodes, {"admission_identity": "tier3-2"})

            self.assertEqual(queued, ["gate"])

    def _legacy_runtime_audit_direct_leaf_is_facility_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            manager = FakeRuntimeAuditManager()
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "DIRECT", "all": ["PROXY-FALLBACK", "DIRECT"]},
                        }
                    }
                ]
            )

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["effective_egress_ok"])
            self.assertIn("DIRECT", result["facility_failure"])
            self.assertEqual(manager.marked_failures, [])
            self.assertEqual(runtime.api_client.delay_calls, [])

    def _legacy_runtime_audit_reject_leaves_are_fail_closed_without_rotation(self) -> None:
        for terminal in ("REJECT", "REJECT-DROP"):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                backend = MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                )
                runtime = self._runtime(temp_path, backend)
                manager = FakeRuntimeAuditManager()
                runtime.manager = manager
                runtime.api_client = FakeRuntimeAuditClient(
                    [
                        {
                            "proxies": {
                                "FINAL-EXIT": {
                                    "type": "Selector",
                                    "now": terminal,
                                    "all": ["PROXY-FALLBACK", terminal],
                                },
                            }
                        }
                    ]
                )

                result = runtime.audit_runtime_egress("final failed")

                self.assertFalse(result["effective_egress_ok"])
                self.assertEqual(result["terminal_kind"], "fail_closed")
                self.assertTrue(result["fail_closed_terminal"])
                self.assertEqual(result["facility_failure"], "")
                self.assertIn(terminal, result["reason"])
                self.assertEqual(result["switch_actions"], [])
                self.assertEqual(runtime.api_client.delay_calls, [])
                self.assertEqual(runtime.api_client.select_calls, [])
                self.assertEqual(manager.marked_failures, [])

    def _legacy_runtime_audit_compatible_leaf_is_unsafe_direct_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeRuntimeAuditManager()
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {
                                "type": "Selector",
                                "now": "COMPATIBLE",
                                "all": ["PROXY-FALLBACK", "COMPATIBLE"],
                            },
                        }
                    }
                ]
            )

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["effective_egress_ok"])
            self.assertEqual(result["terminal_kind"], "unsafe_direct")
            self.assertIn("direct-equivalent", result["facility_failure"])
            self.assertEqual(result["switch_actions"], [])
            self.assertEqual(runtime.api_client.delay_calls, [])
            self.assertEqual(runtime.api_client.select_calls, [])
            self.assertEqual(manager.marked_failures, [])

    def _legacy_runtime_audit_active_gateway_failure_does_not_switch_out_of_exit_select(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            gateway = Node(name="gateway", protocol="ss", uri="ss://gateway", address="gateway.example", port=8388, tier="exit", status=NodeStatus.ALIVE, method="aes-256-gcm", password="pass")
            manager = FakeRuntimeAuditManager()
            manager.exit_active = True
            manager.runtime_nodes["exit"] = [gateway]
            manager.fail_identities.add(manager._node_identity(gateway))
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "EXIT-SELECT", "all": ["EXIT-SELECT", "PROXY-FALLBACK", "DIRECT"]},
                            "EXIT-SELECT": {"type": "Selector", "now": "gateway", "all": ["gateway", "DIRECT"]},
                            "gateway": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["effective_egress_ok"])
            self.assertTrue(result["confirmed_failure"])
            self.assertEqual(runtime.api_client.select_calls, [])
            self.assertEqual(manager.marked_failures, [])
            self.assertEqual(manager.runtime_nodes["exit"][0].status, NodeStatus.ALIVE)
            self.assertIn("selected Gateway exit failed", result["reason"])

    def _legacy_runtime_audit_active_gateway_pass_but_final_exit_fails_is_facility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            gateway = Node(name="gateway", protocol="ss", uri="ss://gateway", address="gateway.example", port=8388, tier="exit", status=NodeStatus.ALIVE, method="aes-256-gcm", password="pass")
            manager = FakeRuntimeAuditManager()
            manager.exit_active = True
            manager.runtime_nodes["exit"] = [gateway]
            runtime.manager = manager
            runtime.api_client = FakeRuntimeAuditClient(
                [
                    {
                        "proxies": {
                            "FINAL-EXIT": {"type": "Selector", "now": "EXIT-SELECT", "all": ["EXIT-SELECT", "PROXY-FALLBACK", "DIRECT"]},
                            "EXIT-SELECT": {"type": "Selector", "now": "gateway", "all": ["gateway", "DIRECT"]},
                            "gateway": {"type": "Shadowsocks"},
                        }
                    }
                ]
            )

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["effective_egress_ok"])
            self.assertIn("leaf proxy passed targeted validation", result["facility_failure"])
            self.assertEqual(manager.marked_failures, [])
            self.assertEqual(runtime.api_client.select_calls, [])

    def _legacy_runtime_audit_tier3_leaf_uses_node_validation_probe_urls(self) -> None:
        class ProbeAwareClient:
            def __init__(self) -> None:
                self.delay_calls: list[tuple[str, str]] = []

            def proxies(self):
                return {
                    "proxies": {
                        "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                        "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER3-AUTO", "all": ["TIER3-AUTO"]},
                        "TIER3-AUTO": {"type": "Fallback", "now": "tier3-leaf", "all": ["tier3-leaf"]},
                        "tier3-leaf": {"type": "Shadowsocks"},
                    }
                }

            def proxy_delay(self, name, *, url=None, timeout_ms=5000):
                self.delay_calls.append((str(name), str(url or "")))
                if name == "DIRECT":
                    return {"delay": 1}
                if name == "tier3-leaf" and url == "https://node-good.example/probe":
                    return {"delay": 12}
                raise RuntimeError("probe failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["testing"] = {
                "probe_urls": [
                    "https://default-bad.example/probe",
                    "https://default-backup.example/probe",
                ]
            }
            node = Node(
                name="tier3-leaf",
                protocol="ss",
                uri="ss://tier3-leaf",
                address="tier3-leaf.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
                extra={
                    "validation_probe_urls": [
                        "https://node-bad.example/probe",
                        "https://node-good.example/probe",
                    ]
                },
            )
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime.api_client = ProbeAwareClient()

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["effective_egress_ok"])
            self.assertIn("leaf proxy passed targeted validation", result["facility_failure"])
            self.assertEqual(manager.marked_failures, [])
            self.assertIn(("tier3-leaf", "https://node-good.example/probe"), runtime.api_client.delay_calls)

    def _legacy_runtime_audit_tier3_leaf_503_is_transient_not_node_failure(self) -> None:
        class TransientLeafClient:
            def proxies(self):
                return {
                    "proxies": {
                        "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                        "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER3-AUTO", "all": ["TIER3-AUTO"]},
                        "TIER3-AUTO": {"type": "Fallback", "now": "tier3-leaf", "all": ["tier3-leaf"]},
                        "tier3-leaf": {"type": "Shadowsocks"},
                    }
                }

            def proxy_delay(self, name, *, url=None, timeout_ms=5000):
                if name == "DIRECT":
                    return {"delay": 1}
                if name == "tier3-leaf":
                    raise RuntimeError("GET /proxies/tier3-leaf/delay failed: HTTP Error 503: Service Unavailable")
                raise RuntimeError("final failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            node = Node(
                name="tier3-leaf",
                protocol="ss",
                uri="ss://tier3-leaf",
                address="tier3-leaf.example",
                port=8388,
                tier="tier3",
                method="aes-256-gcm",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier3"] = [node]
            runtime.manager = manager
            runtime.api_client = TransientLeafClient()

            result = runtime.audit_runtime_egress("final failed")

            self.assertFalse(result["effective_egress_ok"])
            self.assertFalse(result["confirmed_failure"])
            self.assertIn("transient", result["facility_failure"])
            self.assertEqual(manager.marked_failures, [])
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)

    def test_runtime_proxy_name_map_uses_exported_unique_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            first = Node(name="duplicate", protocol="ss", uri="ss://one", address="one.example", port=8388, tier="tier1", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
            second = Node(name="duplicate", protocol="ss", uri="ss://two", address="two.example", port=8388, tier="tier2", method="aes-256-gcm", password="pass", status=NodeStatus.ALIVE)
            manager = FakeRuntimeAuditManager()
            manager.runtime_nodes["tier1"] = [first]
            manager.runtime_nodes["tier2"] = [second]
            runtime.manager = manager

            mapping = runtime._runtime_proxy_name_map()

            self.assertEqual(mapping["duplicate"]["identity"], manager._node_identity(first))
            self.assertEqual(mapping["duplicate-2"]["identity"], manager._node_identity(second))

    def test_verify_tier3_after_core_reload_saves_primary_export_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            warm_calls: list[bool] = []
            save_calls: list[str] = []
            runtime._warm_tier3_group_after_core_start = lambda *, strict=False: warm_calls.append(strict) or True
            runtime._tier3_is_primary_export = lambda: True
            runtime._save_tier3_verified_snapshot = lambda: save_calls.append("save")

            verified = runtime._verify_tier3_after_core_reload()

            self.assertTrue(verified)
            self.assertEqual(warm_calls, [False])
            self.assertEqual(save_calls, ["save"])

    def test_verify_tier3_after_core_reload_skips_snapshot_for_non_primary_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            warm_calls: list[bool] = []
            save_calls: list[str] = []
            runtime._warm_tier3_group_after_core_start = lambda *, strict=False: warm_calls.append(strict) or True
            runtime._tier3_is_primary_export = lambda: False
            runtime._save_tier3_verified_snapshot = lambda: save_calls.append("save")

            verified = runtime._verify_tier3_after_core_reload()

            self.assertTrue(verified)
            self.assertEqual(warm_calls, [False])
            self.assertEqual(save_calls, [])

    def test_strict_reload_rejects_failed_204_even_when_group_delay_succeeds(self) -> None:
        for primary_export in (False, True):
            with self.subTest(primary_export=primary_export), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                backend = MemoryProxyBackend(
                    ProxySettings(enabled=False, server="", override="")
                )
                runtime = self._runtime(temp_path, backend)
                manager = FakeTier3Manager()
                runtime.manager = manager
                runtime._tier3_is_primary_export = lambda: primary_export
                warm_calls: list[bool] = []
                runtime._warm_tier3_group_after_core_start = (
                    lambda *, strict=False: warm_calls.append(strict) or True
                )
                runtime._probe_mihomo_mixed_port = lambda: (
                    _ for _ in ()
                ).throw(RuntimeError("204 unavailable"))
                save_calls: list[str] = []
                runtime._save_tier3_verified_snapshot = lambda: save_calls.append("save")

                with self.assertRaisesRegex(RuntimeError, "204 verification failed"):
                    runtime._verify_tier3_after_core_reload()

                self.assertEqual(warm_calls, [False])
                self.assertEqual(save_calls, [])
                self.assertEqual(manager.effective_states[-1][0], False)

    def test_strict_reload_does_not_accept_203_as_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            )
            runtime = self._runtime(temp_path, backend)
            manager = FakeTier3Manager()
            runtime.manager = manager
            runtime.config["testing"] = {
                "mihomo_helper": {
                    "probe_urls": ["https://probe.example/generate_204"]
                }
            }
            runtime._probe_mihomo_mixed_port = (
                DesktopRuntime._probe_mihomo_mixed_port.__get__(
                    runtime,
                    DesktopRuntime,
                )
            )
            runtime._warm_tier3_group_after_core_start = lambda *, strict=False: True

            class Response203:
                status = 203

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def getcode(self) -> int:
                    return 203

                def read(self, _limit: int) -> bytes:
                    return b""

            class Opener203:
                def open(self, _request, timeout=None):
                    del timeout
                    return Response203()

            with patch("desktop_runtime.build_opener", return_value=Opener203()):
                with self.assertRaisesRegex(RuntimeError, "204 verification failed"):
                    runtime._verify_tier3_after_core_reload()

            self.assertFalse(manager.effective_states[-1][0])

    def test_repair_retry_reload_helper_skips_primary_export_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            calls: list[str] = []
            runtime._tier3_is_primary_export = lambda: True
            runtime._repair_tier3_export_order_from_core_delays = lambda: calls.append("repair") or True
            runtime.write_mihomo_runtime_config = lambda: calls.append("write") or temp_path / "mihomo-runtime.yaml"

            result = runtime._repair_and_retry_tier3_reload_after_verification_failure(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertFalse(result["stop_sensitive"])
            self.assertEqual(result["reason"], "Bastion Layer verification failed after hot reload")
            self.assertEqual(calls, [])
            self.assertEqual(runner.hot_reloaded, 0)

    def test_repair_retry_reload_helper_rewrites_reloads_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            calls: list[str] = []
            runtime._tier3_is_primary_export = lambda: False
            runtime._repair_tier3_export_order_from_core_delays = lambda: calls.append("repair") or True
            runtime.write_mihomo_runtime_config = lambda: calls.append("write") or temp_path / "mihomo-runtime.yaml"
            runner.hot_reload = lambda **_kwargs: calls.append("reload") or {"reloaded": True}
            runtime._verify_tier3_after_core_reload = lambda: calls.append("verify") or True

            result = runtime._repair_and_retry_tier3_reload_after_verification_failure(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertTrue(result["reloaded"])
            self.assertEqual(calls, ["repair", "write", "reload", "verify"])

    def test_repair_retry_reload_helper_reports_retry_reload_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            calls: list[str] = []
            runtime._tier3_is_primary_export = lambda: False
            runtime._repair_tier3_export_order_from_core_delays = lambda: calls.append("repair") or True
            runtime.write_mihomo_runtime_config = lambda: calls.append("write") or temp_path / "mihomo-runtime.yaml"
            runner.hot_reload = lambda **_kwargs: calls.append("reload") or {
                "reloaded": False,
                "reason": "HTTP Error 400: Bad Request",
            }

            result = runtime._repair_and_retry_tier3_reload_after_verification_failure(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["stop_sensitive"])
            self.assertEqual(result["reason"], "hot reload after Bastion Layer repair failed: HTTP Error 400: Bad Request")
            self.assertEqual(calls, ["repair", "write", "reload"])

    def test_repair_retry_reload_helper_reports_verification_failure_after_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            calls: list[str] = []
            runtime._tier3_is_primary_export = lambda: False
            runtime._repair_tier3_export_order_from_core_delays = lambda: calls.append("repair") or True
            runtime.write_mihomo_runtime_config = lambda: calls.append("write") or temp_path / "mihomo-runtime.yaml"
            runner.hot_reload = lambda **_kwargs: calls.append("reload") or {"reloaded": True}
            runtime._verify_tier3_after_core_reload = lambda: (_ for _ in ()).throw(RuntimeError("still unhealthy"))

            result = runtime._repair_and_retry_tier3_reload_after_verification_failure(
                runner,
                {"enabled": True, "host": "127.0.0.1", "port": 9090},
            )

            self.assertFalse(result["reloaded"])
            self.assertFalse(result["stop_sensitive"])
            self.assertEqual(result["reason"], "Bastion Layer verification failed after repair reload")
            self.assertEqual(calls, ["repair", "write", "reload"])

    def test_export_failure_cannot_claim_closed_when_core_stop_fails(self) -> None:
        for stop_raises in (False, True):
            with self.subTest(stop_raises=stop_raises), tempfile.TemporaryDirectory() as temp_dir:
                backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
                runtime = self._runtime(Path(temp_dir), backend)
                runner = FakeLifecycleCoreRunner()
                runner.start()
                runtime.core_runner = runner
                runtime.system_proxy.enable()
                active_proxy_settings = backend.read()

                def failed_stop(**_kwargs):
                    if stop_raises:
                        raise RuntimeError("stop failed")
                    return {"stopped": False, "running": True, "still_running": True}

                runner.stop = failed_stop
                result = runtime._fail_closed_after_core_export_removal_failure(
                    runner=runner, reason="test export failure",
                )
                self.assertFalse(result["fail_closed"])
                self.assertTrue(result["core"]["running"])
                self.assertEqual(backend.read(), active_proxy_settings)
                self.assertTrue(runtime.system_proxy.active)

    def test_failed_hot_reload_stops_stale_export_but_keeps_admitted_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            previous = Node(
                name="previous-good",
                protocol="ss",
                uri="ss://previous-good",
                address="previous.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            expanded = Node(
                name="expanded-good",
                protocol="ss",
                uri="ss://expanded-good",
                address="expanded.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager.runtime_nodes["tier3"] = [expanded]
            runtime.manager = manager
            runtime._last_synced_tier3_nodes = [previous]
            runtime._prime_tier3_pool_before_core_start = lambda: (_ for _ in ()).throw(
                AssertionError("startup priming should not run for an already verified running pool")
            )
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runner.hot_reload = lambda **_kwargs: {"reloaded": False, "reason": "HTTP Error 400: Bad Request"}
            runtime.core_runner = runner

            runtime.system_proxy.enable()
            active_proxy_settings = backend.read()

            def write_new_config() -> Path:
                runtime_config.write_text("new-config", encoding="utf-8")
                return runtime_config

            runtime.write_mihomo_runtime_config = write_new_config

            result = runtime.reload_core_config(
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertFalse(result["reloaded"])
            self.assertFalse(result["kept_existing_core"])
            self.assertTrue(result["fail_closed"])
            self.assertFalse(runner.is_running())
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), "new-config")
            self.assertEqual(backend.read(), active_proxy_settings)
            self.assertTrue(runtime.system_proxy.active)
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["expanded-good"])
            self.assertNotIn("core_reload_rollback", manager.tier3_last_collection_report)

    def test_primary_tier3_authoritative_verification_failure_keeps_pool_transactional(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()

            def node(name: str) -> Node:
                return Node(
                    name=name,
                    protocol="ss",
                    uri=f"ss://{name}",
                    address=f"{name}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )

            manager.runtime_nodes["tier3"] = [node("bad-one"), node("bad-two"), node("good-one")]
            runtime.manager = manager
            runtime._last_synced_tier3_nodes = [node("bad-one"), node("bad-two"), node("good-one")]
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runner.hot_reload = lambda **_kwargs: {"reloaded": True}

            def write_new_config() -> Path:
                runtime_config.write_text("new-config", encoding="utf-8")
                return runtime_config

            runtime.write_mihomo_runtime_config = write_new_config
            # The probes carry the names the Core loaded (stable names by default).
            core_name = {node.name: name for name, node in runtime._tier3_proxy_name_pairs()}

            def fake_delay(name, *, url=None, timeout_ms=5000):
                if name == core_name["good-one"]:
                    return {"delay": 20}
                raise RuntimeError("core probe failed")

            runtime.test_proxy_delay = fake_delay
            runtime._probe_mihomo_mixed_port = lambda: (_ for _ in ()).throw(
                RuntimeError("FINAL-EXIT 204 failed")
            )
            runtime._warm_tier3_group_after_core_start = lambda *, strict=False: True

            result = runtime.reload_core_config(
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )
            self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["kept_existing_core"])
            self.assertIn("verification failed after hot reload", result["reason"])
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), "old-config")
            self.assertEqual(
                [(node.name, node.status) for node in manager.runtime_nodes["tier3"]],
                [("bad-one", NodeStatus.ALIVE), ("bad-two", NodeStatus.ALIVE), ("good-one", NodeStatus.ALIVE)],
            )
            self.assertEqual(runtime._tier3_core_export_override_snapshot(), [])
            self.assertTrue(manager.effective_states)
            self.assertIsNone(manager.effective_states[-1][0])
            self.assertFalse(runtime._runtime_egress_readiness.validation_active)

    def test_keep_existing_tier3_core_helper_restores_config_without_reverting_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("new-config", encoding="utf-8")
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [
                Node(
                    name="new-candidate",
                    protocol="ss",
                    uri="ss://new-candidate",
                    address="new.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
            ]
            previous = Node(
                name="previous-good",
                protocol="ss",
                uri="ss://previous-good",
                address="previous.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()

            result = runtime._keep_existing_tier3_core_after_reload_failure(
                runner=runner,
                controller={"enabled": True, "host": "127.0.0.1", "port": 9090},
                runtime_config_path=runtime_config,
                previous_config_text="old-config",
                rollback_tier3_nodes=[previous],
                reason="verification failed",
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["kept_existing_core"])
            self.assertTrue(result["restored_config"])
            self.assertTrue(result["restore_reload"]["reloaded"])
            self.assertEqual(runner.hot_reloaded, 1)
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), "old-config")
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["new-candidate"])
            self.assertNotIn("core_reload_rollback", manager.tier3_last_collection_report)

    def test_stop_sensitive_reload_failure_response_keeps_existing_when_runtime_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()

            result = runtime._stop_sensitive_reload_failure_response(
                runner=runner,
                reload_result={"reloaded": False, "reason": "verification failed", "stop_sensitive": True},
                keep_existing=lambda reason: {"kept_existing_core": True, "reason": reason},
            )

            self.assertTrue(result["kept_existing_core"])
            self.assertEqual(result["reason"], "verification failed")

    def test_stop_sensitive_reload_failure_response_reports_unavailable_when_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.stop_event.set()
            keep_existing_calls: list[str] = []

            result = runtime._stop_sensitive_reload_failure_response(
                runner=runner,
                reload_result={"reloaded": False, "reason": "reload interrupted", "stop_sensitive": True},
                keep_existing=lambda reason: keep_existing_calls.append(reason) or {"kept_existing_core": True},
            )

            self.assertFalse(result["reloaded"])
            self.assertEqual(result["reason"], "runtime stopping")
            self.assertEqual(keep_existing_calls, [])

    def test_sync_core_config_reloads_fail_closed_when_export_pool_becomes_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = []
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.running = True
            runtime._last_tier3_core_sync_marker = (("previous-good",),)
            reload_calls: list[str] = []
            runtime.reload_core_config = lambda *, mode="strict", reason=None: (
                reload_calls.append(mode) or {"reloaded": True}
            )

            runtime._sync_core_config_after_pool_update()

            self.assertTrue(runner.is_running())
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertEqual(reload_calls, ["incremental_export"])
            self.assertEqual(runtime._last_tier3_core_sync_marker, ())

    def test_sync_core_config_reloads_empty_tier3_when_primary_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": [{"name": "primary"}]},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier1"] = [
                Node(
                    name="primary",
                    protocol="ss",
                    uri="ss://primary",
                    address="primary.example.com",
                    port=8388,
                    tier="tier1",
                    status=NodeStatus.DEAD,
                )
            ]
            manager.runtime_nodes["tier3"] = []
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.running = True
            runtime._last_tier3_core_sync_marker = (("previous-good",),)
            reload_calls: list[str] = []
            runtime.reload_core_config = lambda *, mode="strict", reason=None: (
                reload_calls.append(mode) or {"reloaded": True}
            )

            runtime._sync_core_config_after_pool_update()

            self.assertTrue(runner.is_running())
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertEqual(reload_calls, ["incremental_export"])
            self.assertEqual(runtime._last_tier3_core_sync_marker, ())

    def test_empty_healthy_export_write_failure_stops_stale_core_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": [{"name": "primary"}]},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager(collect_alive=False)
            manager.runtime_nodes["tier1"] = [
                Node(
                    name="primary",
                    protocol="ss",
                    uri="ss://primary",
                    address="primary.example.com",
                    port=8388,
                    tier="tier1",
                    status=NodeStatus.DEAD,
                )
            ]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.write_mihomo_runtime_config = lambda: (_ for _ in ()).throw(
                RuntimeError("write failed")
            )

            runtime.system_proxy.enable()
            active_proxy_settings = backend.read()

            result = runtime.reload_core_config(
                mode="incremental_export",
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["fail_closed"])
            self.assertFalse(result["kept_existing_core"])
            self.assertFalse(runner.is_running())
            self.assertTrue(runner.stopped)
            self.assertEqual(manager.effective_states[-1][0], False)
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), "old-config")
            self.assertEqual(backend.read(), active_proxy_settings)
            self.assertTrue(runtime.system_proxy.active)
            self.assertEqual(runner.hot_reloaded, 0)

    def test_tier3_health_change_updates_core_eligibility_without_deleting_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            self._install_injected_runtime_authority(runtime)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 9090,
            }
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            retained = Node(
                name="retained-tier3",
                protocol="ss",
                uri="ss://retained-tier3",
                address="retained.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager = FakeTier3Manager()
            manager.runtime_nodes["tier3"] = [retained]
            candidate_revision = [1]
            manager.runtime_candidate_snapshot = lambda: SimpleNamespace(
                revision=candidate_revision[0],
                runtime_generation=manager.runtime_generation,
            )
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.running = True
            runtime._core_generation = 1
            runtime._runtime_initial_activation_generation = 1
            runtime._set_core_runtime_phase("formal_capture")
            # This test owns provider eligibility only. Facility hold/CAS
            # release is covered by the dedicated startup-stability suite,
            # which supplies a real immutable route snapshot.
            runtime._record_runtime_facility_degradation = (
                lambda reason: runtime._set_effective_egress_readiness_state(
                    None,
                    str(reason),
                )
            )

            def render_runtime(manager_view, _route, _limits):
                tiers = {
                    tier_id: manager_view.get_nodes_for_subscription(tier_id)
                    for tier_id in ("tier1", "tier2", "tier3", "exit")
                }
                return (
                    "application/yaml; charset=utf-8",
                    build_full_clash_config(tiers),
                )

            def verify_runtime_final_exit(**_kwargs):
                if retained.status is not NodeStatus.ALIVE:
                    raise RuntimeError("no proxy route")
                return {"url": "cp.cloudflare.com", "status": 204}

            runtime._verify_runtime_final_exit = verify_runtime_final_exit
            runtime._hot_reload_runtime_config_phase = (
                lambda *_args, **_kwargs: runtime._set_core_runtime_phase(
                    "formal_capture"
                )
            )
            runtime._apply_system_proxy_capture_after_core_start = (
                lambda *_args, **_kwargs: None
            )
            runtime.traffic_monitor = FakeTrafficMonitor()
            with patch("desktop_runtime.render_subscription_route", side_effect=render_runtime):
                runtime.write_mihomo_runtime_config()
                provider_path = runtime._tier3_core_provider_file_path()
                loaded_provider_bytes = provider_path.read_bytes()
                runtime._last_tier3_core_sync_marker = runtime._tier3_core_sync_marker()
                runtime._mark_core_export_sync_success()

                manager.runtime_nodes["tier3"][0].status = NodeStatus.DEAD
                candidate_revision[0] += 1
                runtime._sync_core_config_after_pool_update()
                runtime._queue_incremental_egress_recheck()
                self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

                rejected = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))
                self.assertEqual(
                    [proxy["name"] for proxy in rejected["proxies"]],
                    [],
                )
                rejected_provider = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    [proxy["name"] for proxy in rejected_provider["proxies"]],
                    ["retained-tier3", "REJECT"],
                )
                self.assertEqual(provider_path.read_bytes(), loaded_provider_bytes)
                self.assertEqual(runner.hot_reloaded, 0)
                self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.DEAD)
                self.assertEqual(len(manager.runtime_nodes["tier3"]), 1)
                self.assertFalse(manager.effective_states[-1][0])

                manager.runtime_nodes["tier3"][0].status = NodeStatus.ALIVE
                candidate_revision[0] += 1
                runtime._sync_core_config_after_pool_update()
                # A real authoritative recheck clears the terminal incident
                # when node eligibility changes.  This provider-only fixture
                # models that event explicitly without reopening recovery on
                # every raw health revision.
                runtime._reset_tier3_standby_activation_failure()
                runtime._queue_incremental_egress_recheck()
                self.assertTrue(runtime._wait_for_tier3_egress_activation(timeout=30.0))

            recovered = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))
            self.assertEqual([proxy["name"] for proxy in recovered["proxies"]], [])
            recovered_provider = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [proxy["name"] for proxy in recovered_provider["proxies"]],
                ["retained-tier3", "REJECT"],
            )
            self.assertEqual(provider_path.read_bytes(), loaded_provider_bytes)
            self.assertEqual(runner.hot_reloaded, 0)
            self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
            self.assertIsNone(manager.effective_states[-1][0])
            self.assertFalse(runtime._runtime_egress_readiness.validation_active)

    def test_primary_proxy_removal_reload_failure_stops_old_loaded_export(self) -> None:
        for tier_id in ("tier1", "tier2"):
            with self.subTest(tier_id=tier_id), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
                runtime = self._runtime(temp_path, backend)
                runtime_config = temp_path / "mihomo-runtime.yaml"
                runtime_config.write_text("old-loaded-config", encoding="utf-8")
                runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
                runtime.config["client"]["mihomo_controller"] = {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 9090,
                }
                runtime.config["tiers"] = {
                    "tier1": {"nodes": [{"name": "primary"}] if tier_id == "tier1" else []},
                    "tier2": {
                        "nodes": [{"name": "primary"}] if tier_id == "tier2" else [],
                        "subscription_urls": [],
                    },
                    "tier3": {"enabled": True, "bootstrap_enabled": True},
                }
                primary = Node(
                    name=f"{tier_id}-primary",
                    protocol="ss",
                    uri=f"ss://{tier_id}-primary",
                    address=f"{tier_id}.example.com",
                    port=8388,
                    tier=tier_id,
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                fallback = Node(
                    name="healthy-tier3",
                    protocol="ss",
                    uri="ss://healthy-tier3",
                    address="tier3.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                )
                manager = FakeTier3Manager()
                manager.runtime_nodes[tier_id] = [primary]
                manager.runtime_nodes["tier3"] = [fallback]
                runtime.manager = manager
                runner = FakeLifecycleCoreRunner()
                runner.start()
                runner.hot_reload = lambda **_kwargs: {
                    "reloaded": False,
                    "reason": "controller rejected config",
                }
                runtime.core_runner = runner
                runtime.running = True
                runtime._set_core_runtime_phase("formal_capture")
                runtime._mark_core_export_sync_success()

                manager.runtime_nodes[tier_id][0].status = NodeStatus.DEAD

                def write_removed_export() -> Path:
                    runtime_config.write_text("new-config-without-primary", encoding="utf-8")
                    return runtime_config

                runtime.write_mihomo_runtime_config = write_removed_export
                result = runtime.reload_core_config(
                    mode="incremental_export",
                    reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
                )

                self.assertFalse(result["reloaded"])
                self.assertTrue(result["fail_closed"])
                self.assertFalse(result["kept_existing_core"])
                self.assertFalse(runner.is_running())
                self.assertEqual(manager.runtime_nodes[tier_id][0].status, NodeStatus.DEAD)
                self.assertEqual(manager.runtime_nodes["tier3"][0].status, NodeStatus.ALIVE)
                self.assertEqual(manager.effective_states[-1][0], False)
                self.assertEqual(
                    runtime_config.read_text(encoding="utf-8"),
                    "new-config-without-primary",
                )

    def test_terminal_tier3_report_does_not_reload_formal_primary_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": [{"name": "primary"}]},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            primary = Node(
                name="primary",
                protocol="ss",
                uri="ss://primary",
                address="primary.example.com",
                port=8388,
                tier="tier1",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager = FakeTier3Manager(collect_alive=False)
            manager.runtime_nodes["tier1"] = [primary]
            manager.runtime_nodes["tier3"] = []
            manager.tier3_last_collection_report = {
                "startup_supply_exhausted": True,
                "source_supply_terminal": True,
            }
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.running = True
            runtime._set_core_runtime_phase("formal_capture")
            runtime._last_tier3_core_sync_marker = runtime._tier3_core_sync_marker()
            runtime._mark_core_export_sync_success()
            reload_calls: list[str] = []
            runtime.reload_core_config = lambda *, mode="strict", reason=None: (
                reload_calls.append(mode) or {"reloaded": True}
            )
            runtime._update_running_tier3_core_provider = lambda _marker: {
                "unchanged": True,
                "semantic_digest": "provider-fixture",
            }

            manager.runtime_nodes["tier1"][0].status = NodeStatus.DEAD
            runtime._sync_core_config_after_pool_update()
            manager.runtime_nodes["tier1"][0].status = NodeStatus.ALIVE
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(
                reload_calls,
                [],
            )
            self.assertTrue(runner.is_running())

    def test_dead_exit_is_removed_from_private_export_without_runtime_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            exit_node = Node(
                name="gateway",
                protocol="ss",
                uri="ss://gateway",
                address="gateway.example.com",
                port=8388,
                tier="exit",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
            )
            manager = FakeTier3Manager()
            manager.runtime_nodes["exit"] = [exit_node]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner
            runtime.running = True
            runtime._set_core_runtime_phase("formal_capture")
            runtime._last_tier3_core_sync_marker = runtime._tier3_core_sync_marker()
            alive_marker = runtime._core_export_sync_marker()
            runtime._mark_core_export_sync_success(alive_marker)
            reload_calls: list[str] = []
            runtime.reload_core_config = lambda *, mode="strict", reason=None: (
                reload_calls.append(mode) or {"reloaded": True}
            )
            runtime._update_running_tier3_core_provider = lambda _marker: {
                "unchanged": True,
                "semantic_digest": "provider-fixture",
            }

            manager.runtime_nodes["exit"][0].status = NodeStatus.DEAD
            dead_marker = runtime._core_export_sync_marker()
            self.assertNotEqual(dead_marker, alive_marker)
            self.assertEqual(
                runtime._manager_for_mihomo_runtime_config().get_nodes_for_subscription(
                    "exit"
                ),
                [],
            )
            runtime._sync_core_config_after_pool_update()

            manager.runtime_nodes["exit"][0].status = NodeStatus.ALIVE
            recovered_marker = runtime._core_export_sync_marker()
            self.assertEqual(recovered_marker, alive_marker)
            runtime._sync_core_config_after_pool_update()

            self.assertEqual(
                reload_calls,
                [],
            )
            self.assertEqual(len(manager.runtime_nodes["exit"]), 1)

    def test_reload_core_config_keeps_running_core_when_new_tier3_pool_fails_core_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            previous_node = Node(
                name="previous-good",
                protocol="ss",
                uri="ss://previous-good",
                address="previous.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
                extra={"startup_core_probe_url": "https://cp.cloudflare.com/generate_204"},
            )
            manager.runtime_nodes["tier3"] = [previous_node]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner

            def replace_with_tcp_candidate() -> None:
                manager.runtime_nodes["tier3"] = [
                    Node(
                        name="tcp-candidate",
                        protocol="ss",
                        uri="ss://tcp-candidate",
                        address="tcp.example.com",
                        port=8388,
                        tier="tier3",
                        status=NodeStatus.ALIVE,
                        method="aes-256-gcm",
                        password="pass",
                        extra={"startup_prelaunch_validation": "tcp"},
                    )
                ]

            def write_new_config() -> Path:
                runtime_config.write_text("new-unverified-config", encoding="utf-8")
                return runtime_config

            runtime._prime_tier3_pool_before_core_start = replace_with_tcp_candidate
            runtime.write_mihomo_runtime_config = write_new_config
            runtime.test_proxy_delay = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("timeout"))

            result = runtime.reload_core_config(
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["kept_existing_core"])
            self.assertTrue(runner.is_running())
            self.assertFalse(runner.stopped)
            self.assertEqual(runner.hot_reloaded, 2)
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), "old-config")
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["previous-good"])

    def test_failed_hot_reload_does_not_restore_previous_tier3_pool_for_full_validation_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime_config = temp_path / "mihomo-runtime.yaml"
            runtime_config.write_text("old-config", encoding="utf-8")
            runtime.config["client"]["mihomo_runtime_config"] = str(runtime_config)
            runtime.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9090}
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            previous_node = Node(
                name="previous-good",
                protocol="ss",
                uri="ss://previous-good",
                address="previous.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
                method="aes-256-gcm",
                password="pass",
                extra={"startup_prelaunch_validation": "tcp"},
            )
            manager.runtime_nodes["tier3"] = [previous_node]
            runtime.manager = manager
            runner = FakeLifecycleCoreRunner()
            runner.start()
            runtime.core_runner = runner

            def replace_with_full_refresh_candidate() -> None:
                manager.runtime_nodes["tier3"] = [
                    Node(
                        name="new-full-refresh",
                        protocol="ss",
                        uri="ss://new-full-refresh",
                        address="new.example.com",
                        port=8388,
                        tier="tier3",
                        status=NodeStatus.ALIVE,
                        method="aes-256-gcm",
                        password="pass",
                    )
                ]

            def write_new_config() -> Path:
                runtime_config.write_text("new-unverified-config", encoding="utf-8")
                return runtime_config

            runtime._prime_tier3_pool_before_core_start = replace_with_full_refresh_candidate
            runtime.write_mihomo_runtime_config = write_new_config
            runtime.test_proxy_delay = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("timeout"))

            result = runtime.reload_core_config(
                reason=RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
            )

            self.assertFalse(result["reloaded"])
            self.assertTrue(result["kept_existing_core"])
            self.assertEqual([node.name for node in manager.runtime_nodes["tier3"]], ["new-full-refresh"])
            self.assertNotIn("core_reload_rollback", manager.tier3_last_collection_report)

    def test_startup_ignores_tcp_only_cached_tier3_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.config["tiers"] = {
                "tier1": {"nodes": []},
                "tier2": {"nodes": [], "subscription_urls": []},
                "tier3": {"enabled": True, "bootstrap_enabled": True},
            }
            manager = FakeTier3Manager()
            manager.tier3_last_collection_report = {
                "startup_mode": True,
                "validation": {"prelaunch_validation": "tcp"},
            }
            manager.runtime_nodes["tier3"] = [
                Node(
                    name=f"tcp-candidate-{index}",
                    protocol="ss",
                    uri=f"ss://tcp-candidate-{index}",
                    address=f"tcp-{index}.example.com",
                    port=8388,
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                    method="aes-256-gcm",
                    password="pass",
                    extra={"startup_prelaunch_validation": "tcp"},
                )
                for index in range(4)
            ]
            runtime.manager = manager

            runtime._prime_tier3_pool_before_core_start()

            self.assertEqual(manager.check_calls, [])
            self.assertEqual(manager.refreshed, 1)
            self.assertEqual(
                manager.tier3_last_collection_report["discarded_tcp_candidate_cache_count"],
                4,
            )

    def test_stop_core_stops_traffic_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            traffic = FakeTrafficMonitor()
            traffic.start()
            runtime.traffic_monitor = traffic

            status = runtime.stop_core()

            self.assertFalse(status["running"])
            self.assertEqual(traffic.stopped, 1)

    def test_stop_core_reports_bounded_substages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            runtime.core_runner = FakeLifecycleCoreRunner()
            runtime.core_runner.start()
            runtime.traffic_monitor = FakeTrafficMonitor()
            stages: list[str] = []

            status = runtime.stop_core(stage_observer=stages.append)

            self.assertFalse(status["running"])
            self.assertEqual(
                stages,
                [
                    "traffic_monitor_started",
                    "traffic_monitor_completed",
                    "egress_activation_completed",
                    "runner_status_completed",
                    "runner_stop_started",
                    "runner_stop_completed",
                    "manager_state_completed",
                ],
            )

    def test_kill_switch_cleanup_cannot_block_runtime_stop(self) -> None:
        class BlockingKillSwitch:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def close(self, *, timeout: float) -> None:
                self.started.set()
                self.release.wait(timeout=5.0)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)
            kill_switch = BlockingKillSwitch()
            runtime.kill_switch = kill_switch

            started_at = time.monotonic()
            try:
                completed = runtime._cleanup_kill_switch_bounded(timeout=0.1)
                elapsed = time.monotonic() - started_at
                self.assertTrue(kill_switch.started.is_set())
                self.assertFalse(completed)
                self.assertLess(elapsed, 0.2)
            finally:
                kill_switch.release.set()

    def test_traffic_snapshot_is_empty_before_core_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
            runtime = self._runtime(temp_path, backend)

            snapshot = runtime.traffic_snapshot()

            self.assertFalse(snapshot["running"])
            self.assertIsNone(snapshot["latest"])
            self.assertEqual(snapshot["history"], [])


if __name__ == "__main__":
    unittest.main()
