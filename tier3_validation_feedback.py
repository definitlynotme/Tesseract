from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from tier3_candidate_scheduler import Tier3CandidateFeedbackTicket
from tier3_validation_protocol import ValidationStage


class Tier3ValidationFeedbackKind(str, Enum):
    FAST_OBSERVATION = "fast_observation"
    ATTEMPT = "attempt"
    CONTINUATION = "continuation"
    TERMINAL = "terminal"
    ADMISSION = "admission"


@dataclass(frozen=True, slots=True)
class Tier3CandidateDispatchContext:
    """Immutable attribution carried after the TCP capacity lease is released."""

    ticket: Tier3CandidateFeedbackTicket
    node_identity: str
    selected_monotonic_ns: int = field(default_factory=time.monotonic_ns)


@dataclass(frozen=True, slots=True)
class Tier3ValidationFeedbackEvent:
    """Immutable process-domain feedback routed to all diagnostic consumers."""

    kind: Tier3ValidationFeedbackKind
    refresh_epoch: str
    candidate_digest: str
    worker_generation: int
    stage: ValidationStage
    outcome: str
    busy_worker_ms: int
    facility_elapsed_ms: int
    terminal: bool
    passed: bool
    ticket: Tier3CandidateFeedbackTicket | None = None
    facility_failure: bool = False
    right_censored: bool = False
    net_new_pool_growth: bool = False
    monotonic_ns: int = field(default_factory=time.monotonic_ns)

    @property
    def source_root(self) -> str:
        return self.ticket.source_root if self.ticket is not None else "unknown"

    @property
    def source_family(self) -> str:
        return self.ticket.family if self.ticket is not None else "other"

    @property
    def source_coarse(self) -> str:
        return self.ticket.coarse_key if self.ticket is not None else "unknown"
