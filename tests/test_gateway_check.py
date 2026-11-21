"""gateway_check: direct first, then through the fallback layers, else wait."""

from __future__ import annotations

from typing import Any

from gateway_check import (
    CHAIN_PROBE_INTERVAL_SECONDS,
    CHECK_PATH_KEY,
    GATEWAY_NO_UPSTREAM_REASON,
    GATEWAY_UNREACHABLE_REASON,
    ChainVerdict,
    resolve_gateway_check,
)
from gateway_selection import gateway_identity
from models import Node, NodeStatus
from node_status_view import _runtime_failure_display_category

TESTING = {"primary_gateway_max_latency_ms": 1900}


def _gateway(name: str = "isp", *, status: NodeStatus = NodeStatus.DEAD, reason: str | None = "timed out") -> Node:
    return Node(
        name=name,
        protocol="socks5",
        address=f"{name}.example.net",
        port=1080,
        tier="exit",
        password="isp-pass",
        extra={"username": "isp-user", "exit_node": True},
        status=status,
        latency_ms=None if status not in {NodeStatus.ALIVE, NodeStatus.SLOW} else 90.0,
        suspicious_reason=reason,
        last_checked_at="2026-11-21T09:30:00+00:00",
    )


def _sentinel() -> Node:
    return Node(
        name="sentinel",
        protocol="ss",
        address="sentinel.example",
        port=8388,
        tier="tier2",
        method="aes-256-gcm",
        password="sentinel-password",
        status=NodeStatus.ALIVE,
        latency_ms=120.0,
    )


class _Prober:
    def __init__(self, *results: dict[str, Any] | Exception) -> None:
        self.results = list(results) or [{"state": "success", "latency_ms": 420.0, "reason": "ok"}]
        self.calls: list[tuple[Node, list[Node]]] = []

    def __call__(self, node: Node, upstream: list[Node]) -> dict[str, Any]:
        self.calls.append((node, upstream))
        result = self.results[min(len(self.calls), len(self.results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _resolve(tested, *, prober, upstream=None, verdicts=None, clock=None, cancelled=lambda: False):
    reads: list[int] = []

    def read_upstream() -> list[Node]:
        reads.append(1)
        return list(upstream or [])

    outcome = resolve_gateway_check(
        tested,
        prober=prober,
        upstream=read_upstream,
        verdicts={} if verdicts is None else verdicts,
        testing_config=TESTING,
        monotonic=clock or _Clock(),
        cancelled=cancelled,
    )
    return outcome, reads


def test_direct_pass_stands_without_chain_probe_or_upstream_read() -> None:
    prober = _Prober()
    alive = _gateway(status=NodeStatus.ALIVE, reason=None)

    outcome, reads = _resolve([alive], prober=prober, upstream=[_sentinel()])

    assert outcome.nodes == [alive]
    assert prober.calls == [] and reads == []
    assert outcome.waiting_for_upstream is False


def test_direct_failure_with_usable_upstream_passes_through_the_chain() -> None:
    prober = _Prober({"state": "success", "latency_ms": 420.0, "reason": "ok"})
    sentinel = _sentinel()

    outcome, _reads = _resolve([_gateway()], prober=prober, upstream=[sentinel])

    (node,) = outcome.nodes
    assert node.status == NodeStatus.ALIVE
    assert node.latency_ms == 420.0
    assert node.suspicious_reason is None
    assert node.extra[CHECK_PATH_KEY] == "chain"
    assert outcome.probes_run == 1 and outcome.waiting_for_upstream is False
    probed, upstream = prober.calls[0]
    assert probed.address == "isp.example.net"
    assert [item.name for item in upstream] == ["sentinel"]


def test_chain_latency_still_follows_the_gateway_latency_limit() -> None:
    prober = _Prober({"state": "success", "latency_ms": 2400.0, "reason": "ok"})

    outcome, _reads = _resolve([_gateway()], prober=prober, upstream=[_sentinel()])

    (node,) = outcome.nodes
    assert node.status == NodeStatus.DEAD
    assert node.suspicious_reason == "latency_limit_exceeded"
    assert node.extra["latency_policy_scope"] == "gateway_chain"


def test_direct_and_chain_failure_is_gateway_unreachable() -> None:
    prober = _Prober({"state": "failed", "latency_ms": None, "reason": "timeout"})

    outcome, _reads = _resolve([_gateway()], prober=prober, upstream=[_sentinel()])

    (node,) = outcome.nodes
    assert node.status == NodeStatus.DEAD
    assert node.latency_ms is None
    assert node.suspicious_reason == GATEWAY_UNREACHABLE_REASON
    assert _runtime_failure_display_category(node.suspicious_reason)[0] == "gateway_unreachable"


def test_direct_failure_without_upstream_waits_instead_of_failing_plainly() -> None:
    prober = _Prober()

    outcome, reads = _resolve([_gateway()], prober=prober, upstream=[])

    (node,) = outcome.nodes
    assert prober.calls == [] and reads == [1]
    assert node.status == NodeStatus.SUSPICIOUS
    assert node.latency_ms is None
    assert node.suspicious_reason == GATEWAY_NO_UPSTREAM_REASON
    assert node.extra[CHECK_PATH_KEY] == "no_upstream"
    assert outcome.waiting_for_upstream is True
    assert _runtime_failure_display_category(node.suspicious_reason)[0] == "gateway_no_upstream"


def test_chain_probe_runs_at_most_once_per_node_per_interval() -> None:
    clock = _Clock()
    verdicts: dict[str, ChainVerdict] = {}
    prober = _Prober(
        {"state": "success", "latency_ms": 420.0, "reason": "ok"},
        {"state": "failed", "latency_ms": None, "reason": "timeout"},
    )
    upstream = [_sentinel()]

    first, _ = _resolve([_gateway()], prober=prober, upstream=upstream, verdicts=verdicts, clock=clock)
    clock.now += CHAIN_PROBE_INTERVAL_SECONDS - 1
    second, _ = _resolve([_gateway()], prober=prober, upstream=upstream, verdicts=verdicts, clock=clock)

    assert len(prober.calls) == 1
    assert second.probes_run == 0
    # Between probes the node keeps its last chain verdict.
    assert first.nodes[0].status == second.nodes[0].status == NodeStatus.ALIVE
    assert second.nodes[0].latency_ms == 420.0

    clock.now += 1
    third, _ = _resolve([_gateway()], prober=prober, upstream=upstream, verdicts=verdicts, clock=clock)

    assert len(prober.calls) == 2
    assert third.nodes[0].suspicious_reason == GATEWAY_UNREACHABLE_REASON


def test_rate_limit_is_per_node_identity() -> None:
    verdicts: dict[str, ChainVerdict] = {}
    prober = _Prober()
    upstream = [_sentinel()]

    _resolve([_gateway("a")], prober=prober, upstream=upstream, verdicts=verdicts)
    _resolve([_gateway("a"), _gateway("b")], prober=prober, upstream=upstream, verdicts=verdicts)

    assert [node.name for node, _upstream in prober.calls] == ["a", "b"]
    assert set(verdicts) == {gateway_identity(_gateway("a")), gateway_identity(_gateway("b"))}


def test_a_probe_that_could_not_run_keeps_the_direct_verdict_and_is_rate_limited() -> None:
    verdicts: dict[str, ChainVerdict] = {}
    prober = _Prober({"state": "failed", "latency_ms": None, "reason": "component_missing"})
    direct = _gateway()

    outcome, _ = _resolve([direct], prober=prober, upstream=[_sentinel()], verdicts=verdicts)
    again, _ = _resolve([direct], prober=prober, upstream=[_sentinel()], verdicts=verdicts)

    assert outcome.nodes == [direct] and again.nodes == [direct]
    assert len(prober.calls) == 1
    assert next(iter(verdicts.values())).outcome == "unavailable"


def test_a_cancelled_or_busy_probe_is_not_remembered() -> None:
    verdicts: dict[str, ChainVerdict] = {}
    prober = _Prober(
        {"state": "cancelled", "latency_ms": None, "reason": "cancelled"},
        {"state": "failed", "latency_ms": None, "reason": "diagnostic_in_progress"},
        {"state": "success", "latency_ms": 420.0, "reason": "ok"},
    )
    direct = _gateway()

    first, _ = _resolve([direct], prober=prober, upstream=[_sentinel()], verdicts=verdicts)
    second, _ = _resolve([direct], prober=prober, upstream=[_sentinel()], verdicts=verdicts)
    third, _ = _resolve([direct], prober=prober, upstream=[_sentinel()], verdicts=verdicts)

    assert first.nodes == [direct] and second.nodes == [direct]
    assert third.nodes[0].status == NodeStatus.ALIVE
    assert len(prober.calls) == 3


def test_a_raising_prober_is_a_probe_that_could_not_run() -> None:
    prober = _Prober(RuntimeError("mihomo exploded"))
    direct = _gateway()

    outcome, _ = _resolve([direct], prober=prober, upstream=[_sentinel()])

    assert outcome.nodes == [direct]
    assert outcome.probes_run == 1


def test_without_a_prober_the_direct_verdict_stands() -> None:
    direct = _gateway()

    outcome, reads = _resolve([direct], prober=None, upstream=[])

    assert outcome.nodes == [direct] and reads == []
    assert outcome.waiting_for_upstream is False


def test_a_deferred_direct_probe_is_not_chain_probed() -> None:
    prober = _Prober()
    deferred = _gateway(status=NodeStatus.SUSPICIOUS, reason="tcp_cancelled:tcp_wave_cancelled")

    outcome, reads = _resolve([deferred], prober=prober, upstream=[_sentinel()])

    assert outcome.nodes == [deferred]
    assert prober.calls == [] and reads == []


def test_no_new_chain_probe_starts_once_stop_is_requested() -> None:
    prober = _Prober()
    direct = _gateway()

    outcome, _ = _resolve([direct], prober=prober, upstream=[_sentinel()], cancelled=lambda: True)

    assert prober.calls == []
    assert outcome.nodes == [direct]
