"""A strict Core reload of a Bastion-primary profile probes the names the Core loaded (F19, 11-02).

The real DesktopRuntime._reload_core_config_serialized(mode="strict") in
formal_capture: the real renderer and provider projection, the real repair
plan (_reload_pruned_primary_tier3_export) and the real qualification.  The
recovery helper's stubs of test_proxy_delay and
_refresh_tier3_runtime_qualification are removed.  Only the Core is fake:
FakeLifecycleCoreRunner (every hot reload succeeds) and a controller that
serves exactly the members of the provider file the runtime just wrote, with
Mihomo's exact-name lookup (404 for any other name); stop_core is recorded.
No Core process, no network.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from mihomo_api import MihomoAPIError
from models import Node, NodeStatus
from system_proxy import ProxySettings
from tests import test_desktop_runtime_recovery as recovery  # module import: no TestCase re-collection
from tests.test_desktop_runtime_recovery import FakeLifecycleCoreRunner, FakeTier3Manager, MemoryProxyBackend


class _ExactNameController:
    """GET /proxies and GET /proxies/{name}/delay over the provider file on disk."""

    def __init__(self, provider_path: Path) -> None:
        self.provider_path = provider_path
        self.delay_calls: list[str] = []

    def members(self) -> list[str]:
        document = yaml.safe_load(self.provider_path.read_text(encoding="utf-8")) or {}
        return [str(proxy["name"]) for proxy in document.get("proxies", [])
                if isinstance(proxy, dict) and proxy.get("name") != "REJECT"]

    def proxies(self, *_args, **_kwargs):
        names = self.members()
        entries = {name: {"type": "Http", "alive": True, "history": [], "extra": {}} for name in names}
        entries["TIER3-AUTO"] = {"type": "Fallback", "now": names[0] if names else "REJECT",
                                 "all": list(names)}
        entries["REJECT"] = {"type": "Reject"}
        return {"proxies": entries}

    def proxy_delay(self, name, **kwargs):
        sink = kwargs.get("connection_abort_sink")
        if callable(sink):
            sink(lambda: None)
        self.delay_calls.append(str(name))
        if str(name) not in set(self.members()):  # hub/route/proxies.go: exact name or 404
            raise MihomoAPIError(f"GET /proxies/{name}/delay failed: HTTP 404 Not Found")
        return {"delay": 12}


def _runtime(tmp_path: Path, verified: bool):
    case = recovery.DesktopRuntimeRecoveryTests(
        "test_tier3_tcp_startup_core_probe_export_is_not_capped_by_subscription_limit")
    runtime = case._runtime(tmp_path, MemoryProxyBackend(ProxySettings(enabled=False, server="", override="")))
    # The helper stubs exactly the seams under test: use the real methods.
    del runtime.test_proxy_delay
    del runtime._refresh_tier3_runtime_qualification
    runtime.config["tiers"] = {
        "tier1": {"nodes": []},
        "tier2": {"nodes": [], "subscription_urls": []},
        "tier3": {"enabled": True, "verified_provider_writes": verified},
    }
    manager = FakeTier3Manager()
    manager.min_alive_nodes = 3
    manager.config = runtime.config
    manager.testing_config = {}
    manager.client_config = runtime.config["client"]
    manager.logger = logging.getLogger("tests.tier3-strict-reload-core-names")
    manager.active_exit_proxy_name = lambda: ""
    manager.runtime_nodes["tier3"] = [
        Node(name=name, protocol="http", uri=f"http://{name}.example.invalid:8080",
             address=f"{name}.example.invalid", port=8080, tier="tier3", status=NodeStatus.ALIVE)
        for name in ("alpha", "beta", "gamma")
    ]
    manager.runtime_nodes["exit"] = [
        Node(name="gw", protocol="http", uri="http://gw.example.invalid:8080",
             address="gw.example.invalid", port=8080, tier="exit", status=NodeStatus.ALIVE)
    ]
    runtime.manager = manager
    return runtime


@pytest.mark.parametrize("delete_gateway", [False, True], ids=["nothing-removed", "gateway-deleted"])
@pytest.mark.parametrize("verified", [True, False], ids=["stable-names", "positional-names"])
def test_strict_reload_of_a_bastion_primary_profile_probes_loaded_names(tmp_path, verified, delete_gateway):
    runtime = _runtime(tmp_path, verified)
    try:
        runner = FakeLifecycleCoreRunner()
        runner.start()
        runtime.core_runner = runner
        runtime._mihomo_controller_config = lambda: {"enabled": True, "host": "127.0.0.1",
                                                     "port": 9090, "secret": ""}
        runtime._prime_tier3_pool_before_core_start = lambda *a, **k: None
        with runtime._core_lifecycle_lock:
            runtime._core_runtime_phase = "formal_capture"
        # The loaded Core: this export, marked synced.
        runtime.write_mihomo_runtime_config()
        runtime._mark_core_export_sync_success()
        runtime._last_synced_tier3_nodes = runtime._clone_runtime_nodes(
            runtime._tier3_nodes_for_core_export(runtime._mihomo_tier3_export_limit()))
        controller = _ExactNameController(runtime._tier3_core_provider_file_path())
        runtime.api_client = controller
        runtime._probe_urls_for_node = lambda _node: ["http://127.0.0.1:9/generate_204"]
        stops: list[str] = []

        def record_stop_core(**kwargs):
            stops.append(str(kwargs.get("reason") or ""))
            runner.stop()
            return {"running": False}

        runtime.stop_core = record_stop_core
        if delete_gateway:  # the same reload also retires a loaded proxy (unsafe export removal)
            runtime.manager.runtime_nodes["exit"] = []

        result = runtime._reload_core_config_serialized(mode="strict")

        assert result.get("reloaded") is True, result
        assert stops == []
        assert set(controller.delay_calls) == set(controller.members())
        assert all(("·" in name) is verified for name in controller.delay_calls), controller.delay_calls
    finally:
        runtime.stop_event.set()
