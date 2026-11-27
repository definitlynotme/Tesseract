"""A rollback after READY keeps the Windows proxy on Tesseract (11-25).

Field report 11-25 (0.1.7, a Gateway exit over the fallback layers, Windows
system proxy, kill switch off): a DNS re-plan reloaded the Core while the
Gateway was briefly judged dead, so the graph moved and the incremental export
rolled capture back.  The rollback wrote back the proxy setting from before
the start, which was "off": for about seven minutes every browser request went
out directly with the link's own DNS (the reported DNS leak), and the hold
that followed was never verified again, so the service read "starting" until
the user stopped it.  TUN would not have helped: the capture-disabled config
drops TUN as well.

Driven through the real ``reload_core_config(mode="incremental_export")`` on
the verified-reload harness of ``test_runtime_reload_ready`` (real renderer,
export contracts, reload gate, and ``SystemProxyManager`` with its journal
over an in-memory registry; fake runner and controller; no Core process, no
network).
"""

from __future__ import annotations

import pytest

from runtime_egress_recovery import RuntimeEgressReadinessState
from runtime_reload_gate import RuntimeReloadReason
from system_proxy import ProxySettings
from tests.test_runtime_dns_reload_keeps_route import _grow_bastion, _plan, _settle
from tests.test_runtime_reload_ready import _loaded, _status

READY = RuntimeEgressReadinessState.READY
PRE_START = ProxySettings(enabled=False, server="", override="")


@pytest.fixture
def runtime(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    try:
        yield runtime
    finally:
        runtime.stop_event.set()


def _capture(runtime):
    """Windows points at Tesseract, as the admission's capture step leaves it."""

    applied = runtime.enable_system_proxy()
    assert applied["state"] == "applied", applied
    assert runtime.system_proxy.is_target_active()


def _registry(runtime):
    return runtime.system_proxy.backend.read()


def _served(runtime):
    """What the admission's READY commit records (_note_system_proxy_capture_served)."""

    runtime._system_proxy_capture_served_revision = runtime._lifecycle_revision


def _graph_moving_reload(runtime):
    """The 11-25 reload: a DNS re-plan whose render also moved the graph."""

    activations = []
    runtime._queue_tier3_egress_activation = lambda: activations.append(
        runtime._current_core_runtime_phase()
    )
    _plan(runtime)
    _grow_bastion(runtime)
    result = runtime.reload_core_config(
        mode="incremental_export", reason=RuntimeReloadReason.DNS_LIFECYCLE,
    )
    _settle()
    assert result.get("reloaded") is True, result
    return activations


def test_after_ready_served_through_the_proxy_a_rollback_keeps_it_and_reverifies(runtime):
    _capture(runtime)
    _served(runtime)

    activations = _graph_moving_reload(runtime)

    # The graph moved: the route is revoked and verified again, as before ...
    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert runtime._runtime_egress_readiness.state is not READY
    assert _status(runtime)[1] == "core_standby"
    # ... but Windows still sends every request to Tesseract, never direct ...
    current = _registry(runtime)
    assert current.enabled and current.server == runtime.system_proxy.proxy_server()
    assert runtime.system_proxy.active is True
    # ... and the standby gets its verifier instead of a hold nothing reopens.
    assert activations == ["formal_no_capture"]


def test_a_run_that_never_served_through_the_proxy_restores_the_pre_start_setting(runtime):
    _capture(runtime)  # a capture step whose run never reached its own READY

    activations = _graph_moving_reload(runtime)

    assert _registry(runtime) == PRE_START
    assert runtime.system_proxy.active is False
    assert activations == ["formal_no_capture"]


@pytest.mark.parametrize("change", ["stop_requested", "stopping", "next_run", "not_ours", "tun"])
def test_the_hold_never_outlives_its_run_or_keeps_a_proxy_it_does_not_own(runtime, change):
    _capture(runtime)
    _served(runtime)
    assert runtime._system_proxy_capture_hold_applies() is True
    other_client = ProxySettings(enabled=True, server="127.0.0.1:7897", override="")
    if change == "stop_requested":
        runtime._stop_request_pending = True
    elif change == "stopping":
        runtime.stop_event.set()
    elif change == "next_run":
        runtime._lifecycle_revision += 1
    elif change == "not_ours":
        runtime.system_proxy.backend.write(other_client)
    else:
        runtime.network_mode = "tun"

    assert runtime._system_proxy_capture_hold_applies() is False
    assert runtime._release_system_proxy_capture_for_rollback("test") is False
    # Restored as before; another client's setting is never touched.
    assert _registry(runtime) == (other_client if change == "not_ours" else PRE_START)
