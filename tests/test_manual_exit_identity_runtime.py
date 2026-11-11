from __future__ import annotations

from types import SimpleNamespace

import desktop_runtime as desktop_runtime_module
from desktop_runtime import DesktopRuntime
from exit_quality_endpoints import EXIT_IDENTITY_PROVIDERS
from runtime_egress_recovery import ManualValidationLease


def _runtime_with_routes(routes: list[dict[str, object]]) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    route_values = iter(routes)
    runtime.current_route_context = lambda **_kwargs: dict(next(route_values))
    runtime._registered_identity_urls = []
    runtime._register_runtime_internal_probe = (
        lambda url, **_kwargs: runtime._registered_identity_urls.append(url)
    )
    return runtime


def test_manual_exit_identity_is_one_observational_request(monkeypatch) -> None:
    marker = "stable-route"
    runtime = _runtime_with_routes(
        [
            {
                "binding_marker": marker,
                "execution_binding_marker": "7:11:101",
                "confirmed": True,
            },
            {
                "binding_marker": marker,
                "execution_binding_marker": "8:12:202",
                "confirmed": True,
            },
        ]
    )
    calls: list[dict[str, object]] = []

    def collect(**kwargs):
        calls.append(dict(kwargs))
        return {
            "identity": {
                "ipv4": {"state": "confirmed", "ip": "203.0.113.7"},
                "ipv6": {"state": "unavailable", "ip": ""},
            }
        }

    monkeypatch.setattr(desktop_runtime_module, "collect_exit_identity", collect)

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890",
        request_id="manual-1",
        timeout_seconds=3.0,
    )

    assert result["state"] == "complete"
    assert result["request_id"] == "manual-1"
    assert result["identity"]["ipv4"]["ip"] == "203.0.113.7"
    assert result["route"] == {
        "binding_marker": marker,
        "confirmed": True,
        "stable_during_check": True,
    }
    assert len(calls) == 1
    assert calls[0]["rounds"] == 1
    assert calls[0]["max_workers"] == 4
    assert runtime._registered_identity_urls == [
        entry[2] for entry in EXIT_IDENTITY_PROVIDERS
    ]


def test_manual_exit_identity_marks_a_route_change_without_retry(monkeypatch) -> None:
    runtime = _runtime_with_routes(
        [
            {"binding_marker": "route-a", "confirmed": True},
            {"binding_marker": "route-b", "confirmed": True},
        ]
    )
    calls = 0

    def collect(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "identity": {
                "ipv4": {"state": "confirmed", "ip": "203.0.113.8"},
            }
        }

    monkeypatch.setattr(desktop_runtime_module, "collect_exit_identity", collect)

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890",
        request_id="manual-2",
    )

    assert calls == 1
    assert result["state"] == "route_changed"
    assert result["route_changed"] is True
    assert result["freshness"]["state"] == "stale"


def test_manual_exit_identity_unavailable_does_not_start_recovery(monkeypatch) -> None:
    runtime = _runtime_with_routes(
        [
            {"binding_marker": "route-a", "confirmed": True},
            {"binding_marker": "route-a", "confirmed": True},
        ]
    )
    runtime._submit_current_route_probe_failure = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("manual identity lookup must not submit an incident")
    )
    runtime._publish_runtime_egress_readiness = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("manual identity lookup must not publish readiness")
    )
    monkeypatch.setattr(
        desktop_runtime_module,
        "collect_exit_identity",
        lambda **_kwargs: {"identity": {}},
    )

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890",
        request_id="manual-3",
    )

    assert result["state"] == "unavailable"
    assert result["error"] == "identity_unavailable"


def test_manual_exit_identity_does_not_report_unconfirmed_generation_churn(
    monkeypatch,
) -> None:
    runtime = _runtime_with_routes(
        [
            {
                "binding_marker": "route-a",
                "execution_binding_marker": "7:11:101",
                "confirmed": False,
                "facility_failure": "controller pending",
            },
            {
                "binding_marker": "route-b",
                "execution_binding_marker": "8:12:202",
                "confirmed": False,
                "facility_failure": "controller pending",
            },
        ]
    )
    monkeypatch.setattr(
        desktop_runtime_module,
        "collect_exit_identity",
        lambda **_kwargs: {
            "identity": {
                "ipv4": {"state": "confirmed", "ip": "203.0.113.9"},
            }
        },
    )

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890",
        request_id="manual-unconfirmed",
    )

    assert result["state"] == "unavailable"
    assert result["error"] == "route_unconfirmed"
    assert "route_changed" not in result
    assert result["route"]["binding_marker"] == ""


def test_bounded_profile_manual_check_uses_canonical_data_plane_validation(monkeypatch) -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_complete = lambda: True
    runtime._lifecycle_revision = 4
    runtime._runtime_route_epoch = 7
    runtime._runtime_network_context_epoch = 2
    runtime.current_route_context = lambda **_kwargs: {"binding_marker": "route-a", "confirmed": True}
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    monkeypatch.setattr(desktop_runtime_module, "collect_exit_identity", lambda **_kwargs: {
        "identity": {"ipv4": {"state": "confirmed", "ip": "8.8.8.8"}},
    })
    calls: list[dict[str, object]] = []

    def check_exit_quality(**kwargs):
        calls.append(dict(kwargs))
        return {
            "schema_version": 2,
            "request_id": kwargs["request_id"],
            "state": "pending_identity",
            "route": {"binding_marker": "route-a", "confirmed": True},
            "identity": {},
        }

    runtime.check_exit_quality = check_exit_quality

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890",
        request_id="manual-data-plane",
        timeout_seconds=3.0,
    )

    assert result["state"] == "complete"
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"
    assert calls[0]["queue_identity_diagnostic"] is False
    assert len(calls) == 1
    assert calls[0]["proxy_url"] == "http://127.0.0.1:7890"
    lease = calls[0]["manual_validation_lease"]
    assert isinstance(lease, ManualValidationLease)
    assert lease.request_id == "manual-data-plane"
    assert lease.lifecycle_revision == 4
    assert lease.route_epoch == 7
    assert lease.network_context_epoch == 2


def test_bounded_profile_manual_check_reopens_standby_admission_without_identity() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_complete = lambda: False
    attached: list[float] = []
    runtime._wait_for_manual_initial_egress_admission = (
        lambda *, deadline_monotonic: (
            attached.append(float(deadline_monotonic)) or False,
            "initial_egress_admission_pending",
        )
    )

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890",
        request_id="manual-standby",
        timeout_seconds=3.0,
    )

    assert attached
    assert result["state"] == "unavailable"
    assert result["error"] == "initial_egress_admission_pending"
    assert result["identity"] == {}


def test_short_identity_budget_reserves_final_route_readback(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(desktop_runtime_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    runtime = _runtime_with_routes([])
    route_reads = []

    def route(**_kwargs):
        route_reads.append(now[0])
        now[0] += 0.05
        return {"binding_marker": "route-a", "confirmed": True}

    runtime.current_route_context = route

    def collect(**kwargs):
        # IPv4 completes while another provider consumes the entire collection
        # budget. The successful result still needs a final route readback.
        now[0] += kwargs["total_timeout_seconds"]
        return {"identity": {"ipv4": {"state": "unconfirmed", "ip": "8.8.8.8"}}}

    monkeypatch.setattr(desktop_runtime_module, "collect_exit_identity", collect)
    result = runtime._collect_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="short-budget",
        timeout_seconds=0.7,
        proven_route={"binding_marker": "route-a", "confirmed": True},
    )

    assert result["state"] == "complete"
    assert len(route_reads) == 2
    assert now[0] < 100.7
