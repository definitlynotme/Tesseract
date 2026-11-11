from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NodeStatus(str, Enum):
    UNKNOWN = "unknown"
    ALIVE = "alive"
    DEAD = "dead"
    SLOW = "slow"
    SUSPICIOUS = "suspicious"


def node_status_from_value(value: Any) -> NodeStatus:
    try:
        return NodeStatus(value)
    except ValueError:
        return NodeStatus.UNKNOWN


class AlertType(str, Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"
    RECOVERY = "RECOVERY"
    POOL_UPDATE = "POOL_UPDATE"
    BACKUP_REMINDER = "BACKUP_REMINDER"
    KILL_SWITCH_ON = "KILL_SWITCH_ON"
    KILL_SWITCH_OFF = "KILL_SWITCH_OFF"
    KILL_SWITCH_DISABLED = "KILL_SWITCH_DISABLED"
    KILL_SWITCH_OVERRIDE = "KILL_SWITCH_OVERRIDE"


@dataclass
class AlertRecord:
    timestamp: str
    alert_type: str
    title: str
    message: str
    event_key: str = ""
    event_group: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "type": self.alert_type,
            "title": self.title,
            "message": self.message,
            "event_key": self.event_key,
            "event_group": self.event_group,
        }


@dataclass
class Node:
    name: str = ""
    protocol: str = ""
    uri: str = ""
    address: str = ""
    port: int = 0
    source: str = ""
    tier: str = ""
    encryption: Optional[str] = None
    security: Optional[str] = None
    network: Optional[str] = None
    host: Optional[str] = None
    path: Optional[str] = None
    sni: Optional[str] = None
    uuid: Optional[str] = None
    password: Optional[str] = None
    method: Optional[str] = None
    plugin: Optional[str] = None
    remarks: Optional[str] = None
    tls: Optional[bool] = None
    status: NodeStatus = NodeStatus.UNKNOWN
    latency_ms: Optional[float] = None
    last_checked_at: Optional[str] = None
    suspicious_reason: Optional[str] = None
    sources: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> tuple[str, ...]:
        auth = self.uuid or self.password or self.method or ""
        transport = "|".join(
            str(value or "")
            for value in [
                self.security,
                self.network,
                self.host,
                self.path,
                self.sni,
                self.plugin,
            ]
        )
        return (
            str(self.protocol or "").lower(),
            str(self.address or "").lower(),
            str(int(self.port)),
            str(auth),
            transport,
        )

    def clone(self) -> "Node":
        return Node.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "uri": self.uri,
            "address": self.address,
            "port": self.port,
            "source": self.source,
            "tier": self.tier,
            "encryption": self.encryption,
            "security": self.security,
            "network": self.network,
            "host": self.host,
            "path": self.path,
            "sni": self.sni,
            "uuid": self.uuid,
            "password": self.password,
            "method": self.method,
            "plugin": self.plugin,
            "remarks": self.remarks,
            "tls": self.tls,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "last_checked_at": self.last_checked_at,
            "suspicious_reason": self.suspicious_reason,
            "sources": list(self.sources),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Node":
        return cls(
            name=payload.get("name", ""),
            protocol=payload.get("protocol", ""),
            uri=payload.get("uri", ""),
            address=payload.get("address", ""),
            port=int(payload.get("port", 0)),
            source=payload.get("source", ""),
            tier=payload.get("tier", ""),
            encryption=payload.get("encryption"),
            security=payload.get("security"),
            network=payload.get("network"),
            host=payload.get("host"),
            path=payload.get("path"),
            sni=payload.get("sni"),
            uuid=payload.get("uuid"),
            password=payload.get("password"),
            method=payload.get("method"),
            plugin=payload.get("plugin"),
            remarks=payload.get("remarks"),
            tls=payload.get("tls"),
            status=node_status_from_value(payload.get("status", NodeStatus.UNKNOWN.value)),
            latency_ms=payload.get("latency_ms"),
            last_checked_at=payload.get("last_checked_at"),
            suspicious_reason=payload.get("suspicious_reason"),
            sources=list(payload.get("sources", [])),
            extra=dict(payload.get("extra", {})),
        )


@dataclass
class TestResult:
    node_key: tuple[str, int]
    status: NodeStatus
    latency_ms: Optional[float]
    checked_at: str
    error: Optional[str] = None
    suspicious_reason: Optional[str] = None


@dataclass
class TierState:
    tier_id: str
    name: str
    security_level: str
    check_interval: int
    nodes: list[Node] = field(default_factory=list)
    alive_count: int = 0
    total_count: int = 0
    last_checked_at: Optional[str] = None
    last_refresh_at: Optional[str] = None


@dataclass
class PoolSnapshot:
    generated_at: str
    nodes: list[Node]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "metadata": dict(self.metadata),
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PoolSnapshot":
        return cls(
            generated_at=payload.get("generated_at", utc_now_iso()),
            metadata=dict(payload.get("metadata", {})),
            nodes=[Node.from_dict(item) for item in payload.get("nodes", [])],
        )


def deduplicate_nodes(
    nodes: Iterable[Node],
    *,
    cancel_event: Any | None = None,
) -> list[Node]:
    deduped: dict[tuple[str, ...], Node] = {}
    for index, node in enumerate(nodes):
        if index % 128 == 0:
            is_set = getattr(cancel_event, "is_set", None)
            if callable(is_set) and is_set():
                return []
        key = node.dedupe_key
        if key not in deduped:
            if node.source and node.source not in node.sources:
                node.sources.append(node.source)
            deduped[key] = node
            continue
        existing = deduped[key]
        for source in [node.source, *node.sources]:
            if source and source not in existing.sources:
                existing.sources.append(source)
        if not existing.remarks and node.remarks:
            existing.remarks = node.remarks
        if not existing.uri and node.uri:
            existing.uri = node.uri
    is_set = getattr(cancel_event, "is_set", None)
    if callable(is_set) and is_set():
        return []
    return list(deduped.values())


def count_healthy_nodes(nodes: Iterable[Node]) -> int:
    return sum(1 for node in nodes if node.status in {NodeStatus.ALIVE, NodeStatus.SLOW})


def sort_nodes_for_use(nodes: Iterable[Node]) -> list[Node]:
    order = {
        NodeStatus.ALIVE: 0,
        NodeStatus.SLOW: 1,
        NodeStatus.UNKNOWN: 2,
        NodeStatus.SUSPICIOUS: 3,
        NodeStatus.DEAD: 4,
    }
    return sorted(
        nodes,
        key=lambda item: (
            order.get(item.status, 5),
            item.latency_ms if item.latency_ms is not None else float("inf"),
            item.name or item.address,
        ),
    )
