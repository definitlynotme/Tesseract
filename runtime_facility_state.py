from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable


class RuntimeListenerKind(str, Enum):
    MIXED = "mixed"
    CONTROLLER = "controller"
    DNS = "dns"
    SOURCE_PRIVATE = "source_private"
    UNKNOWN = "unknown"


class RuntimeFacilityImpact(str, Enum):
    DATA_PLANE = "data_plane"
    CAPTURE_ONLY = "capture_only"
    SOURCE_ONLY = "source_only"
    ATTRIBUTION_ONLY = "attribution_only"


_LOCAL_ENDPOINT_RE = re.compile(
    r"(?P<host>127(?:\.\d{1,3}){3}|0\.0\.0\.0|localhost|\[?::1?\]?):(?P<port>\d{1,5})",
    re.IGNORECASE,
)
_BIND_FAILURE_MARKERS = (
    "address already in use",
    "only one usage",
    "access permissions",
    "permission denied",
    "bind:",
)
_LISTENER_MARKERS = ("listen ", "listener", "dns server")


@dataclass(frozen=True, slots=True)
class RuntimeListenerFault:
    token: str
    kind: RuntimeListenerKind
    impact: RuntimeFacilityImpact
    endpoint: str
    runtime_generation: int
    core_generation: int
    observed_at_monotonic: float
    retry_stage: int = 0
    next_probe_at_monotonic: float = 0.0

    @property
    def blocks_data_plane(self) -> bool:
        return self.impact is RuntimeFacilityImpact.DATA_PLANE

    @property
    def blocks_capture(self) -> bool:
        return self.impact in {
            RuntimeFacilityImpact.DATA_PLANE,
            RuntimeFacilityImpact.CAPTURE_ONLY,
        }

    @property
    def blocks_node_attribution(self) -> bool:
        return self.impact in {
            RuntimeFacilityImpact.DATA_PLANE,
            RuntimeFacilityImpact.ATTRIBUTION_ONLY,
        }


@dataclass(frozen=True, slots=True)
class RuntimeFacilitySnapshot:
    faults: tuple[RuntimeListenerFault, ...]

    @property
    def data_plane_blocked(self) -> bool:
        return any(fault.blocks_data_plane for fault in self.faults)

    @property
    def capture_blocked(self) -> bool:
        return any(fault.blocks_capture for fault in self.faults)

    @property
    def node_attribution_blocked(self) -> bool:
        return any(fault.blocks_node_attribution for fault in self.faults)

    @property
    def blocking_reason(self) -> str:
        for fault in self.faults:
            if fault.blocks_data_plane:
                return f"mihomo {fault.kind.value} listener is unavailable"
        return ""

    @property
    def attribution_reason(self) -> str:
        for fault in self.faults:
            if fault.blocks_node_attribution:
                return f"mihomo {fault.kind.value} listener cannot support safe attribution"
        return ""


def _canonical_host(value: str) -> str:
    host = str(value or "").strip().casefold().strip("[]")
    return "127.0.0.1" if host in {"localhost", "::1"} else host


def _canonical_endpoint(host: str, port: int) -> str:
    return f"{_canonical_host(host)}:{max(0, int(port))}"


def _normalise_endpoints(values: Iterable[str]) -> set[str]:
    endpoints: set[str] = set()
    for value in values:
        match = _LOCAL_ENDPOINT_RE.search(str(value or ""))
        if match is None:
            continue
        endpoints.add(
            _canonical_endpoint(match.group("host"), int(match.group("port")))
        )
    return endpoints


def classify_runtime_listener_fault(
    raw_line: str,
    *,
    runtime_generation: int,
    core_generation: int,
    observed_at_monotonic: float,
    network_mode: str,
    mixed_endpoint: str,
    controller_endpoint: str,
    dns_endpoint: str,
    source_endpoints: Iterable[str] = (),
) -> RuntimeListenerFault | None:
    """Classify a local Mihomo bind failure without treating every listener as egress.

    The returned token contains no raw log text. Unknown local listeners block
    node attribution only; a neutral mixed-port success may still prove the
    current FINAL-EXIT data plane.
    """

    text = str(raw_line or "")
    lowered = text.casefold()
    if not any(marker in lowered for marker in _LISTENER_MARKERS):
        return None
    if "bind" not in lowered or not any(
        marker in lowered for marker in _BIND_FAILURE_MARKERS
    ):
        return None
    match = _LOCAL_ENDPOINT_RE.search(text)
    if match is None:
        return None
    port = int(match.group("port"))
    if not 0 < port <= 65535:
        return None
    endpoint = _canonical_endpoint(match.group("host"), port)
    mixed = _normalise_endpoints((mixed_endpoint,))
    controller = _normalise_endpoints((controller_endpoint,))
    dns = _normalise_endpoints((dns_endpoint,))
    sources = _normalise_endpoints(source_endpoints)
    if endpoint in mixed:
        kind = RuntimeListenerKind.MIXED
        impact = RuntimeFacilityImpact.DATA_PLANE
    elif endpoint in controller:
        kind = RuntimeListenerKind.CONTROLLER
        # Losing the controller revokes selector/readback authority, but it
        # does not prove that the already-loaded mixed-port data plane is down.
        # Treating it as a data-plane outage made a healthy route appear
        # offline and could permanently gate manual validation.
        impact = RuntimeFacilityImpact.ATTRIBUTION_ONLY
    elif endpoint in dns:
        kind = RuntimeListenerKind.DNS
        mode = str(network_mode or "system_proxy").strip().casefold().replace("-", "_")
        impact = (
            RuntimeFacilityImpact.DATA_PLANE
            if mode in {"tun", "mixed"}
            else RuntimeFacilityImpact.CAPTURE_ONLY
        )
    elif endpoint in sources:
        kind = RuntimeListenerKind.SOURCE_PRIVATE
        impact = RuntimeFacilityImpact.SOURCE_ONLY
    else:
        kind = RuntimeListenerKind.UNKNOWN
        impact = RuntimeFacilityImpact.ATTRIBUTION_ONLY
    token_material = (
        int(runtime_generation),
        int(core_generation),
        kind.value,
        endpoint,
    )
    token = hashlib.sha256(repr(token_material).encode("utf-8")).hexdigest()[:20]
    observed = float(observed_at_monotonic)
    if not math.isfinite(observed):
        raise ValueError("observed_at_monotonic must be finite")
    return RuntimeListenerFault(
        token=token,
        kind=kind,
        impact=impact,
        endpoint=endpoint,
        runtime_generation=max(0, int(runtime_generation)),
        core_generation=max(0, int(core_generation)),
        observed_at_monotonic=observed,
        retry_stage=0,
        next_probe_at_monotonic=observed + 1.0,
    )


class RuntimeLocalFacilityState:
    """Runtime-scoped listener faults with bounded, causal recovery.

    Repeated stdout lines for the same generation/endpoint never extend the
    first retry deadline. A successful readiness proof clears only its matching
    token, so an old callback cannot reopen a new Core generation.
    """

    _RETRY_DELAYS = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)

    def __init__(self) -> None:
        self._faults: dict[str, RuntimeListenerFault] = {}

    def reset(self) -> None:
        self._faults.clear()

    def snapshot(self) -> RuntimeFacilitySnapshot:
        return RuntimeFacilitySnapshot(
            faults=tuple(
                sorted(
                    self._faults.values(),
                    key=lambda item: (item.observed_at_monotonic, item.token),
                )
            )
        )

    def observe(self, fault: RuntimeListenerFault) -> bool:
        current = self._faults.get(fault.token)
        if current is not None:
            return False
        self._faults[fault.token] = fault
        return True

    def due(self, now_monotonic: float) -> tuple[RuntimeListenerFault, ...]:
        now = float(now_monotonic)
        if not math.isfinite(now):
            return ()
        return tuple(
            fault
            for fault in self.snapshot().faults
            if fault.next_probe_at_monotonic <= now
        )

    def wake(
        self,
        *,
        now_monotonic: float,
        runtime_generation: int | None = None,
        core_generation: int | None = None,
    ) -> int:
        now = float(now_monotonic)
        if not math.isfinite(now):
            return 0
        changed = 0
        for token, fault in tuple(self._faults.items()):
            if runtime_generation is not None and fault.runtime_generation != int(
                runtime_generation
            ):
                continue
            if core_generation is not None and fault.core_generation != int(
                core_generation
            ):
                continue
            if fault.next_probe_at_monotonic > now:
                self._faults[token] = replace(
                    fault,
                    next_probe_at_monotonic=now,
                )
                changed += 1
        return changed

    def record_probe(
        self,
        token: str,
        *,
        now_monotonic: float,
        endpoint_ready: bool,
        final_exit_verified: bool = False,
    ) -> bool:
        fault = self._faults.get(str(token or ""))
        if fault is None:
            return False
        clears = bool(endpoint_ready)
        if fault.blocks_data_plane:
            clears = bool(clears and final_exit_verified)
        if clears:
            self._faults.pop(fault.token, None)
            return True
        stage = min(
            len(self._RETRY_DELAYS) - 1,
            max(0, int(fault.retry_stage)) + 1,
        )
        delay = self._RETRY_DELAYS[stage]
        now = float(now_monotonic)
        if not math.isfinite(now):
            now = fault.next_probe_at_monotonic
        self._faults[fault.token] = replace(
            fault,
            retry_stage=stage,
            next_probe_at_monotonic=now + delay,
        )
        return False

    def discard_stale(
        self,
        *,
        runtime_generation: int,
        core_generation: int,
    ) -> int:
        removed = 0
        for token, fault in tuple(self._faults.items()):
            if (
                fault.runtime_generation != int(runtime_generation)
                or fault.core_generation != int(core_generation)
            ):
                self._faults.pop(token, None)
                removed += 1
        return removed


__all__ = [
    "RuntimeFacilityImpact",
    "RuntimeFacilitySnapshot",
    "RuntimeListenerFault",
    "RuntimeListenerKind",
    "RuntimeLocalFacilityState",
    "classify_runtime_listener_fault",
]
