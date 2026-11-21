"""How a Gateway (exit) node's health check reaches its verdict.

A Gateway node carries user traffic through the fallback layers: its Mihomo
proxy has ``dialer-proxy: PROXY-FALLBACK``.  Its scheduled check, however, is a
direct TCP probe from this computer, and many residential/ISP gateways answer
direct connections poorly or refuse some source networks.  A failed direct
probe alone must not condemn such a node: a Gateway left out of the export is
unselectable, and a selected one leaves EXIT-SELECT with only REJECT.

1. Direct first: a passed direct probe is a pass.
2. Direct failed and the fallback layers have usable nodes: probe once more
   through them (the runtime's chain probe, a request-owned Mihomo); that
   verdict stands.
3. Direct failed and no fallback node is usable: unusable with reason
   ``gateway_no_upstream`` (not a plain failure), and the caller rechecks as
   soon as a fallback node becomes usable.

A chain probe starts a Mihomo process, so it runs only after a direct failure
and at most once per node per ``CHAIN_PROBE_INTERVAL_SECONDS``; in between the
node keeps its last chain verdict.  A probe that could not run at all (no
Mihomo binary, startup failure, cancelled) says nothing about the node: the
direct verdict stands.

This module never touches Mihomo or the Manager: the prober is injected.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from gateway_selection import gateway_identity
from log_event_reports import log_raw_diagnostic
from models import Node, NodeStatus, utc_now_iso
from runtime_health_check import health_check_verdict
from tester import apply_success_latency_policy

GATEWAY_NO_UPSTREAM_REASON = "gateway_no_upstream"
GATEWAY_UNREACHABLE_REASON = "gateway_unreachable"
CHAIN_PROBE_INTERVAL_SECONDS = 300.0
CHAIN_UPSTREAM_LIMIT = 8
CHECK_PATH_KEY = "gateway_check_path"
CHAIN_LATENCY_SCOPE = "gateway_chain"

# (node, usable fallback nodes) -> an EphemeralNodeDiagnosticService result:
# {"state": "success" | "failed" | "cancelled", "latency_ms", "reason"}.
ChainProber = Callable[[Node, list[Node]], Mapping[str, Any]]

# Chain results that measured the node: the probe ran and the path failed.
_NODE_FAILURE_REASONS = frozenset({"probe_failed", "timeout"})
# A probe that never got to run for reasons of the moment is not remembered:
# the next check may probe again (a missing binary or a failed start is).
_TRANSIENT_REASONS = frozenset({"cancelled", "diagnostic_in_progress", "service_closed"})
_LATENCY_POLICY_EXTRA_KEYS = (
    "latency_policy_state",
    "latency_policy_limit_ms",
    "latency_policy_observed_ms",
    "latency_policy_scope",
)


@dataclass(frozen=True)
class ChainVerdict:
    """One chain probe's verdict: pass | fail | unavailable (could not run)."""

    probed_at: float
    outcome: str
    latency_ms: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class GatewayCheckOutcome:
    nodes: list[Node]
    waiting_for_upstream: bool = False
    probes_run: int = 0


def direct_check_failed(node: Node) -> bool:
    """A committed direct failure (not a deferred or cancelled probe)."""

    return health_check_verdict(node) == "fail"


def chain_verdict_from_result(result: Any, *, probed_at: float) -> ChainVerdict:
    payload = result if isinstance(result, Mapping) else {}
    state = str(payload.get("state") or "")
    reason = str(payload.get("reason") or "")
    latency = payload.get("latency_ms")
    if (
        state == "success"
        and isinstance(latency, (int, float))
        and not isinstance(latency, bool)
        and latency >= 0
    ):
        return ChainVerdict(probed_at, "pass", float(latency), "ok")
    if state == "failed" and reason in _NODE_FAILURE_REASONS:
        return ChainVerdict(probed_at, "fail", None, reason)
    return ChainVerdict(probed_at, "unavailable", None, reason or state or "unavailable")


def _failed_clone(node: Node, *, status: NodeStatus, reason: str, path: str) -> Node:
    updated = node.clone()
    for key in _LATENCY_POLICY_EXTRA_KEYS:
        updated.extra.pop(key, None)
    updated.status = status
    updated.latency_ms = None
    updated.suspicious_reason = reason
    updated.extra[CHECK_PATH_KEY] = path
    updated.last_checked_at = node.last_checked_at or utc_now_iso()
    return updated


def no_upstream_result(node: Node) -> Node:
    """Direct failed and nothing to chain through: unusable, not proven dead."""

    return _failed_clone(
        node,
        status=NodeStatus.SUSPICIOUS,
        reason=GATEWAY_NO_UPSTREAM_REASON,
        path="no_upstream",
    )


def apply_chain_verdict(
    node: Node,
    verdict: ChainVerdict,
    testing_config: Mapping[str, Any] | None,
) -> Node:
    """The node's verdict after a direct failure and this chain verdict."""

    if verdict.outcome == "pass" and verdict.latency_ms is not None:
        updated = apply_success_latency_policy(
            node,
            verdict.latency_ms,
            dict(testing_config or {}),
            CHAIN_LATENCY_SCOPE,
        )
        updated.extra[CHECK_PATH_KEY] = "chain"
        updated.last_checked_at = node.last_checked_at or utc_now_iso()
        return updated
    if verdict.outcome == "fail":
        return _failed_clone(
            node,
            status=NodeStatus.DEAD,
            reason=GATEWAY_UNREACHABLE_REASON,
            path="chain",
        )
    return node


def resolve_gateway_check(
    tested: Sequence[Node],
    *,
    prober: ChainProber | None,
    upstream: Callable[[], list[Node]],
    verdicts: dict[str, ChainVerdict],
    testing_config: Mapping[str, Any] | None,
    interval_seconds: float = CHAIN_PROBE_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
    logger: logging.Logger | None = None,
) -> GatewayCheckOutcome:
    """Apply steps 2 and 3 to the direct results in ``tested``.

    ``verdicts`` is the caller's per-node memo (keyed by the Gateway identity,
    which covers the credentials) and is updated in place.  Without a prober
    the direct results are returned unchanged.  ``upstream`` is read once, and
    only when some node failed directly.
    """

    nodes = list(tested)
    failed = [index for index, node in enumerate(nodes) if direct_check_failed(node)]
    if prober is None or not failed:
        return GatewayCheckOutcome(nodes)
    now = monotonic()
    for key, verdict in list(verdicts.items()):
        if now - verdict.probed_at >= interval_seconds:
            verdicts.pop(key, None)
    upstream_nodes = list(upstream() or [])
    if not upstream_nodes:
        for index in failed:
            nodes[index] = no_upstream_result(nodes[index])
        return GatewayCheckOutcome(nodes, waiting_for_upstream=True)
    probes = 0
    counts = {"pass": 0, "fail": 0, "unavailable": 0}
    for index in failed:
        node = nodes[index]
        key = gateway_identity(node)
        verdict = verdicts.get(key)
        if verdict is None:
            if cancelled():
                continue
            try:
                result = prober(node.clone(), [item.clone() for item in upstream_nodes])
            except Exception as exc:
                result = {"state": "failed", "reason": f"prober_error:{type(exc).__name__}"}
            verdict = chain_verdict_from_result(result, probed_at=monotonic())
            if verdict.reason not in _TRANSIENT_REASONS:
                verdicts[key] = verdict
            probes += 1
            counts[verdict.outcome] += 1
        nodes[index] = apply_chain_verdict(node, verdict, testing_config)
    if probes and logger is not None:
        log_raw_diagnostic(
            logger,
            "info",
            "Gateway direct check failed; checked through the fallback layers "
            "(probes=%s pass=%s fail=%s unavailable=%s upstream=%s)",
            probes,
            counts["pass"],
            counts["fail"],
            counts["unavailable"],
            len(upstream_nodes),
        )
    return GatewayCheckOutcome(nodes, probes_run=probes)
