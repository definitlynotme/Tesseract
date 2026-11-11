from __future__ import annotations

from dataclasses import dataclass

from models import NodeStatus
from tier3_validation_protocol import Tier3TerminalKind


@dataclass(frozen=True, slots=True)
class Tier3AHealthCheckResult:
    identity: str
    config_digest: str
    outcome: Tier3TerminalKind
    status: NodeStatus
    checked_at: str
    failure_class: str = ""
    latency_ms: float | None = None
    pending_recheck: bool = False

