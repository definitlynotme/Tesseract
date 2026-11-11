"""Route-snapshot publications built by the PRODUCTION code path (10-30).

Never hand-make a ``/proxies`` payload for runtime route tests: the stored
publication is ``{"proxies": <GET /proxies response>, "proxy_map": ...}``
and a hand-made flat dict hid the ship2 exhausted_hold bug.  This helper
runs the real ``DesktopRuntime._refresh_runtime_route_snapshot`` over a
``GET /proxies`` response captured live from Mihomo 1.19.27
(``tests/fixtures/mihomo_1_19_27/proxies_<state>.json``, recaptured by
``tests/test_runtime_ship_act_arm_real_core.py``'s RealCore: TIER3-AUTO =
fallback over the TIER3B-RUNTIME provider members n1, n2, n3 + REJECT,
PROXY-FALLBACK, FINAL-EXIT, GENERAL, GLOBAL), so the snapshot and the stored
payload are exactly what production publishes.

States: ``healthy`` (3/3 alive), ``two_alive``, ``one_alive``,
``exhausted`` (0/3).  Added 11-04 (decision D): ``failover`` (n1 refused;
the members' own checks moved TIER3-AUTO to n2) and ``rebuilt`` (a provider
rewrite inside the 1 s singledo window: every member alive-by-default with
no ``extra[testUrl]``).  These two came from the same RealCore in one
scratch run; their test URL differs from the older four.  Also 11-04, from a
scratch Core with the same groups plus ``EXIT-SELECT`` and a Gateway ``gw``
whose ``dialer-proxy`` is PROXY-FALLBACK (FINAL-EXIT -> EXIT-SELECT -> gw):
``gateway_healthy``, ``gateway_failover`` (n1 refused, the underlay moved to
n2) and ``gateway_underlay_dead`` (every member refused); and ``stale_pin``
(TIER3-AUTO pinned to n3, then a provider rewrite without n3).  A Gateway
capture needs ``gw`` in the proxy map (see tests/test_runtime_ready_carry.py).
And a Selector wrapper below a fallback group (FINAL-EXIT -> PROXY-FALLBACK
-> TIER1-AUTO -> P1-SELECT -> L1; L1/L2 need map entries too):
``wrapper_alive_leaf_stale`` (L1's own record an old False, TIER1-AUTO's
round through the wrapper passed) and ``wrapper_dead_leaf_alive`` (the
reverse).
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mihomo_1_19_27"
STATES = ("healthy", "two_alive", "one_alive", "exhausted", "failover", "rebuilt",
          "gateway_healthy", "gateway_failover", "gateway_underlay_dead", "stale_pin",
          "wrapper_alive_leaf_stale", "wrapper_dead_leaf_alive")
MEMBERS = ("n1", "n2", "n3")


def captured_proxies(state: str) -> dict[str, Any]:
    """The live ``GET /proxies`` response of one state (a fresh copy)."""

    return json.loads((FIXTURES / f"proxies_{state}.json").read_text(encoding="utf-8"))


def contract_url(state: str = "healthy") -> str:
    return str(captured_proxies(state)["proxies"]["TIER3-AUTO"]["testUrl"])


class CapturedController:
    """A read-only controller answering ``GET /proxies`` (and single
    entries) from a capture; ``state`` can be switched between publications."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[str] = []

    def proxies(self, *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append("proxies")
        return captured_proxies(self.state)

    def proxy(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append("proxy:" + name)
        return copy.deepcopy(captured_proxies(self.state)["proxies"][name])

    def __getattr__(self, name: str) -> Any:
        def refused(*_a: Any, **_k: Any) -> Any:
            # Recorded before raising: a caller that swallows the error (a
            # probe helper's own try/except) still shows up in ``calls``.
            self.calls.append("refused:" + name)
            raise AssertionError(f"controller call {name} is not part of a publication")

        return refused


def _proxy_map() -> dict[str, Any]:
    return {
        name: {"proxy_name": name, "tier": "tier3", "pool_kind": "tier3b", "identity": f"identity-{name}",
               "node": None, "proxy": {"name": name, "type": "http"}}
        for name in MEMBERS
    }


def published_runtime(tmp_path: Path, state: str, *, node_judgement: dict[str, Any] | None = None) -> Any:
    """A DesktopRuntime whose route snapshot was published by the real
    ``_refresh_runtime_route_snapshot`` from the ``state`` capture.

    Returns ``SimpleNamespace(runtime, controller, publish)``;
    ``publish(state)`` switches the capture and publishes again."""

    from desktop_runtime import DesktopRuntime
    from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState

    config = {
        "client": {
            "runtime_state_path": str(tmp_path / "runtime-state.json"),
            "egress_recovery": {"node_judgement": dict(node_judgement or {"post_dial_judge": "act"})},
        },
        "alerts": {"log_file": str(tmp_path / "monitor.log"), "desktop": {"enabled": False}},
    }
    runtime = DesktopRuntime(config, project_root=tmp_path)
    for handler in list(runtime.logger.handlers):
        handler.close()
        runtime.logger.removeHandler(handler)
    logger = logging.getLogger(f"real-route-publication.{id(runtime)}")
    logger.propagate = False
    logger.handlers[:] = [logging.NullHandler()]
    runtime.logger = logger
    controller = CapturedController(state)
    runtime.api_client = controller
    runtime.core_runner = SimpleNamespace(is_running=lambda: True,
                                          status=lambda: {"pid": 4242, "process_generation": 1})
    runtime._core_generation = 1
    runtime._runtime_loaded_export_contract = {"revision": 1, "semantic_digest": "captured-1.19.27",
                                               "proxy_map": _proxy_map()}
    runtime._runtime_proxy_name_map = _proxy_map
    runtime.manager = SimpleNamespace(lock=threading.RLock(), runtime_generation=int(runtime._runtime_generation))
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY, validation_active=False)
    runtime._runtime_initial_activation_generation = 1
    runtime._core_runtime_phase = "formal_capture"

    def publish(new_state: str | None = None) -> Any:
        if new_state is not None:
            controller.state = new_state
        snapshot = runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0)
        assert snapshot is not None, "the real publication did not publish"
        return snapshot

    publish()
    return SimpleNamespace(runtime=runtime, controller=controller, publish=publish)
