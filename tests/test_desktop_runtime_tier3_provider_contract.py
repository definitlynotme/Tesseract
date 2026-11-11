"""Focused contracts for DesktopRuntime's Tier3B provider integration."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
import pytest

from clash_formatter import build_capture_disabled_clash_config
from desktop_runtime import (
    DeferredProviderSyncKey,
    DesktopRuntime,
    ProviderSyncDisposition,
)
from runtime_egress_recovery import CoreLoadedRouteSnapshot, RuntimeNodeRef
from tier3_core_provider import project_tier3b_file_provider


def _provider_runtime_yaml(*, server: str) -> str:
    return yaml.safe_dump(
        {
            "mixed-port": 7890,
            "proxies": [
                {
                    "name": "stable-name",
                    "type": "ss",
                    "server": server,
                    "port": 443,
                    "cipher": "aes-256-gcm",
                    "password": "secret",
                }
            ],
            "proxy-groups": [
                {
                    "name": "TIER3-AUTO",
                    "type": "fallback",
                    "proxies": ["stable-name", "REJECT"],
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                    "empty-fallback": "REJECT",
                },
                {
                    "name": "PROXY-FALLBACK",
                    "type": "fallback",
                    "proxies": ["TIER3-AUTO"],
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                    "empty-fallback": "REJECT",
                },
                {
                    "name": "FINAL-EXIT",
                    "type": "select",
                    "proxies": ["TIER3-AUTO", "REJECT"],
                },
            ],
            "rules": ["MATCH,FINAL-EXIT"],
        },
        allow_unicode=True,
        sort_keys=False,
    )


def _runtime_shell(tmp_path: Path) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.project_root = tmp_path
    runtime.config = {
        "client": {
            "mihomo_runtime_config": str(tmp_path / "config" / "mihomo-runtime.yaml"),
            "mihomo_work_dir": str(tmp_path / "core-home"),
        }
    }
    runtime.manager = object()
    runtime._tier3_core_provider_name = "TIER3B-RUNTIME"
    runtime._tier3_core_provider_filename = "tier3b-runtime.yaml"
    return runtime


class _RunningCore:
    @staticmethod
    def is_running() -> bool:
        return True


class _ProviderClient:
    def update_proxy_provider(self, _name: str, *, timeout: float) -> None:
        assert timeout > 0

    def proxy_provider(self, name: str, *, timeout: float) -> dict[str, object]:
        assert timeout > 0
        return {
            "name": name,
            # Mihomo v1.19.27 exposes ordered members but no content digest or
            # provider version.  This deliberately models that exact boundary.
            "proxies": [
                {"name": "stable-name"},
                {"name": "REJECT"},
            ],
        }


class _Coordinator:
    def set_current_snapshot(self, _snapshot: object) -> None:
        return None


class _ReloadGate:
    def begin_recovery(self, transaction_id: str):
        assert transaction_id.startswith("tier3-provider:")
        return object()

    @staticmethod
    def end_recovery(_lease: object) -> bool:
        return True

    @staticmethod
    def set_loaded_semantic_digest(_digest: str) -> None:
        return None


class _SequencedProviderClient:
    def __init__(
        self,
        provider_path: Path,
        provider_members: list[tuple[str, ...]],
    ) -> None:
        self.provider_path = provider_path
        self.provider_members = list(provider_members)
        self.update_file_snapshots: list[bytes] = []
        self.provider_readbacks: list[tuple[str, ...]] = []

    def update_proxy_provider(self, _name: str, *, timeout: float) -> None:
        assert timeout > 0
        self.update_file_snapshots.append(self.provider_path.read_bytes())

    def proxy_provider(self, name: str, *, timeout: float) -> dict[str, object]:
        assert timeout > 0
        members = self.provider_members.pop(0)
        self.provider_readbacks.append(members)
        return {
            "name": name,
            "proxies": [{"name": member} for member in members],
        }


def _named_projection(*, name: str, server: str):
    document = yaml.safe_load(_provider_runtime_yaml(server=server))
    document["proxies"][0]["name"] = name
    document["proxy-groups"][0]["proxies"] = [name, "REJECT"]
    return project_tier3b_file_provider(document, [name])


def _numbered_projection(member_count: int):
    document = yaml.safe_load(_provider_runtime_yaml(server="unused.example"))
    proxies = [
        {
            "name": f"node-{index:02d}",
            "type": "ss",
            "server": f"node-{index:02d}.example",
            "port": 443,
            "cipher": "aes-256-gcm",
            "password": f"secret-{index:02d}",
        }
        for index in range(member_count)
    ]
    names = [str(proxy["name"]) for proxy in proxies]
    document["proxies"] = proxies
    tier3_group = next(
        group
        for group in document["proxy-groups"]
        if group["name"] == "TIER3-AUTO"
    )
    tier3_group["proxies"] = [*names, "REJECT"]
    return project_tier3b_file_provider(document, names)


def _projection_proxy_map(projection) -> dict[str, object]:
    provider = yaml.safe_load(projection.provider_yaml)
    return {
        str(proxy["name"]): {
            "proxy_name": str(proxy["name"]),
            "tier": "tier3",
            "pool_kind": "tier3b",
            "identity": f"identity-{proxy['name']}",
            "node": None,
            "proxy": dict(proxy),
        }
        for proxy in provider["proxies"]
        if str(proxy.get("name") or "") != "REJECT"
    }


def _contract_proxy_map(projection, *, identity: str) -> dict[str, object]:
    proxy = yaml.safe_load(projection.provider_yaml)["proxies"][0]
    name = str(proxy["name"])
    return {
        name: {
            "proxy_name": name,
            "tier": "tier3",
            "pool_kind": "tier3b",
            "identity": identity,
            "node": None,
            "proxy": proxy,
        }
    }


def _write_loaded_main_file(runtime: DesktopRuntime, payload: str) -> Path:
    runtime_config_path = runtime._runtime_config_path_for_client()
    runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_config_path.write_text(payload, encoding="utf-8")
    return runtime_config_path


def _provider_transaction_shell(tmp_path: Path):
    runtime = _runtime_shell(tmp_path)
    old_projection = _named_projection(name="old-node", server="old.example")
    candidate_projection = _named_projection(
        name="candidate-node",
        server="candidate.example",
    )
    old_map = _contract_proxy_map(old_projection, identity="old-identity")
    candidate_map = _contract_proxy_map(
        candidate_projection,
        identity="candidate-identity",
    )
    runtime._runtime_proxy_name_map = lambda: old_map
    old_contract = runtime._runtime_export_contract_for_payload(
        old_projection.main_yaml,
        provider_payload=old_projection.provider_yaml,
    )
    old_contract["runtime_config_path"] = str(
        _write_loaded_main_file(runtime, old_projection.main_yaml)
    )
    runtime.stop_event = threading.Event()
    runtime.core_runner = _RunningCore()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_loaded_export_contract = {**old_contract, "revision": 7}
    runtime._runtime_loaded_export_revision = 7
    runtime._runtime_pending_export_contract = None
    runtime._runtime_route_epoch = 11
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_egress_coordinator = _Coordinator()
    runtime._tier3_core_provider_last_projection = old_projection
    runtime._runtime_proxy_name_map = lambda: candidate_map

    provider_path = runtime._tier3_core_provider_file_path()
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_bytes(old_projection.provider_yaml.encode("utf-8"))
    return runtime, old_projection, candidate_projection, provider_path


def _group_payload(members: tuple[str, ...]) -> dict[str, object]:
    return {
        "proxies": {
            "TIER3-AUTO": {
                "all": list(members),
            }
        }
    }


def test_provider_path_resolves_to_the_file_written_by_runtime_under_core_home(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    runtime_yaml = _provider_runtime_yaml(server="old.example")

    with (
        patch.object(
            runtime,
            "_manager_for_mihomo_runtime_config",
            return_value=object(),
        ),
        patch.object(runtime, "_mihomo_tier3_export_limit", return_value=None),
        patch.object(runtime, "_default_probe_urls", return_value=()),
        patch(
            "desktop_runtime.render_subscription_route",
            return_value=("application/yaml", runtime_yaml),
        ),
    ):
        _main_yaml, projection = (
            runtime._render_mihomo_runtime_payload_base_with_provider()
        )

    assert projection is not None
    main = yaml.safe_load(projection.main_yaml)
    configured_path = Path(
        main["proxy-providers"][projection.provider_name]["path"]
    )
    core_home = runtime._resolve_path(
        runtime._client_config()["mihomo_work_dir"]
    )
    core_resolved_path = (
        configured_path
        if configured_path.is_absolute()
        else core_home / configured_path
    ).resolve()

    assert core_resolved_path == runtime._tier3_core_provider_file_path().resolve()


def test_names_only_readback_cannot_publish_changed_same_name_proxy_as_loaded(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    old_projection = project_tier3b_file_provider(
        _provider_runtime_yaml(server="old.example"),
        ["stable-name"],
    )
    candidate_projection = project_tier3b_file_provider(
        _provider_runtime_yaml(server="new.example"),
        ["stable-name"],
    )
    old_proxy = yaml.safe_load(old_projection.provider_yaml)["proxies"][0]
    candidate_proxy = yaml.safe_load(candidate_projection.provider_yaml)["proxies"][0]
    assert old_proxy["name"] == candidate_proxy["name"]
    assert old_proxy != candidate_proxy

    old_map = {
        "stable-name": {
            "proxy_name": "stable-name",
            "tier": "tier3",
            "pool_kind": "tier3b",
            "identity": "stable-identity",
            "node": None,
            "proxy": old_proxy,
        }
    }
    candidate_map = {
        "stable-name": {
            **old_map["stable-name"],
            "proxy": candidate_proxy,
        }
    }
    runtime._runtime_proxy_name_map = lambda: old_map
    old_contract = runtime._runtime_export_contract_for_payload(
        old_projection.main_yaml,
        provider_payload=old_projection.provider_yaml,
    )
    old_contract["runtime_config_path"] = str(
        _write_loaded_main_file(runtime, old_projection.main_yaml)
    )

    provider_path = runtime._tier3_core_provider_file_path()
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_text(old_projection.provider_yaml, encoding="utf-8")
    runtime.stop_event = threading.Event()
    runtime.core_runner = _RunningCore()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_loaded_export_contract = old_contract
    runtime._runtime_loaded_export_revision = 1
    runtime._runtime_pending_export_contract = None
    runtime._runtime_route_epoch = 0
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_egress_coordinator = _Coordinator()
    runtime._tier3_core_provider_last_projection = old_projection
    runtime._runtime_proxy_name_map = lambda: candidate_map

    with (
        patch.object(
            runtime,
            "_current_core_runtime_phase",
            return_value="formal_capture",
        ),
        patch.object(
            runtime,
            "_runtime_recovery_transaction_active",
            return_value=False,
        ),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    # Rejecting/degrading the provider transaction is safe.  Committing it is
    # also safe only when the names-only controller evidence is not promoted
    # into a false claim that the changed full proxy definition was loaded.
    loaded_contract = runtime._runtime_loaded_export_contract or {}
    loaded_map = loaded_contract.get("proxy_map") or {}
    published_proxy = (
        loaded_map.get("stable-name", {}).get("proxy")
        if isinstance(loaded_map, dict)
        else None
    )
    assert result["updated"] is False
    assert "names-only controller readback" in result["reason"]
    assert published_proxy != candidate_proxy, result


def test_formal_no_capture_provider_growth_uses_loaded_main_graph_without_reload(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    old_projection = _numbered_projection(7)
    candidate_projection = _numbered_projection(16)
    old_map = _projection_proxy_map(old_projection)
    candidate_map = _projection_proxy_map(candidate_projection)
    loaded_main = build_capture_disabled_clash_config(old_projection.main_yaml)
    assert yaml.safe_load(loaded_main) != yaml.safe_load(
        candidate_projection.main_yaml
    )

    runtime._runtime_proxy_name_map = lambda: old_map
    old_contract = runtime._runtime_export_contract_for_payload(
        loaded_main,
        provider_payload=old_projection.provider_yaml,
    )
    old_contract["runtime_config_path"] = str(
        _write_loaded_main_file(runtime, loaded_main)
    )
    old_contract = {**old_contract, "revision": 7}
    runtime.stop_event = threading.Event()
    runtime.core_runner = _RunningCore()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_loaded_export_contract = old_contract
    runtime._runtime_loaded_export_revision = 7
    runtime._runtime_pending_export_contract = None
    runtime._runtime_route_epoch = 11
    runtime._runtime_network_context_epoch = 3
    runtime._runtime_selector_intent_revision = 5
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    selection_signature = (
        ("FINAL-EXIT", "TIER3-AUTO"),
        ("TIER3-AUTO", "node-00"),
    )
    runtime._runtime_route_selection_signature = selection_signature
    runtime._runtime_route_attribution_barrier_epoch = 11
    runtime._runtime_route_attribution_barrier_until = 12345.0
    runtime._runtime_hot_standby_lock = threading.RLock()
    # Prefix-preserving provider growth is a recovery-supply operation and is
    # allowed through an attribution/network stabilization hold.  Otherwise a
    # missing healthy backup can permanently self-lock activation.
    runtime._runtime_network_context_hold_active = True
    runtime._runtime_network_context_hold_reason = (
        "runtime network context is stabilizing"
    )
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_egress_coordinator = _Coordinator()
    runtime._tier3_core_provider_last_projection = old_projection
    runtime._runtime_proxy_name_map = lambda: candidate_map
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_worker_running = True
    runtime._tier3_egress_activation_active_mode = "activation"
    runtime._tier3_standby_activation_hold_kind = ""
    runtime._reset_tier3_standby_activation_failure = MagicMock()
    runtime._queue_tier3_egress_activation = MagicMock()
    runtime.manager = type(
        "ManagerShell",
        (),
        {"lock": threading.RLock(), "tier3_pool_revision": 41},
    )()

    provider_path = runtime._tier3_core_provider_file_path()
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_text(old_projection.provider_yaml, encoding="utf-8")
    client = _SequencedProviderClient(
        provider_path,
        [tuple(candidate_projection.provider_member_names)],
    )
    pool_marker = ("pool", 16)
    binding = {
        "route_guard": {},
        "tier3_pool_revision": 41,
        "tier3_pool_marker": pool_marker,
        "contract_revision": 7,
        "contract_identity": id(old_contract),
        "semantic_digest": str(old_contract["semantic_digest"]),
        "route_epoch": 11,
        "network_context_epoch": 3,
        "selector_intent_revision": 5,
    }

    with (
        patch.object(
            runtime,
            "_current_core_runtime_phase",
            return_value="formal_no_capture",
        ),
        patch.object(
            runtime,
            "_runtime_recovery_transaction_active",
            return_value=False,
        ),
        patch.object(
            runtime,
            "_runtime_initial_activation_complete",
            return_value=False,
        ),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(
                candidate_projection.main_yaml,
                candidate_projection,
            ),
        ),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding",
            return_value=binding,
        ),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            return_value=True,
        ),
        patch.object(
            runtime,
            "_tier3_core_sync_marker",
            return_value=pool_marker,
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(
            runtime,
            "mihomo_proxies",
            return_value=_group_payload(
                tuple(candidate_projection.provider_member_names)
            ),
        ),
        patch.object(runtime, "_refresh_runtime_route_snapshot", return_value=None),
        patch.object(runtime, "reload_core_config") as reload_core,
        patch.object(runtime, "_hot_reload_runtime_config_phase") as hot_reload,
    ):
        result = runtime._update_running_tier3_core_provider_leased(
            marker=pool_marker
        )

    assert result["updated"] is True, result
    assert result["reloaded"] is False
    assert result["member_count"] == 16
    reload_core.assert_not_called()
    hot_reload.assert_not_called()
    assert provider_path.read_text(encoding="utf-8") == (
        candidate_projection.provider_yaml
    )
    loaded_contract = runtime._runtime_loaded_export_contract or {}
    assert loaded_contract["main_semantic_digest"] == (
        old_contract["main_semantic_digest"]
    )
    assert loaded_contract["provider_semantic_digest"] != (
        old_contract["provider_semantic_digest"]
    )
    assert len(loaded_contract.get("proxy_map") or {}) == 16
    assert runtime._runtime_route_epoch == 11
    assert runtime._runtime_route_selection_signature == selection_signature
    assert runtime._runtime_route_attribution_barrier_epoch == 11
    assert runtime._runtime_route_attribution_barrier_until == 12345.0
    runtime._reset_tier3_standby_activation_failure.assert_called_once()
    runtime._queue_tier3_egress_activation.assert_called_once()


def test_incident_provider_append_preserves_route_authority_digest(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    route_digest = "route-authority-before-capacity-growth"
    old_combined_digest = "combined-before"
    new_combined_digest = "combined-after"
    current = RuntimeNodeRef("tier3", "tier3b", "identity-a", "A")
    added = RuntimeNodeRef("tier3", "tier3b", "identity-b", "B")
    proxies_payload = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "TIER3-AUTO",
                "all": ["TIER3-AUTO", "REJECT"],
            },
            "TIER3-AUTO": {
                "type": "Fallback",
                "now": "A",
                "fixed": "A",
                "all": ["A", "B"],
            },
            "A": {"type": "Shadowsocks"},
            "B": {"type": "Shadowsocks"},
            "REJECT": {"type": "Reject"},
        }
    }
    proxy_map = {
        "A": {
            "tier": current.tier,
            "pool_kind": current.pool_kind,
            "identity": current.identity,
        },
        "B": {
            "tier": added.tier,
            "pool_kind": added.pool_kind,
            "identity": added.identity,
        },
    }
    runtime.stop_event = threading.Event()
    runtime.core_runner = MagicMock()
    runtime.core_runner.is_running.return_value = True
    runtime.core_runner.status.return_value = {
        "pid": 4242,
        "process_generation": 9,
    }
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._core_runtime_phase = "formal_capture"
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_loaded_export_revision = 7
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": old_combined_digest,
        "revision": 7,
        "proxy_map": {"A": proxy_map["A"]},
    }
    runtime._runtime_pending_export_contract = None
    runtime._runtime_route_epoch = 12
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_facility_epoch = 2
    runtime._runtime_selector_intent_revision = 6
    selection = runtime._runtime_final_exit_selection_signature(
        proxies_payload,
        proxy_map,
    )
    runtime._runtime_route_selection_signature = selection
    runtime._runtime_route_snapshot = CoreLoadedRouteSnapshot(
        process_pid=4242,
        process_generation=9,
        core_generation=3,
        semantic_digest=route_digest,
        route_epoch=12,
        network_context_epoch=4,
        proxy_nodes=(current,),
    )
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_egress_coordinator = _Coordinator()
    runtime._runtime_unknown_provider_refresh_key = None
    runtime._core_inventory_from_route_snapshot = MagicMock(return_value={})
    runtime._runtime_facility_hold_reason = lambda: ""
    pool_marker = ("pool", 25)
    runtime.manager = type(
        "ManagerShell",
        (),
        {"lock": threading.RLock(), "tier3_pool_revision": 25},
    )()
    runtime._tier3_core_sync_marker = lambda: pool_marker
    mutation_binding = {
        "pid": 4242,
        "process_generation": 9,
        "core_generation": 3,
        "phase": "formal_capture",
        "contract_revision": 7,
        "semantic_digest": old_combined_digest,
        "route_epoch": 12,
        "network_context_epoch": 4,
        "facility_epoch": 2,
        "selector_intent_revision": 6,
        "tier3_pool_revision": 25,
        "tier3_pool_marker": pool_marker,
        "route_authority_semantic_digest": route_digest,
    }
    contract = {
        "semantic_digest": new_combined_digest,
        "proxy_map": proxy_map,
    }

    committed = runtime._commit_tier3_provider_contract_from_readback(
        contract=contract,
        proxies_payload=proxies_payload,
        mutation_binding=mutation_binding,
        preserve_route_authority=True,
    )

    assert committed is not None
    assert committed.semantic_digest == route_digest
    assert committed.snapshot_key[:4] == (4242, 9, 3, route_digest)
    assert {ref.proxy_name for ref in committed.proxy_nodes} == {"A", "B"}
    loaded_contract = runtime._runtime_loaded_export_contract
    assert loaded_contract["semantic_digest"] == new_combined_digest
    assert loaded_contract["route_authority_semantic_digest"] == route_digest


def test_provider_update_defers_when_runtime_file_is_not_the_loaded_main_graph(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = provider_path.read_bytes()
    runtime_config_path = Path(
        str(runtime._runtime_loaded_export_contract["runtime_config_path"])
    )
    runtime_config_path.write_text(
        build_capture_disabled_clash_config(old_projection.main_yaml),
        encoding="utf-8",
    )

    with (
        patch.object(
            runtime,
            "_current_core_runtime_phase",
            return_value="formal_capture",
        ),
        patch.object(
            runtime,
            "_runtime_recovery_transaction_active",
            return_value=False,
        ),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(
                candidate_projection.main_yaml,
                candidate_projection,
            ),
        ),
        patch.object(runtime, "_require_api_client") as require_client,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["stale"] is True
    assert result["deferred"] is True
    assert "no longer matches" in result["reason"]
    assert provider_path.read_bytes() == old_bytes
    require_client.assert_not_called()


def test_provider_update_defers_when_file_is_not_the_loaded_provider_contract(
    tmp_path: Path,
) -> None:
    runtime, _old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    provider_path.write_text(candidate_projection.provider_yaml, encoding="utf-8")
    changed_bytes = provider_path.read_bytes()

    with (
        patch.object(
            runtime,
            "_current_core_runtime_phase",
            return_value="formal_capture",
        ),
        patch.object(
            runtime,
            "_runtime_recovery_transaction_active",
            return_value=False,
        ),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(
                candidate_projection.main_yaml,
                candidate_projection,
            ),
        ),
        patch.object(runtime, "_require_api_client") as require_client,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["stale"] is True
    assert result["deferred"] is True
    assert "provider contract" in result["reason"]
    assert provider_path.read_bytes() == changed_bytes
    require_client.assert_not_called()


def test_provider_activation_failure_restores_and_reads_back_exact_old_state(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    candidate_bytes = candidate_projection.provider_yaml.encode("utf-8")
    client = _SequencedProviderClient(
        provider_path,
        [
            ("unexpected", "REJECT"),
            ("old-node", "REJECT"),
        ],
    )

    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
        patch.object(runtime, "_tier3_provider_mutation_binding", return_value={"id": 1}),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            return_value=True,
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(
            runtime,
            "mihomo_proxies",
            return_value=_group_payload(("old-node", "REJECT")),
        ) as group_readback,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["rollback_verified"] is True
    assert result["unknown"] is False
    assert result["provider_update"]["committed"] is False
    assert result["provider_update"]["rolled_back"] is True
    assert provider_path.read_bytes() == old_bytes
    assert client.update_file_snapshots == [candidate_bytes, old_bytes, old_bytes]
    assert client.provider_readbacks == [
        ("unexpected", "REJECT"),
        ("old-node", "REJECT"),
    ]
    group_readback.assert_called_once_with(timeout_seconds=0.75)


def test_membership_mismatch_rolls_back_and_reads_back_exact_old_state(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    candidate_bytes = candidate_projection.provider_yaml.encode("utf-8")
    client = _SequencedProviderClient(
        provider_path,
        [
            ("candidate-node", "REJECT"),
            ("old-node", "REJECT"),
        ],
    )
    group_readbacks = [
        _group_payload(("unexpected", "REJECT")),
        _group_payload(("old-node", "REJECT")),
    ]

    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
        patch.object(runtime, "_tier3_provider_mutation_binding", return_value={"id": 1}),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            return_value=True,
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(runtime, "mihomo_proxies", side_effect=group_readbacks) as group_readback,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["rolled_back"] is True
    assert result["rollback_verified"] is True
    assert result["unknown"] is False
    assert provider_path.read_bytes() == old_bytes
    assert client.update_file_snapshots == [candidate_bytes, old_bytes]
    assert client.provider_readbacks == [
        ("candidate-node", "REJECT"),
        ("old-node", "REJECT"),
    ]
    assert group_readback.call_count == 2


def test_stale_provider_cas_rolls_back_and_reads_back_exact_old_state(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    candidate_bytes = candidate_projection.provider_yaml.encode("utf-8")
    client = _SequencedProviderClient(
        provider_path,
        [
            ("candidate-node", "REJECT"),
            ("old-node", "REJECT"),
        ],
    )
    group_readbacks = [
        _group_payload(("candidate-node", "REJECT")),
        _group_payload(("old-node", "REJECT")),
    ]

    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
        patch.object(runtime, "_tier3_provider_mutation_binding", return_value={"id": 1}),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            side_effect=[True, False],
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(runtime, "mihomo_proxies", side_effect=group_readbacks) as group_readback,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["stale"] is True
    assert result["rolled_back"] is True
    assert result["rollback_verified"] is True
    assert result["unknown"] is False
    assert provider_path.read_bytes() == old_bytes
    assert client.update_file_snapshots == [candidate_bytes, old_bytes]
    assert client.provider_readbacks == [
        ("candidate-node", "REJECT"),
        ("old-node", "REJECT"),
    ]
    assert group_readback.call_count == 2


@pytest.mark.parametrize("depends_on_tier3", [False, True])
def test_unknown_provider_state_revokes_attribution_without_facility_or_reject(
    tmp_path: Path,
    depends_on_tier3: bool,
) -> None:
    runtime = _runtime_shell(tmp_path)
    runtime._runtime_selector_intent_revision = 19
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_confirmed_fail_closed_snapshot_key = (1, 2, 3)
    runtime._runtime_route_snapshot = object()
    runtime._runtime_route_snapshot_payload = {"proxies": {}}
    runtime._runtime_route_selection_signature = ("old",)
    runtime._runtime_egress_coordinator = MagicMock()

    with (
        patch.object(runtime, "_record_runtime_facility_degradation") as facility,
        patch.object(
            runtime,
            "_runtime_route_depends_on_tier3_provider",
            return_value=depends_on_tier3,
        ),
        patch.object(runtime, "_enforce_runtime_fail_closed") as fail_closed,
        patch.object(runtime, "_set_effective_egress_readiness_state") as readiness,
        patch.object(runtime, "_queue_runtime_route_snapshot_refresh") as refresh,
        patch.object(runtime, "_queue_egress_revalidation") as revalidate,
    ):
        runtime._handle_unknown_tier3_provider_state("rollback is unverified")

    facility.assert_not_called()
    fail_closed.assert_not_called()
    readiness.assert_called_once_with(
        None,
        "Bastion provider inventory readback is pending",
    )
    refresh.assert_called_once_with()
    revalidate.assert_called_once_with("incremental")
    assert runtime._runtime_route_snapshot is None
    assert runtime._runtime_route_snapshot_payload is None
    assert runtime._runtime_route_selection_signature == ()
    assert runtime._runtime_confirmed_fail_closed_snapshot_key is None


def test_unverified_rollback_is_propagated_to_the_unknown_state_handler(
    tmp_path: Path,
) -> None:
    runtime, _old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    runtime._runtime_reload_gate = _ReloadGate()
    runtime._core_transition_lock = threading.RLock()
    client = _SequencedProviderClient(
        provider_path,
        [
            ("candidate-node", "REJECT"),
            ("still-candidate", "REJECT"),
        ],
    )

    with (
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
        patch.object(runtime, "_tier3_provider_mutation_binding", return_value={"id": 1}),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            return_value=True,
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(
            runtime,
            "mihomo_proxies",
            return_value=_group_payload(("unexpected", "REJECT")),
        ),
        patch.object(runtime, "_handle_unknown_tier3_provider_state") as unknown_state,
    ):
        result = runtime._update_running_tier3_core_provider(marker=("pool", 2))

    assert result["updated"] is False
    assert result["rollback_verified"] is False
    assert result["unknown"] is True
    assert "rollback readback mismatch" in result["rollback_reason"]
    unknown_state.assert_called_once_with(result["reason"])


def test_tier3_provider_dependency_follows_current_final_exit_and_gateway_underlay(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    snapshot = object()
    direct_payload = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "tier1-node",
                "all": ["tier1-node", "TIER3-AUTO", "REJECT"],
            },
            "tier1-node": {"type": "Shadowsocks"},
        },
        "proxy_map": {},
    }
    tier3_payload = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "PROXY-FALLBACK",
                "all": ["PROXY-FALLBACK", "REJECT"],
            },
            "PROXY-FALLBACK": {
                "type": "Fallback",
                "now": "TIER3-AUTO",
                "all": ["TIER3-AUTO"],
            },
            "TIER3-AUTO": {
                "type": "Fallback",
                "now": "tier3-node",
                "all": ["tier3-node", "REJECT"],
            },
            "tier3-node": {"type": "Shadowsocks"},
        },
        "proxy_map": {},
    }
    gateway_payload = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "fixed-gateway",
                "all": ["fixed-gateway", "REJECT"],
            },
            "fixed-gateway": {"type": "Shadowsocks"},
            "PROXY-FALLBACK": {
                "type": "Fallback",
                "now": "TIER3-AUTO",
                "all": ["TIER3-AUTO"],
            },
            "TIER3-AUTO": {
                "type": "Fallback",
                "now": "tier3-node",
                "all": ["tier3-node", "REJECT"],
            },
            "tier3-node": {"type": "Shadowsocks"},
        },
        "proxy_map": {
            "fixed-gateway": {
                "proxy": {"dialer-proxy": "PROXY-FALLBACK"},
            }
        },
    }

    with patch.object(
        runtime,
        "_runtime_route_snapshot_state",
        side_effect=[
            (snapshot, direct_payload),
            (snapshot, tier3_payload),
            (snapshot, gateway_payload),
        ],
    ):
        assert runtime._runtime_route_depends_on_tier3_provider() is False
        assert runtime._runtime_route_depends_on_tier3_provider() is True
        assert runtime._runtime_route_depends_on_tier3_provider() is True


def test_provider_only_sync_does_not_publish_runtime_source_listener_loaded(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    runtime.manager = None
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime.core_runner = _RunningCore()

    with (
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_tier3_core_sync_marker", return_value=("tier3", 1)),
        patch.object(
            runtime,
            "_update_running_tier3_core_provider",
            return_value={"updated": True, "semantic_digest": "provider-digest"},
        ),
        patch.object(
            runtime,
            "_mark_tier3_provider_sync_complete",
            wraps=runtime._mark_tier3_provider_sync_complete,
        ) as provider_complete,
        patch.object(runtime, "_core_export_sync_marker", return_value=("core", 1)),
        patch.object(runtime, "_queue_node_diagnostic_core_binding_refresh"),
        patch.object(runtime, "_record_tier3_core_sync_ledger"),
        patch.object(runtime, "_mark_runtime_source_transport_loaded") as listener_loaded,
        patch.object(runtime, "_mark_core_export_sync_success") as full_load_success,
    ):
        outcome = runtime._sync_core_config_after_pool_update(
            allow_live_rebase=True
        )

    assert outcome.disposition is ProviderSyncDisposition.COMMITTED
    provider_complete.assert_called_once_with(("tier3", 1))
    listener_loaded.assert_not_called()
    full_load_success.assert_not_called()


def test_provider_deferred_result_parks_without_recreating_naked_pending(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime.core_runner = _RunningCore()
    runtime._tier3_pool_sync_pending = False

    with (
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_tier3_core_sync_marker", return_value=("tier3", 4)),
        patch.object(
            runtime,
            "_update_running_tier3_core_provider",
            return_value={
                "updated": False,
                "deferred": True,
                "reason": "runtime facility hold is active",
            },
        ),
    ):
        outcome = runtime._sync_core_config_after_pool_update(
            allow_live_rebase=True
        )

    assert outcome.disposition is ProviderSyncDisposition.PARKED
    assert outcome.reason == "runtime facility hold is active"
    assert runtime._tier3_pool_sync_pending is False


def test_initial_activation_exclusively_owns_pre_ready_provider_mutation(
    tmp_path: Path,
) -> None:
    runtime = _runtime_shell(tmp_path)
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime.core_runner = _RunningCore()
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime._tier3_egress_activation_lock = threading.Lock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_active_mode = "activation"
    runtime._tier3_egress_activation_active_generation = 7
    runtime._update_running_tier3_core_provider = MagicMock(
        side_effect=AssertionError(
            "provider mutation raced the initial activation owner"
        )
    )

    outcome = runtime._sync_core_config_after_pool_update()

    assert outcome.disposition is ProviderSyncDisposition.PARKED
    assert "activation owns" in outcome.reason
    runtime._update_running_tier3_core_provider.assert_not_called()


def test_releasing_initial_activation_wakes_same_parked_provider_key() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.core_runner = _RunningCore()
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime._tier3_egress_activation_lock = threading.Lock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_active_mode = "activation"
    runtime._tier3_egress_activation_active_generation = 11
    runtime._tier3_pool_sync_lock = threading.Lock()
    runtime._tier3_pool_sync_parked_event = "append_only"
    parked_key = DeferredProviderSyncKey(
        runtime_generation=3,
        core_generation=5,
        pool_revision=9,
        provider_digest="candidate",
        loaded_digest="loaded",
        network_epoch=2,
        facility_epoch=1,
    )
    runtime._tier3_pool_sync_parked_key = parked_key
    runtime._tier3_provider_sync_key = lambda: parked_key
    queue = MagicMock(return_value=True)
    runtime._queue_tier3_pool_sync_after_commit = queue

    assert runtime._wake_parked_tier3_pool_sync_if_causal_key_changed() is False
    queue.assert_not_called()

    with runtime._tier3_egress_activation_lock:
        runtime._tier3_egress_activation_active_mode = ""
        runtime._tier3_egress_activation_active_generation = 0

    assert runtime._wake_parked_tier3_pool_sync_if_causal_key_changed() is True
    assert runtime._tier3_pool_sync_parked_key is None
    queue.assert_called_once_with(
        immediate=True,
        sync_event="append_only",
        _sync_key=parked_key,
    )


def test_parked_provider_key_runs_once_and_only_causal_change_wakes_it() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.manager = None
    runtime.logger = MagicMock()
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
    runtime._tier3_pool_sync_batch_size = 1
    runtime._tier3_pool_sync_max_wait_seconds = 0.0
    runtime._tier3_pool_sync_stable_hard_limit_seconds = 0.0
    runtime._tier3_pool_sync_cooldown_until = 0.0
    runtime._tier3_pool_sync_debounce_seconds = 0.0
    runtime._tier3_pool_sync_slow_threshold_seconds = 5.0
    runtime._tier3_pool_sync_slow_cooldown_seconds = 2.0
    runtime._tier3_runtime_qualification_stable = lambda: False
    runtime._tier3_pool_sync_formal_baseline_loaded = lambda: False
    runtime._mark_tier3_pool_sync_formal_baseline_loaded = MagicMock()
    runtime._record_tier3_core_sync_ledger = MagicMock()
    runtime._record_tier3_core_sync_event = MagicMock()

    parked_key = DeferredProviderSyncKey(
        runtime_generation=7,
        core_generation=11,
        pool_revision=23,
        provider_digest="candidate-a",
        loaded_digest="loaded-a",
        network_epoch=5,
        facility_epoch=3,
    )
    resumed_key = DeferredProviderSyncKey(
        runtime_generation=7,
        core_generation=11,
        pool_revision=23,
        provider_digest="candidate-a",
        loaded_digest="loaded-a",
        network_epoch=5,
        facility_epoch=4,
    )
    current_key = [parked_key]
    attempts: list[DeferredProviderSyncKey] = []

    runtime._tier3_provider_sync_key = lambda: current_key[0]

    def sync_once(*, expected_key: DeferredProviderSyncKey):
        attempts.append(expected_key)
        return (
            ProviderSyncDisposition.PARKED
            if len(attempts) == 1
            else ProviderSyncDisposition.NOOP
        )

    runtime._sync_core_config_after_pool_update = sync_once

    assert runtime._queue_tier3_pool_sync_after_commit() is True
    for _index in range(100):
        runtime._queue_tier3_pool_sync_after_commit()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with runtime._tier3_pool_sync_lock:
            running = runtime._tier3_pool_sync_worker_running
        if not running:
            break
        time.sleep(0.005)

    assert attempts == [parked_key]
    assert runtime._tier3_pool_sync_pending is False
    assert runtime._tier3_pool_sync_pending_key is None
    assert runtime._tier3_pool_sync_parked_key == parked_key
    assert runtime._tier3_pool_sync_worker_running is False
    assert runtime._tier3_pool_sync_thread is None
    assert runtime._record_tier3_core_sync_ledger.call_count == 1
    runtime._record_tier3_core_sync_event.assert_not_called()
    parked_messages = [
        str(call.args[0])
        for call in runtime.logger.info.call_args_list
    ]
    assert sum("provider sync parked" in message for message in parked_messages) == 1
    assert not any("completed" in message for message in parked_messages)

    assert runtime._wake_parked_tier3_pool_sync_if_causal_key_changed() is False
    assert attempts == [parked_key]

    current_key[0] = resumed_key
    assert runtime._wake_parked_tier3_pool_sync_if_causal_key_changed() is True
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with runtime._tier3_pool_sync_lock:
            running = runtime._tier3_pool_sync_worker_running
        if not running:
            break
        time.sleep(0.005)

    assert attempts == [parked_key, resumed_key]
    assert runtime._tier3_pool_sync_pending is False
    assert runtime._tier3_pool_sync_parked_key is None
    assert runtime._record_tier3_core_sync_ledger.call_count == 2
    runtime._record_tier3_core_sync_event.assert_not_called()
