"""Whether one layer round checked through the Gateway says anything about each node.

While a Gateway is the exit, Vanguard and Sentinel nodes are checked as chains:
this computer -> the node -> the selected Gateway -> a probe URL
(``tester._gateway_chain_validate``; a failure reads ``all_gateways_unavailable:``).
Every chain in a round shares the Gateway hop and this computer's link.  When
every one of them fails at once, the round has measured that shared part, not
each node: a phone hotspot that stalls, a Gateway that stops answering for a
moment, or a computer too busy (or just back from sleep) to start the helper
Mihomo processes in time all fail every chain together.  Committed as it is,
such a round marks every node unusable, takes the layer offline and raises an
emergency alert, which the next round clears again seconds later.

1. A round in which some chain check passed is committed as it is: the shared
   part worked, so each failure is that node's own.
2. The first round in which every chain check failed is held: the nodes that
   were usable keep their last verdict (nothing is committed for them), and the
   Manager checks the layer again on its next urgent cycle.
3. A held layer's next round is committed whatever it finds, so a real outage
   (dead nodes, a dead Gateway, no network) still takes the layer down, one
   round later.

A node whose helper Mihomo was still running but never listened was not
tested at all (``tester.GATEWAY_CHAIN_HELPER_NOT_READY_REASON``, a pending
result): it is left out of the commit like a held node, counts towards none of
the steps above, and a round of nothing else neither confirms nor clears a
hold.  A helper that exited before listening is an ordinary chain failure.

How this meets the Gateway's own health (``gateway_check.py``): the Gateway is
judged by a direct probe and, after a direct failure, by a chain probe through
usable fallback nodes, which is the same hop a held round just failed.  A held
layer stays usable, so the Gateway keeps its upstream instead of turning
``gateway_no_upstream`` over one round, and while a layer is held the Manager
keeps the Gateway's remembered chain verdicts rather than probe the doubtful
hop again and remember a failure against the Gateway for
``CHAIN_PROBE_INTERVAL_SECONDS`` (a Gateway with no remembered verdict is
still probed).  Neither verdict feeds the other: a held round never reads or
writes the Gateway's verdicts, and a Gateway verdict never holds or confirms
a layer.

This module never touches Mihomo or the Manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Hashable, Sequence

from models import Node, NodeStatus
from runtime_health_check import health_check_verdict
from tester import GATEWAY_CHAIN_HELPER_NOT_READY_REASON

# tester._gateway_chain_validate: the chain ran through every selected Gateway
# and failed (probe timeout or error, a helper that exited before listening).
CHAIN_FAILURE_PREFIX = "all_gateways_unavailable:"
_USABLE_STATUSES = frozenset({NodeStatus.ALIVE, NodeStatus.SLOW})


@dataclass(frozen=True)
class ChainRound:
    """One judged round.  ``commit`` goes to the Manager's commit path;
    ``held`` and ``not_ready`` results are not committed, so their nodes keep
    their last verdict.  ``decided``: some result passed or failed."""

    commit: list[Node]
    held: list[Node] = field(default_factory=list)
    not_ready: list[Node] = field(default_factory=list)
    every_chain_failed: bool = False
    decided: bool = False


def chain_check_failed(node: Node) -> bool:
    """A committed failure of the node's chain through the Gateway."""

    return health_check_verdict(node) == "fail" and str(
        node.suspicious_reason or ""
    ).startswith(CHAIN_FAILURE_PREFIX)


def chain_helper_not_ready(node: Node) -> bool:
    """The node's chain helper was still starting: the chain was not tested."""

    return str(node.suspicious_reason or "").startswith(GATEWAY_CHAIN_HELPER_NOT_READY_REASON)


def every_chain_failed(tested: Sequence[Node]) -> bool:
    """Every decided result is a chain failure (pending results decide nothing)."""

    decided = [node for node in tested if health_check_verdict(node) != "pending"]
    return bool(decided) and all(chain_check_failed(node) for node in decided)


def judge_chain_round(
    previous: Sequence[Node],
    tested: Sequence[Node],
    *,
    held_before: bool,
    key: Callable[[Node], Hashable],
) -> ChainRound:
    """Apply steps 1-3 to one round of a layer checked through the Gateway.

    ``previous`` is the layer as the round found it, ``tested`` the round's
    results, matched by ``key``.  ``held_before`` says the layer's previous
    round was held, so this one confirms it.
    """

    not_ready = [node for node in tested if chain_helper_not_ready(node)]
    nodes = [node for node in tested if not chain_helper_not_ready(node)]
    decided = any(health_check_verdict(node) != "pending" for node in nodes)
    if not every_chain_failed(nodes):
        return ChainRound(commit=nodes, not_ready=not_ready, decided=decided)
    if held_before:
        return ChainRound(commit=nodes, not_ready=not_ready, every_chain_failed=True, decided=True)
    usable_before = {key(node) for node in previous if node.status in _USABLE_STATUSES}
    commit: list[Node] = []
    held: list[Node] = []
    for node in nodes:
        if health_check_verdict(node) == "fail" and key(node) in usable_before:
            held.append(node)
        else:
            # Pending results commit nothing anyway, and a node that was
            # already unusable loses nothing by a fresh failure.
            commit.append(node)
    return ChainRound(
        commit=commit,
        held=held,
        not_ready=not_ready,
        every_chain_failed=True,
        decided=True,
    )
