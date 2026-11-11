from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import Node, NodeStatus
from tier3_refresh_controller import Tier3ControllerDecision, Tier3ValidationBudget
from tier3_sources import CollectionReport
from tier3_transport_contract import tcp_reachability_is_required
from tier3_validation_feedback import Tier3CandidateDispatchContext


@dataclass(frozen=True)
class Tier3SourceCollectionResult:
    nodes: list[Node]
    report: CollectionReport


@dataclass(frozen=True)
class Tier3RefreshInputs:
    previous_nodes: list[Node]
    sources_payload: dict[str, Any]
    settings: dict[str, Any]
    tier3_config: dict[str, Any]
    decision: Tier3ControllerDecision
    target_alive: int
    intensive_expansion: bool
    validation_budget: Tier3ValidationBudget | None = None


@dataclass(frozen=True)
class Tier3PartialRefreshOutcome:
    nodes: list[Node]
    alive_count: int
    buffered: bool


@dataclass(frozen=True)
class Tier3EmptyRefreshOutcome:
    nodes: list[Node]
    alive_count: int
    collection_report: dict[str, Any]


@dataclass(frozen=True)
class Tier3RefreshValidationOutcome:
    candidates: list[Node]
    nodes: list[Node]
    alive_count: int
    validation_report: dict[str, Any]
    observed_nodes: list[Node]


@dataclass(frozen=True)
class Tier3StreamedTcpObservation:
    """Freeze the TCP verdict before validation can mutate the shared node."""

    node: Node
    dispatch_context: Tier3CandidateDispatchContext | None = None
    responsive: bool = field(init=False)
    validation_required: bool = field(init=False)
    failure_stage: str = field(init=False)
    failure_reason: str = field(init=False)
    prestart_reclaimed: bool = field(init=False)

    def __post_init__(self) -> None:
        responsive = self.node.status in {NodeStatus.ALIVE, NodeStatus.SLOW}
        object.__setattr__(self, "responsive", responsive)
        object.__setattr__(self, "validation_required", bool(
            responsive or (
                self.node.status is NodeStatus.DEAD
                and not tcp_reachability_is_required(self.node)
                and self.node.extra.get("tier3_failure_stage")
                not in {"tcp_deferred", "tcp_cancelled"}
            )
        ))
        object.__setattr__(self, "failure_stage", (
            "" if responsive else str(self.node.extra.get("tier3_failure_stage") or "")
        ))
        object.__setattr__(self, "failure_reason", (
            "" if responsive else str(self.node.suspicious_reason or "").lower()
        ))
        object.__setattr__(self, "prestart_reclaimed", bool(
            self.node.extra.get("tier3_tcp_prestart_reclaimed")
        ))


@dataclass(frozen=True)
class Tier3StreamedBatchOutcome:
    should_stop_collection: bool
    candidates: list[Node]
    nodes: list[Node]
    alive_count: int
    validation_report: dict[str, Any]
    observed_nodes: list[Node]
    observed_items: tuple[Tier3StreamedTcpObservation, ...] = ()


@dataclass(frozen=True)
class Tier3RefreshRetentionOutcome:
    nodes: list[Node]
    alive_count: int
    collection_report: dict[str, Any]


@dataclass(frozen=True)
class Tier3RefreshCommitOutcome:
    nodes: list[Node]
    alive_count: int
    committed: bool
    pool_commit_elapsed_ms: int = 0
