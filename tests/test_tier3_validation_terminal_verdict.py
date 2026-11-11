"""A process-domain candidate that never answers must end in a real verdict."""

from __future__ import annotations

import json

import tier_cache
from models import NodeStatus
from tier3_validation_domain import node_from_terminal
from tier3_validation_protocol import (
    ProbeAttemptFact,
    Tier3TerminalKind,
    ValidationStage,
    ValidationTerminal,
)

CACHEABLE_PREFIXES = (
    "mihomo_validation_failed:",
    "mihomo_batch_validation_failed:",
    "all_gateways_unavailable:",
)


def _fact(endpoint: str, *, outcome: str, status_code: int | None, exhausted: bool, stage: str) -> ProbeAttemptFact:
    return ProbeAttemptFact(
        endpoint=endpoint,
        elapsed_ms=3000 if stage == ValidationStage.FAST_PROBE.value else 5000,
        full_budget=True,
        completed=outcome != "right_censored",
        entitlement_exhausted=exhausted,
        status_code=status_code,
        outcome=outcome,
        stage=stage,
    )


def _terminal(kind: Tier3TerminalKind, reason: str, facts: tuple[ProbeAttemptFact, ...]) -> ValidationTerminal:
    node_json = json.dumps({
        "name": "candidate",
        "protocol": "vmess",
        "address": "203.0.113.10",
        "port": 443,
        "tier": "tier3",
    })
    return ValidationTerminal(
        refresh_epoch="epoch-1",
        candidate_digest="digest-1",
        config_digest="config-1",
        lease_id="lease-1",
        sequence=1,
        worker_id=3,
        worker_generation=1,
        kind=kind,
        node_json=node_json,
        reason=reason,
        stage=ValidationStage.CANONICAL_DEEP,
        attempt_facts=facts,
        total_elapsed_ms=18000,
        clean_confirmed=True,
    )


def _ledger(*, canonical_exhausted: bool = True) -> tuple[ProbeAttemptFact, ...]:
    fast = _fact("https://a.example/generate_204", outcome="right_censored", status_code=None, exhausted=True, stage=ValidationStage.FAST_PROBE.value)
    canon = tuple(
        _fact(url, outcome="right_censored", status_code=None, exhausted=canonical_exhausted, stage=ValidationStage.CANONICAL_DEEP.value)
        for url in ("https://a.example/generate_204", "http://b.example/generate_204", "http://c.example/generate_204")
    )
    return (fast,) + canon


def test_whole_ledger_without_a_response_is_a_session_failure_not_a_deferral():
    node = node_from_terminal(_terminal(Tier3TerminalKind.CENSORED, "endpoint_ledger_transient_or_censored", _ledger()))

    assert node.status is NodeStatus.SUSPICIOUS
    reason = str(node.suspicious_reason)
    assert reason.startswith(CACHEABLE_PREFIXES), reason
    assert not reason.startswith("validation_deferred:")
    assert node.extra["tier3_failure_stage"] == "full_validation_probe_deadline"
    assert node.extra["tier3_probe_entitlement_exhausted"] is True
    assert node.extra["tier3_full_validation_deadline_hit"] is True
    # The stage stays non-persistent on purpose: the candidate is reported as a
    # real failure for this session's accounting, but no durable negative record
    # is written and no deterministic-failure time is stamped, so cache revival
    # of a historically passing node is never suppressed by a probe deadline.
    assert node.extra["tier3_failure_stage"] in tier_cache.NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
    lowered = reason.lower()
    assert "http error 503" not in lowered and "http error 504" not in lowered


def test_deep_retention_overflow_and_wave_cuts_stay_deferred():
    for reason in ("deep_retention_window_full", "deep_retention_fair_replaced"):
        node = node_from_terminal(_terminal(Tier3TerminalKind.CENSORED, reason, _ledger()))
        assert str(node.suspicious_reason).startswith("validation_deferred:"), reason
        assert node.extra["tier3_failure_stage"] == "validation_deferred"
        assert "tier3_probe_entitlement_exhausted" not in node.extra


def test_any_http_response_in_the_ledger_keeps_the_candidate_deferred():
    facts = _ledger()
    answered = facts[:2] + (
        _fact("http://b.example/generate_204", outcome="transient", status_code=503, exhausted=False, stage=ValidationStage.CANONICAL_DEEP.value),
    ) + facts[3:]
    node = node_from_terminal(_terminal(Tier3TerminalKind.CENSORED, "endpoint_ledger_transient_or_censored", answered))
    assert str(node.suspicious_reason).startswith("validation_deferred:")
    assert node.extra["tier3_failure_stage"] == "validation_deferred"


def test_ledger_without_exhausted_entitlement_stays_deferred():
    facts = tuple(
        ProbeAttemptFact(
            endpoint=f.endpoint, elapsed_ms=f.elapsed_ms, full_budget=False, completed=False,
            entitlement_exhausted=False, status_code=None, outcome="right_censored", stage=f.stage,
        )
        for f in _ledger()
    )
    node = node_from_terminal(_terminal(Tier3TerminalKind.CENSORED, "endpoint_ledger_transient_or_censored", facts))
    assert str(node.suspicious_reason).startswith("validation_deferred:")


def test_candidate_fail_and_cancelled_are_unchanged():
    failed = node_from_terminal(_terminal(Tier3TerminalKind.CANDIDATE_FAIL, "endpoint_ledger_candidate_failed", _ledger()))
    assert failed.extra["tier3_failure_stage"] == "full_validation_candidate_failed"
    cancelled = node_from_terminal(_terminal(Tier3TerminalKind.CANCELLED, "endpoint_attempt_cancelled", ()))
    assert cancelled.extra["tier3_failure_stage"] == "validation_cancelled"
