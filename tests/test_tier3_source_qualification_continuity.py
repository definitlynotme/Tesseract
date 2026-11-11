"""A provider capacity update must not erase a verified source route."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from runtime_egress_recovery import CoreLoadedRouteSnapshot, RuntimeNodeRef
from runtime_source_transport import (
    RuntimeSourceListenerPorts,
    RuntimeSourceRouteKind,
    build_runtime_source_transport_generation,
)
from tests.test_desktop_runtime_tier3_provider_contract import (
    _SequencedProviderClient,
)
from tests.test_desktop_runtime_tier3_provider_transaction_regressions import (
    _route_payload,
    _transaction_case,
)
from tests.test_runtime_source_demand import _finish, _runtime


@pytest.mark.parametrize("changed", [
    "none", "route", "stop", "mutation", "worker", "probe", "readback", "readback_missing",
])
def test_source_demand_renews_source_lease_only_for_current_route(changed):
    runtime, old_snapshot, loaded_payload, old_binding = _runtime()
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    assert runtime._set_tier3_source_control_qualification(old_snapshot, old_binding)

    snapshot = replace(old_snapshot, route_epoch=old_snapshot.route_epoch + 1)
    binding = replace(old_binding, route_epoch=snapshot.route_epoch)
    runtime._runtime_route_snapshot = snapshot
    runtime._refresh_loaded_runtime_authority.return_value = (snapshot, loaded_payload, binding)
    assert not runtime._source_control_final_exit_ready(runtime.manager)
    runtime.manager.notify_tier3_source_supply_route_changed.reset_mock()

    def probe(**_kwargs):
        if changed == "route":
            runtime._runtime_route_snapshot = replace(
                snapshot, route_epoch=snapshot.route_epoch + 1,
            )
        elif changed == "stop":
            runtime.stop_event.set()
        elif changed == "mutation":
            runtime._core_route_mutation_is_pending = lambda: True
        elif changed == "worker":
            runtime._runtime_worker_token_is_current.return_value = False
        elif changed == "probe":
            raise TimeoutError("neutral probe failed")
        elif changed == "readback":
            runtime._refresh_loaded_runtime_authority.return_value = (
                replace(snapshot, route_epoch=snapshot.route_epoch + 1), {}, binding,
            )
        elif changed == "readback_missing":
            runtime._refresh_loaded_runtime_authority.return_value = None
        return {"status": 204}

    runtime._probe_mihomo_mixed_port_once.side_effect = probe
    runtime._request_runtime_source_qualification("core")
    _finish(runtime)

    assert runtime._source_control_final_exit_ready(runtime.manager) is (changed == "none")
    if changed == "none":
        runtime.manager.notify_tier3_source_supply_route_changed.assert_called_once_with(
            "verified_final_exit_ready",
        )
        assert runtime._tier3_source_control_qualification.snapshot_key == snapshot.snapshot_key
    else:
        runtime.manager.notify_tier3_source_supply_route_changed.assert_not_called()


@contextmanager
def _verified_append_case(tmp_path, *, qualified=True):
    runtime, old_projection, projection, provider_path = _transaction_case(tmp_path)
    runtime.core_runner = MagicMock()
    runtime.core_runner.is_running.return_value = True
    runtime.core_runner.status.return_value = {"pid": 4242, "process_generation": 9}
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._runtime_generation = 4
    runtime._core_runtime_phase = "formal_capture"
    runtime._core_route_mutation_pending = False
    runtime._runtime_facility_epoch = 0
    runtime._runtime_incident_recovery_state = {}
    runtime.manager = SimpleNamespace(
        lock=threading.RLock(),
        tier3_pool_revision=8,
        notify_tier3_source_supply_route_changed=MagicMock(),
    )
    old_contract = runtime._runtime_loaded_export_contract
    old_map = old_contract["proxy_map"]
    old_payload = _route_payload(tuple(old_projection.provider_member_names))
    graph, _, states = runtime._runtime_group_graph_and_selection(old_payload)
    old_snapshot = CoreLoadedRouteSnapshot(
        process_pid=4242,
        process_generation=9,
        core_generation=3,
        semantic_digest=old_contract["semantic_digest"],
        route_epoch=11,
        network_context_epoch=0,
        group_graph=graph,
        group_states=states,
        proxy_nodes=tuple(
            RuntimeNodeRef(info["tier"], info["pool_kind"], info["identity"], name)
            for name, info in old_map.items()
        ),
    )
    runtime._runtime_route_snapshot = old_snapshot
    runtime._runtime_route_snapshot_payload = {
        "proxies": old_payload, "proxy_map": old_map,
    }
    runtime._runtime_route_selection_signature = (
        runtime._runtime_final_exit_selection_signature(old_payload, old_map)
    )
    binding = runtime._runtime_route_binding_for_outbound(
        old_snapshot, runtime._runtime_route_snapshot_payload, "FINAL-EXIT"
    )
    assert binding is not None
    if qualified:
        assert runtime._set_tier3_source_control_qualification(old_snapshot, binding)
        assert runtime._source_control_final_exit_ready(runtime.manager)
    client = _SequencedProviderClient(
        provider_path, [tuple(projection.provider_member_names)]
    )
    with (
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(runtime, "_runtime_facility_hold_reason", return_value=""),
        patch.object(runtime, "_runtime_uses_early_single_coordinator", return_value=True),
        patch.object(runtime, "_runtime_initial_activation_complete", return_value=True),
        patch.object(runtime, "_wake_runtime_facility_rebaseline"),
        patch.object(runtime, "_core_inventory_from_route_snapshot", return_value={}),
        patch.object(runtime, "_tier3_core_sync_marker", return_value=("pool", 8)),
        patch.object(runtime, "_runtime_source_transport_enabled", return_value=False),
        patch.object(
            runtime, "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(projection.main_yaml, projection, runtime._runtime_proxy_name_map()),
        ),
        patch.object(
            runtime, "_refresh_runtime_route_snapshot",
            side_effect=lambda **_kwargs: runtime._runtime_route_snapshot,
        ),
        patch.object(runtime, "_require_api_client", return_value=client),
        patch.object(
            runtime, "mihomo_proxies",
            return_value=_route_payload(tuple(projection.provider_member_names)),
        ) as readback,
        patch.object(runtime, "reload_core_config") as reload_core,
    ):
        yield runtime, old_snapshot, readback, reload_core


def test_verified_append_retains_source_proof_without_reverification(tmp_path):
    with _verified_append_case(tmp_path) as (runtime, old_snapshot, _, reload_core):
        proof = runtime._tier3_source_control_qualification
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

        assert result["updated"] is True, result
        assert runtime._runtime_loaded_export_contract["semantic_digest"] != (
            old_snapshot.semantic_digest
        )
        assert runtime._source_control_final_exit_ready(runtime.manager)
        assert runtime._runtime_route_snapshot.snapshot_key == old_snapshot.snapshot_key
        assert runtime._tier3_source_control_qualification is proof
        runtime.manager.notify_tier3_source_supply_route_changed.assert_called_once_with(
            "verified_final_exit_ready"
        )
        reload_core.assert_not_called()


@pytest.mark.parametrize("phase", ["formal_capture", "formal_no_capture"])
def test_private_final_exit_endpoint_lease_survives_verified_append(tmp_path, phase):
    with _verified_append_case(tmp_path) as (runtime, _, _, _):
        runtime._core_runtime_phase = phase
        transport = build_runtime_source_transport_generation(
            runtime_generation=str(runtime._runtime_generation),
            config_generation=3,
            ports=RuntimeSourceListenerPorts(final_exit=18101, direct=18102),
        )
        with (
            patch.object(runtime, "_runtime_source_transport_enabled", return_value=True),
            patch.object(runtime, "_runtime_source_loaded_transport_snapshot", return_value=transport),
            patch.object(runtime, "_runtime_source_listener_requires_auth", return_value=True),
        ):
            before = runtime._runtime_source_fetch_endpoints_provider("core")
            assert before[0].route_kind is RuntimeSourceRouteKind.FINAL_EXIT
            assert runtime._update_running_tier3_core_provider_leased(marker=None)["updated"]
            after = runtime._runtime_source_fetch_endpoints_provider("core")
            assert after == before
            runtime._revoke_tier3_source_control_qualification("confirmed_failure")
            assert all(
                endpoint.route_kind is not RuntimeSourceRouteKind.FINAL_EXIT
                for endpoint in runtime._runtime_source_fetch_endpoints_provider("core")
                if endpoint.ready
            )


@pytest.mark.parametrize("stale", ["no-proof", "runtime", "network", "revoked"])
def test_append_cannot_create_or_resurrect_source_proof(tmp_path, stale):
    with _verified_append_case(tmp_path, qualified=stale != "no-proof") as (
        runtime, old_snapshot, _, reload_core,
    ):
        if stale == "runtime":
            runtime._runtime_generation += 1
        elif stale == "network":
            runtime._runtime_network_context_epoch += 1
            runtime._runtime_route_snapshot = replace(old_snapshot, network_context_epoch=1)
        elif stale == "revoked":
            runtime._revoke_tier3_source_control_qualification("confirmed_failure")
        assert not runtime._source_control_final_exit_ready(runtime.manager)

        result = runtime._update_running_tier3_core_provider_leased(marker=None)

        assert result["updated"] is True, result
        assert not runtime._source_control_final_exit_ready(runtime.manager)
        assert "route_authority_semantic_digest" not in runtime._runtime_loaded_export_contract
        reload_core.assert_not_called()


@pytest.mark.parametrize("changed", ["selection", "network", "stop", "revoked"])
def test_append_does_not_hide_concurrent_authority_loss(tmp_path, changed):
    with _verified_append_case(tmp_path) as (runtime, _, readback, reload_core):
        old_contract = runtime._runtime_loaded_export_contract
        candidate_payload = readback.return_value

        def changed_readback(**_kwargs):
            if changed == "selection":
                return _route_payload(
                    ("old-node", "candidate-node", "REJECT"),
                    tier3_now="candidate-node", tier3_fixed="candidate-node",
                )
            if changed == "network":
                runtime._runtime_network_context_epoch += 1
            elif changed == "stop":
                runtime.stop_event.set()
            else:
                runtime._revoke_tier3_source_control_qualification("confirmed_failure")
            return candidate_payload

        readback.side_effect = changed_readback
        result = runtime._update_running_tier3_core_provider_leased(marker=None)

        if changed == "revoked":
            assert result["updated"] is True, result
            assert not runtime._source_control_final_exit_ready(runtime.manager)
        else:
            assert result["updated"] is False, result
            assert runtime._runtime_loaded_export_contract is old_contract
        reload_core.assert_not_called()
