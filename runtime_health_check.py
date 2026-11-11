"""Identity-bound authority for one scheduled or requested node health check."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckContext:
    check_run_id: str
    origin: str
    tier: str
    pool_kind: str
    identity: str
    proxy_name: str
    runtime_generation: int
    node_config_digest: str
    started_monotonic: float
    sequence: int = 0
    health_signature: tuple[Any, ...] = ()
    incident: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def node_key(self) -> tuple[str, str, str, str]:
        return self.tier, self.pool_kind, self.identity, self.proxy_name

    def to_request(self) -> dict[str, Any]:
        # A bound check answers the incident, which names the Core leaf it was
        # recorded under (a stable Tier3 name or a "-2" suffix can differ from
        # node.name).  self.proxy_name and node_key stay the Manager node's.
        incident_proxy_name = (
            str(self.incident.get("proxy_name") or "")
            if self.incident.get("incident_id")
            else ""
        )
        return {
            **self.incident,
            "check_run_id": self.check_run_id,
            "check_origin": self.origin,
            "tier": self.tier,
            "pool_kind": self.pool_kind,
            "identity": self.identity,
            "proxy_name": incident_proxy_name or self.proxy_name,
            "runtime_generation": self.runtime_generation,
            "node_config_digest": self.node_config_digest,
            "recheck_started_monotonic": self.started_monotonic,
            "authoritative": True,
        }


def health_check_verdict(node: Any) -> str:
    """Keep cancellation/capacity evidence separate from an actual node FAIL."""
    extra = getattr(node, "extra", {}) or {}
    stage = str(extra.get("tier3_failure_stage") or "").lower()
    outcome = str(extra.get("tier3a_health_outcome") or "").lower()
    reason = str(getattr(node, "suspicious_reason", "") or "").lower()
    if (
        extra.get("tier3a_health_pending_recheck")
        or reason.startswith(("tcp_deferred:", "validation_deferred:", "tcp_cancelled:"))
        or stage in {"tcp_deferred", "tcp_cancelled", "validation_deferred", "validation_cancelled"}
        or outcome in {"censored", "cancelled", "deferred"}
    ):
        return "pending"
    status = str(getattr(getattr(node, "status", None), "value", ""))
    return "pass" if status in {"alive", "slow"} else "fail"
