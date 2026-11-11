"""Regression coverage for Tier3B provider transaction mutation guards."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tier3_core_provider import project_tier3b_file_provider
from tests.test_desktop_runtime_tier3_provider_contract import (
    _Coordinator,
    _RunningCore,
    _SequencedProviderClient,
    _contract_proxy_map,
    _named_projection,
    _provider_transaction_shell,
    _runtime_shell,
    _write_loaded_main_file,
)


def _candidate_projection_with_old_member():
    # An append must preserve the exact old configuration, including credentials.
    old_proxy = yaml.safe_load(_named_projection(
        name="old-node", server="old.example",
    ).provider_yaml)["proxies"][0]
    candidate_proxy = yaml.safe_load(_named_projection(
        name="candidate-node", server="candidate.example",
    ).provider_yaml)["proxies"][0]
    document = {
        "mixed-port": 7890,
        "proxies": [old_proxy, candidate_proxy],
        "proxy-groups": [
            {
                "name": "TIER3-AUTO",
                "type": "fallback",
                "proxies": ["old-node", "candidate-node", "REJECT"],
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
    }
    return project_tier3b_file_provider(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        ["old-node", "candidate-node"],
    )


def _transaction_case(tmp_path: Path):
    runtime = _runtime_shell(tmp_path)
    old_projection = _named_projection(name="old-node", server="old.example")
    candidate_projection = _candidate_projection_with_old_member()
    old_map = _contract_proxy_map(old_projection, identity="old-identity")
    candidate_map = {
        **old_map,
        **_contract_proxy_map(
            _named_projection(
                name="candidate-node",
                server="candidate.example",
            ),
            identity="candidate-identity",
        ),
    }
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
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_loaded_export_contract = {**old_contract, "revision": 7}
    runtime._runtime_loaded_export_revision = 7
    runtime._runtime_pending_export_contract = None
    runtime._runtime_route_epoch = 11
    runtime._runtime_network_context_epoch = 0
    runtime._runtime_selector_intent_revision = 0
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


def _route_payload(
    members: tuple[str, ...],
    *,
    tier3_now: str = "old-node",
    tier3_fixed: str = "old-node",
    final_now: str = "TIER3-AUTO",
) -> dict[str, object]:
    return {
        "proxies": {
            "TIER3-AUTO": {
                "type": "Fallback",
                "all": list(members),
                "now": tier3_now,
                "fixed": tier3_fixed,
            },
            "FINAL-EXIT": {
                "type": "Selector",
                "all": ["TIER3-AUTO", "REJECT"],
                "now": final_now,
            },
            "old-node": {"type": "Shadowsocks"},
            "candidate-node": {"type": "Shadowsocks"},
            "REJECT": {"type": "Reject"},
        }
    }


@pytest.mark.parametrize(
    "changed_route",
    [
        _route_payload(
            ("old-node", "candidate-node", "REJECT"),
            tier3_now="candidate-node",
            tier3_fixed="candidate-node",
        ),
        _route_payload(
            ("old-node", "candidate-node", "REJECT"),
            tier3_fixed="candidate-node",
        ),
        _route_payload(
            ("old-node", "candidate-node", "REJECT"),
            final_now="REJECT",
        ),
    ],
    ids=["tier3-now", "tier3-fixed", "final-exit-chain"],
)
def test_provider_route_drift_rolls_back_file_membership_and_selection(
    tmp_path: Path,
    changed_route: dict[str, object],
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _transaction_case(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    candidate_bytes = candidate_projection.provider_yaml.encode("utf-8")
    old_route = _route_payload(("old-node", "REJECT"))
    previous_route_guard = runtime._tier3_provider_route_guard(
        {"proxies": old_route}
    )
    client = _SequencedProviderClient(
        provider_path,
        [
            ("old-node", "candidate-node", "REJECT"),
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
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding",
            return_value={"route_guard": previous_route_guard},
        ),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            return_value=True,
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(
            runtime,
            "mihomo_proxies",
            side_effect=[changed_route, old_route],
        ),
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["rolled_back"] is True
    assert result["rollback_verified"] is True
    assert result["unknown"] is False
    assert "changed fixed/selected route state" in result["reason"]
    assert provider_path.read_bytes() == old_bytes
    assert client.update_file_snapshots == [candidate_bytes, old_bytes]
    assert runtime._runtime_loaded_export_contract["revision"] == 7


def test_manager_marker_change_before_final_publication_rolls_back_without_new_contract(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _transaction_case(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    old_contract = runtime._runtime_loaded_export_contract
    old_route = _route_payload(("old-node", "REJECT"))
    candidate_route = _route_payload(
        ("old-node", "candidate-node", "REJECT")
    )
    previous_route_guard = runtime._tier3_provider_route_guard(
        {"proxies": old_route}
    )
    runtime.manager = type(
        "ManagerShell",
        (),
        {"lock": threading.RLock(), "tier3_pool_revision": 41},
    )()
    binding = {
        "route_guard": previous_route_guard,
        "tier3_pool_revision": 41,
        "tier3_pool_marker": ("pool", "before"),
        "contract_revision": 7,
        "contract_identity": id(old_contract),
        "semantic_digest": str(old_contract.get("semantic_digest") or ""),
        "route_epoch": 11,
        "network_context_epoch": 0,
        "selector_intent_revision": 0,
    }
    client = _SequencedProviderClient(
        provider_path,
        [
            ("old-node", "candidate-node", "REJECT"),
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
        patch.object(runtime, "_tier3_provider_mutation_binding", return_value=binding),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
            return_value=True,
        ),
        patch.object(runtime, "_tier3_core_sync_marker", return_value=("pool", "after")),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(
            runtime,
            "mihomo_proxies",
            side_effect=[candidate_route, old_route],
        ),
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result["updated"] is False
    assert result["stale"] is True
    assert result["rolled_back"] is True
    assert result["rollback_verified"] is True
    assert "atomically published" in result["reason"]
    assert provider_path.read_bytes() == old_bytes
    assert runtime._runtime_loaded_export_contract is old_contract
    loaded_map = runtime._runtime_loaded_export_contract.get("proxy_map") or {}
    assert "candidate-node" not in loaded_map


def test_active_fixed_tier3_lease_target_removal_defers_before_provider_mutation(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = {
        "group": "TIER3-AUTO",
        "mode": "fixed",
        "target_member": "old-node",
        "incident_id": "incident-1",
    }

    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
        patch.object(runtime, "_tier3_provider_mutation_binding") as bind,
        patch.object(runtime, "_require_api_client") as require_client,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result == {
        "updated": False,
        "deferred": True,
        "reason": (
            "Tier3B provider projection would remove the incident-owned "
            "fixed route target"
        ),
    }
    assert provider_path.read_bytes() == old_bytes
    bind.assert_not_called()
    require_client.assert_not_called()


def test_provider_update_defers_during_facility_hold_before_any_mutation(
    tmp_path: Path,
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    route_epoch = int(runtime._runtime_route_epoch)
    loaded_revision = int(runtime._runtime_loaded_export_revision)

    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(
            runtime,
            "_runtime_facility_hold_reason",
            return_value="runtime network context is stabilizing",
        ),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ) as render,
        patch.object(runtime, "_tier3_provider_mutation_binding") as bind,
        patch.object(runtime, "_require_api_client") as require_client,
        patch(
            "desktop_runtime.replace_provider_file_transactionally"
        ) as provider_transaction,
        patch.object(runtime, "_hot_reload_runtime_config_phase") as reload_config,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result == {
        "updated": False,
        "deferred": True,
        "reason": "runtime facility hold is active",
    }
    assert provider_path.read_bytes() == old_bytes
    assert runtime._runtime_route_epoch == route_epoch
    assert runtime._runtime_loaded_export_revision == loaded_revision
    # The runtime must render/read the candidate to distinguish an allowed
    # prefix-preserving append from a destructive replacement.  This case
    # replaces the only loaded member, so it still stops before controller I/O.
    render.assert_called_once()
    bind.assert_called_once()
    require_client.assert_not_called()
    provider_transaction.assert_not_called()
    reload_config.assert_not_called()


@pytest.mark.parametrize(
    "protected_route_guard",
    [
        {"tier3_now": "old-node", "tier3_fixed": "candidate-node"},
        {"tier3_now": "candidate-node", "tier3_fixed": "old-node"},
    ],
    ids=["current-member", "fixed-member"],
)
def test_provider_candidate_removing_current_or_fixed_member_defers_before_write(
    tmp_path: Path,
    protected_route_guard: dict[str, str],
) -> None:
    runtime, old_projection, candidate_projection, provider_path = (
        _provider_transaction_shell(tmp_path)
    )
    old_bytes = old_projection.provider_yaml.encode("utf-8")
    route_epoch = int(runtime._runtime_route_epoch)
    loaded_revision = int(runtime._runtime_loaded_export_revision)

    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(runtime, "_runtime_facility_hold_reason", return_value=""),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(candidate_projection.main_yaml, candidate_projection),
        ),
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding",
            return_value={"route_guard": protected_route_guard},
        ) as bind,
        patch.object(
            runtime,
            "_tier3_provider_mutation_binding_is_current",
        ) as binding_is_current,
        patch.object(runtime, "_require_api_client") as require_client,
        patch(
            "desktop_runtime.replace_provider_file_transactionally"
        ) as provider_transaction,
        patch.object(runtime, "_hot_reload_runtime_config_phase") as reload_config,
    ):
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

    assert result == {
        "updated": False,
        "deferred": True,
        "reason": (
            "Tier3B provider projection would remove the current or fixed "
            "Core route member"
        ),
    }
    assert provider_path.read_bytes() == old_bytes
    assert runtime._runtime_route_epoch == route_epoch
    assert runtime._runtime_loaded_export_revision == loaded_revision
    bind.assert_called_once_with(None)
    binding_is_current.assert_not_called()
    require_client.assert_not_called()
    provider_transaction.assert_not_called()
    reload_config.assert_not_called()
