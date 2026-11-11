"""Overview exit check: usable degraded exits, lifecycle-aware clicks and tones."""
from __future__ import annotations

import pytest

from system_proxy import ProxySettings, SystemProxyManager
from overview_validation_view import (
    overview_lookup_display,
    overview_result_awaits_connection,
    overview_service_availability,
    overview_visible_identity,
)
from tests.test_overview_validation_contract import _identity, _route
from tests.test_overview_validation_integration import _configure_local_identity
from tests.test_runtime_sudden_failure import _Origin, _start_and_verify, local_runtime


# ---- presentation policy -------------------------------------------------


def test_degraded_exit_keeps_identity_with_a_warning_not_an_error():
    result = {
        "state": "partial", "route": _route(), **_identity(),
        "diagnostics": {"overview_warning_reason": "system_proxy_capture_inactive"},
    }
    display = overview_lookup_display(result)
    assert display.state == "partial"
    assert display.tone == "warning"
    assert "系统代理未指向本应用" in display.message
    assert overview_visible_identity(result)["identity"]["ipv4"]["ip"] == "8.8.8.8"


def test_warning_never_masks_a_failed_request():
    result = {
        "state": "partial", "error": "partial_validation",
        "diagnostics": {
            "overview_failure_reason": "partial_validation",
            "overview_warning_reason": "partial_validation",
        },
    }
    display = overview_lookup_display(result)
    assert display.message == "部分校验站点未响应，请稍后刷新"
    assert display.tone == "error"


@pytest.mark.parametrize("reason,tone", [
    ("connection_not_ready", "warning"),
    ("waiting_connection", "warning"),
    ("validation_cancelled", "warning"),
    ("exit_unreachable", "error"),
    ("identity_timeout", "error"),
    ("probe_busy", "error"),
])
def test_failure_tone_separates_waiting_from_failing(reason, tone):
    display = overview_lookup_display({
        "state": "unavailable", "error": reason,
        "diagnostics": {"overview_failure_reason": reason},
    })
    assert display.tone == tone


@pytest.mark.parametrize("status,busy,expected", [
    ({}, (), "ready"),
    ({"readiness_state": "ready", "route_context": {}}, (), "ready"),
    ({"startup_phase": "stopped"}, (), "offline"),
    ({"startup_phase": "failed", "core": {"running": False}}, (), "offline"),
    ({"startup_phase": "stopping", "core_running": True}, (), "offline"),
    ({"startup_phase": "collecting"}, (), "starting"),
    ({"startup_phase": "core_standby", "core": {"running": True}}, (), "ready"),
    ({"startup_phase": "running", "core_running": True}, (), "ready"),
    ({"startup_phase": "stopped"}, ("start-services",), "starting"),
    ({"startup_phase": "running", "core_running": True}, ("stop-services",), "offline"),
])
def test_service_availability_only_blocks_explicit_lifecycle_states(status, busy, expected):
    assert overview_service_availability(status, busy_tasks=busy) == expected


@pytest.mark.parametrize("diagnostics,expected", [
    ({"overview_failure_reason": "waiting_connection"}, True),
    ({"overview_failure_reason": "connection_not_ready", "data_plane_reason": "runtime_route_not_ready"}, True),
    ({"overview_failure_reason": "connection_not_ready", "data_plane_reason": "core_not_running"}, True),
    ({"overview_failure_reason": "connection_not_ready",
      "data_plane_reason": "FINAL-EXIT activation ended without a verified route (RuntimeError)"}, False),
    ({"overview_failure_reason": "validation_timeout"}, False),
    ({}, False),
])
def test_only_connection_building_results_wait_for_readiness(diagnostics, expected):
    assert overview_result_awaits_connection({"diagnostics": diagnostics}) is expected


# ---- runtime request -------------------------------------------------------


def test_real_core_shows_identity_when_another_client_took_over_windows_proxy(
    local_runtime, monkeypatch,
):
    runtime, proxies = local_runtime

    class MemoryProxyBackend:
        settings = ProxySettings(enabled=False, server="", override="")

        def read(self):
            return self.settings

        def write(self, settings):
            self.settings = settings

    backend = MemoryProxyBackend()
    runtime.config["client"]["system_proxy"]["enabled"] = True
    runtime.system_proxy = SystemProxyManager(
        {"enabled": True, "host": "127.0.0.1", "port": runtime._client_mixed_port(runtime._client_config())},
        backend=backend, platform_name="windows",
    )
    _start_and_verify(runtime, proxies)
    assert runtime.system_proxy.is_target_active()
    # Another proxy client rewrites the Windows setting after READY.
    backend.settings = ProxySettings(enabled=True, server="127.0.0.1:10808", override="")

    original_get = _Origin.do_GET

    def identity_get(self):
        if self.path != "/ip":
            return original_get(self)
        body = b"8.8.8.8"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    monkeypatch.setattr(_Origin, "do_GET", identity_get)
    _configure_local_identity(runtime, monkeypatch, tuple(
        (f"local-identity-{index}", 4, url.replace("/generate_204", "/ip"))
        for index, url in enumerate(runtime._default_probe_urls())
    ))
    result = runtime.check_current_exit_identity(
        proxy_url=f"http://127.0.0.1:{runtime._client_mixed_port(runtime._client_config())}",
        request_id="taken-over", timeout_seconds=8,
    )

    assert result["state"] == "partial", result
    assert "error" not in result
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"
    assert result["route"]["confirmed"] is True
    diagnostics = result["diagnostics"]
    assert diagnostics["overview_warning_reason"] == "system_proxy_capture_inactive"
    assert diagnostics["system_proxy_capture"]["target_active"] is False
    assert diagnostics["neutral_connectivity"]["success_count"] == 2
    # The lookup reports the takeover; it never rewrites the user's setting.
    assert backend.settings.server == "127.0.0.1:10808"
