from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from tier3_validation_protocol import (
    ProbeAttemptFact,
    ValidationStage,
    ValidationTerminal,
)


def _wilson_lower(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts <= 0:
        return 0.0
    n = float(attempts)
    p = max(0.0, min(1.0, float(successes) / n))
    denominator = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    radius = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
    return max(0.0, (center - radius) / denominator)


@dataclass(slots=True)
class _EndpointStats:
    completed: int = 0
    successes: int = 0
    worker_ms: int = 0
    roots: set[str] = field(default_factory=set)
    protocols: set[str] = field(default_factory=set)
    strata: set[tuple[str, str]] = field(default_factory=set)
    paired_fail_then_other_pass: int = 0
    fast_completed: int = 0
    fast_entitlement_coverage: int = 0
    fast_entitlement_exhausted: int = 0
    fast_successes: int = 0
    fast_worker_ms: int = 0
    fast_strata: set[tuple[str, str]] = field(default_factory=set)

    @property
    def throughput_lcb(self) -> float:
        worker_seconds = max(0.001, self.worker_ms / 1000.0)
        return (
            _wilson_lower(self.successes, self.completed)
            * self.completed
            / worker_seconds
        )

    @property
    def fast_throughput_lcb(self) -> float:
        worker_seconds = max(0.001, self.fast_worker_ms / 1000.0)
        return (
            _wilson_lower(self.fast_successes, self.fast_completed)
            * self.fast_completed
            / worker_seconds
        )


@dataclass(slots=True)
class _PairEvidence:
    count: int = 0
    roots: set[str] = field(default_factory=set)
    protocols: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class EndpointOrderDecision:
    endpoints: tuple[str, ...]
    policy_revision: int
    portfolio_digest: str
    promoted_endpoint: str = ""
    configured_order: bool = True
    stage: ValidationStage = ValidationStage.CANONICAL_DEEP
    remaining_endpoints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EndpointWorkDecision:
    endpoint: str
    stage: ValidationStage
    policy_revision: int
    portfolio_digest: str
    selection_reason: str
    source_root: str
    source_family: str
    coverage_epoch: int
    canonical_position: int


class Tier3EndpointController:
    """Expansion-scoped endpoint ordering; it has no admission authority."""

    def __init__(self, endpoints: Iterable[str]) -> None:
        self._endpoints = tuple(dict.fromkeys(str(item) for item in endpoints if str(item)))
        self._http = tuple(
            endpoint
            for endpoint in self._endpoints
            if urlsplit(endpoint).scheme.lower() == "http"
        )
        self._lock = threading.Lock()
        self._fast_rotation = self._build_fast_rotation(self._endpoints)
        self._fast_entitlement_count = 0
        self._fast_completed_entitlement_count = 0
        self._fast_rotor_by_stratum: dict[tuple[str, str], int] = {}
        self._fast_dispatch_by_endpoint = {
            endpoint: 0 for endpoint in self._endpoints
        }
        self._canonical_dispatch_by_stratum_endpoint: dict[
            tuple[str, str, str], int
        ] = {}
        self._decision_count = 0
        self._stats = {endpoint: _EndpointStats() for endpoint in self._endpoints}
        self._pair_evidence: dict[tuple[str, str], _PairEvidence] = {}
        self._seen_attempt_ids: set[str] = set()
        self._seen_pair_ids: set[tuple[str, str]] = set()
        self._promoted_endpoint = ""
        self._promotion_reason = ""
        self._temporary_promoted_endpoint = ""
        self._temporary_fast_decisions_remaining = 0
        self._policy_revision = 1

    @staticmethod
    def _build_fast_rotation(endpoints: tuple[str, ...]) -> tuple[str, ...]:
        """Interleave schemes while retaining configured order within each scheme."""
        if len(endpoints) <= 1:
            return endpoints
        by_scheme: dict[str, list[str]] = {}
        scheme_order: list[str] = []
        for endpoint in endpoints:
            scheme = urlsplit(endpoint).scheme.lower() or "unknown"
            if scheme not in by_scheme:
                by_scheme[scheme] = []
                scheme_order.append(scheme)
            by_scheme[scheme].append(endpoint)
        ordered: list[str] = []
        while len(ordered) < len(endpoints):
            for scheme in scheme_order:
                bucket = by_scheme[scheme]
                if bucket:
                    ordered.append(bucket.pop(0))
        return tuple(ordered)

    def ordered(self, candidate_digest: str) -> tuple[str, ...]:
        return self.decision(candidate_digest).endpoints

    def decision(self, candidate_digest: str) -> EndpointOrderDecision:
        with self._lock:
            promoted = self._promoted_endpoint
            revision = self._policy_revision
        configured_order = True
        ordered = self._endpoints
        if promoted and promoted in self._endpoints:
            digest = hashlib.sha256(
                str(candidate_digest).encode("ascii", "ignore")
            ).digest()
            fraction = int.from_bytes(digest[:2], "big") / 65535.0
            if fraction < 0.75 and self._endpoints[0] != promoted:
                configured_order = False
                ordered = (promoted,) + tuple(
                    endpoint for endpoint in self._endpoints if endpoint != promoted
                )
        portfolio_digest = hashlib.sha256(
            b"\x00".join(endpoint.encode("utf-8") for endpoint in ordered)
        ).hexdigest()[:24]
        return EndpointOrderDecision(
            endpoints=ordered,
            policy_revision=revision,
            portfolio_digest=portfolio_digest,
            promoted_endpoint=promoted,
            configured_order=configured_order,
        )

    def slice_decision(
        self,
        candidate_digest: str,
        stage: ValidationStage,
        *,
        attempt_facts: tuple[ProbeAttemptFact, ...] = (),
        remaining_urls: tuple[str, ...] = (),
    ) -> EndpointOrderDecision:
        """Choose only service order; it never changes validation semantics."""
        with self._lock:
            promoted = self._promoted_endpoint
            revision = self._policy_revision
            fast_entitlement = self._fast_entitlement_count
            if stage is ValidationStage.FAST_PROBE:
                self._fast_entitlement_count += 1
                learned, promotion_reason = self._eligible_promotion_locked()
                if learned != self._promoted_endpoint:
                    self._promoted_endpoint = learned
                    self._promotion_reason = promotion_reason
                    self._policy_revision += 1
                elif learned:
                    self._promotion_reason = promotion_reason
                promoted = self._promoted_endpoint
                revision = self._policy_revision
            positive_fast_endpoints = {
                endpoint
                for endpoint, stats in self._stats.items()
                if stats.fast_throughput_lcb > 0.0
            }

        if stage is ValidationStage.FAST_PROBE:
            candidates = self._fast_rotation
            if not candidates:
                ordered: tuple[str, ...] = ()
            elif fast_entitlement < 32:
                # The first cohort is an entitlement audit, not a bandit
                # decision. Every configured endpoint and scheme receives a
                # real mixed-port slice before yield history can steer traffic.
                ordered = (candidates[fast_entitlement % len(candidates)],)
            else:
                digest = hashlib.sha256(
                    str(candidate_digest).encode("ascii", "ignore")
                ).digest()
                fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
                if promoted in candidates and fraction < 0.75:
                    selected = promoted
                else:
                    exploration = tuple(
                        endpoint for endpoint in candidates if endpoint != promoted
                    ) or candidates
                    selected = exploration[int.from_bytes(digest[4:8], "big") % len(exploration)]
                ordered = (selected,)
            remainder = tuple(
                endpoint for endpoint in self._endpoints if endpoint not in ordered
            )
        else:
            ordered = self._deep_order(
                attempt_facts=attempt_facts,
                remaining_urls=remaining_urls,
                positive_fast_endpoints=positive_fast_endpoints,
            )
            remainder = ()

        portfolio_digest = hashlib.sha256(
            stage.value.encode("ascii")
            + b"\x00"
            + b"\x00".join(endpoint.encode("utf-8") for endpoint in ordered)
        ).hexdigest()[:24]
        return EndpointOrderDecision(
            endpoints=ordered,
            policy_revision=revision,
            portfolio_digest=portfolio_digest,
            promoted_endpoint=promoted,
            configured_order=(ordered == self._endpoints),
            stage=stage,
            remaining_endpoints=remainder,
        )

    def work_decision(
        self,
        candidate_digest: str,
        stage: ValidationStage,
        *,
        source_root: str,
        source_family: str,
        attempt_facts: tuple[ProbeAttemptFact, ...] = (),
        remaining_urls: tuple[str, ...] = (),
    ) -> EndpointWorkDecision:
        """Return the single endpoint a worker must execute for this dispatch."""
        root = str(source_root or "unknown")
        family = str(source_family or "unknown")
        stratum = (root, family)
        with self._lock:
            self._decision_count += 1
            revision = self._policy_revision
            if stage is ValidationStage.FAST_PROBE:
                endpoint, reason, epoch = self._select_fast_locked(
                    candidate_digest,
                    stratum,
                )
                self._fast_entitlement_count += 1
                if endpoint:
                    self._fast_dispatch_by_endpoint[endpoint] = (
                        self._fast_dispatch_by_endpoint.get(endpoint, 0) + 1
                    )
            else:
                endpoint, reason, epoch = self._select_canonical_locked(
                    stratum,
                    attempt_facts=attempt_facts,
                    remaining_urls=remaining_urls,
                )
                if endpoint:
                    key = (root, family, endpoint)
                    self._canonical_dispatch_by_stratum_endpoint[key] = (
                        self._canonical_dispatch_by_stratum_endpoint.get(key, 0)
                        + 1
                    )
            revision = self._policy_revision
        canonical_position = (
            self._endpoints.index(endpoint) if endpoint in self._endpoints else -1
        )
        portfolio_digest = hashlib.sha256(
            b"\x00".join(
                (
                    stage.value.encode("ascii"),
                    endpoint.encode("utf-8"),
                    str(revision).encode("ascii"),
                )
            )
        ).hexdigest()[:24]
        return EndpointWorkDecision(
            endpoint=endpoint,
            stage=stage,
            policy_revision=revision,
            portfolio_digest=portfolio_digest,
            selection_reason=reason,
            source_root=root,
            source_family=family,
            coverage_epoch=epoch,
            canonical_position=canonical_position,
        )

    def _select_fast_locked(
        self,
        candidate_digest: str,
        stratum: tuple[str, str],
    ) -> tuple[str, str, int]:
        candidates = self._fast_rotation
        if not candidates:
            return "", "portfolio_empty", 0
        rotor = self._fast_rotor_by_stratum.get(stratum, 0)
        coverage_ready = self._fast_coverage_ready_locked()
        if not coverage_ready:
            # Each root/family owns an independent rotor. The deterministic
            # offset prevents many newly-arriving strata from all selecting
            # the same endpoint while global coverage debt is still high.
            offset = int.from_bytes(
                hashlib.sha256(
                    f"{stratum[0]}\x00{stratum[1]}".encode("utf-8")
                ).digest()[:4],
                "big",
            ) % len(candidates)
            endpoint = candidates[(offset + rotor) % len(candidates)]
            self._fast_rotor_by_stratum[stratum] = rotor + 1
            return endpoint, "stratified_warmup", rotor // len(candidates)

        if (
            self._temporary_fast_decisions_remaining > 0
            and self._temporary_promoted_endpoint in candidates
        ):
            promoted = self._temporary_promoted_endpoint
            self._temporary_fast_decisions_remaining -= 1
            digest = hashlib.sha256(
                str(candidate_digest).encode("ascii", "ignore")
            ).digest()
            fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
            if fraction < 0.75:
                return promoted, "deep_pass_two_wave_tilt", rotor
            exploration = tuple(
                endpoint for endpoint in candidates if endpoint != promoted
            ) or candidates
            endpoint = exploration[
                int.from_bytes(digest[4:8], "big") % len(exploration)
            ]
            return endpoint, "deep_pass_bounded_exploration", rotor

        promoted, promotion_reason = self._eligible_promotion_locked()
        if promoted != self._promoted_endpoint:
            self._promoted_endpoint = promoted
            self._promotion_reason = promotion_reason
            self._policy_revision += 1
        digest = hashlib.sha256(
            str(candidate_digest).encode("ascii", "ignore")
        ).digest()
        fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        if promoted in candidates and fraction < 0.75:
            return promoted, promotion_reason or "promoted_75_percent", rotor
        exploration = tuple(
            endpoint for endpoint in candidates if endpoint != promoted
        ) or candidates
        endpoint = exploration[int.from_bytes(digest[4:8], "big") % len(exploration)]
        return endpoint, "bounded_exploration", rotor

    def _select_canonical_locked(
        self,
        stratum: tuple[str, str],
        *,
        attempt_facts: tuple[ProbeAttemptFact, ...],
        remaining_urls: tuple[str, ...],
    ) -> tuple[str, str, int]:
        completed = {
            fact.endpoint
            for fact in attempt_facts
            if fact.stage == ValidationStage.CANONICAL_DEEP.value
            and fact.completed
            and fact.full_budget
        }
        remaining = tuple(
            endpoint
            for endpoint in (remaining_urls or self._endpoints)
            if endpoint in self._endpoints and endpoint not in completed
        )
        if not remaining:
            return "", "canonical_exhausted", 0

        fast_facts = [
            fact
            for fact in attempt_facts
            if fact.stage in {
                ValidationStage.FAST_PROBE.value,
                "fast_http",
            }
            and fact.endpoint in self._endpoints
        ]
        latest_fast = fast_facts[-1] if fast_facts else None
        fast_endpoint = latest_fast.endpoint if latest_fast is not None else ""
        if (
            latest_fast is not None
            and not latest_fast.completed
            and fast_endpoint in remaining
        ):
            endpoint = fast_endpoint
            reason = "canonical_fast_censored_entitlement"
        elif latest_fast is not None and latest_fast.completed:
            fast_position = self._endpoints.index(fast_endpoint)
            configured_after_fast = (
                self._endpoints[fast_position + 1 :]
                + self._endpoints[: fast_position + 1]
            )
            endpoint = next(
                (
                    configured
                    for configured in configured_after_fast
                    if configured in remaining
                ),
                remaining[0],
            )
            reason = "canonical_after_completed_fast"
        else:
            endpoint = next(
                (
                    configured
                    for configured in self._endpoints
                    if configured in remaining
                ),
                remaining[0],
            )
            reason = "canonical_configured_order"
        epoch = self._canonical_dispatch_by_stratum_endpoint.get(
            (stratum[0], stratum[1], endpoint),
            0,
        )
        return endpoint, reason, epoch

    def _fast_coverage_ready_locked(self) -> bool:
        if self._fast_completed_entitlement_count < 32:
            return False
        return bool(self._endpoints) and all(
            len(self._stats[endpoint].fast_strata) >= 2
            for endpoint in self._endpoints
        )

    def _deep_order(
        self,
        *,
        attempt_facts: tuple[ProbeAttemptFact, ...],
        remaining_urls: tuple[str, ...],
        positive_fast_endpoints: set[str],
    ) -> tuple[str, ...]:
        facts_by_endpoint: dict[str, list[ProbeAttemptFact]] = {}
        for fact in attempt_facts:
            facts_by_endpoint.setdefault(fact.endpoint, []).append(fact)

        consumed = {
            endpoint
            for endpoint, facts in facts_by_endpoint.items()
            if any(
                fact.completed
                and fact.full_budget
                and fact.stage == ValidationStage.CANONICAL_DEEP.value
                for fact in facts
            )
        }
        fast_endpoints = [
            fact.endpoint
            for fact in attempt_facts
            if fact.stage in {
                ValidationStage.FAST_PROBE.value,
                "fast_http",
            }
            and fact.endpoint not in consumed
        ]
        preferred_fast = next(
            (
                endpoint
                for endpoint in reversed(fast_endpoints)
                if endpoint in positive_fast_endpoints
            ),
            "",
        )
        ordered: list[str] = []
        # With no positive FAST evidence, canonical validation is exactly the
        # configured portfolio. A FAST slice is only right-censored evidence
        # and never consumes canonical work. Positive session-wide evidence
        # may promote that one endpoint, while every remaining endpoint keeps
        # its configured relative order.
        # ``remaining_urls`` from FAST is diagnostic only. FAST exposure never
        # consumes canonical entitlement, so DEEP always rebuilds from the
        # full configured portfolio and filters only canonical full-budget
        # completions above.
        configured = self._endpoints
        candidates = (
            (preferred_fast,) + configured + self._endpoints
            if preferred_fast
            else configured + self._endpoints
        )
        for endpoint in candidates:
            if endpoint in consumed or endpoint in ordered:
                continue
            ordered.append(endpoint)
        return tuple(ordered)

    def observe(self, terminal: ValidationTerminal, *, root: str, protocol: str) -> None:
        self.observe_attempts(
            terminal.attempt_facts,
            passed_endpoint=terminal.probe_url,
            root=root,
            protocol=protocol,
        )

    def observe_attempts(
        self,
        attempt_facts: tuple[ProbeAttemptFact, ...],
        *,
        passed_endpoint: str = "",
        root: str,
        protocol: str,
        fast_tilt_budget: int = 0,
    ) -> None:
        observed = [
            fact
            for fact in attempt_facts
            if fact.endpoint in self._stats
            and self._attempt_counts_for_coverage(fact)
        ]
        if not observed:
            return
        passed_endpoint = str(passed_endpoint or "")
        root_value = str(root or "unknown")
        protocol_value = str(protocol or "unknown")
        with self._lock:
            accepted: list[ProbeAttemptFact] = []
            for fact in observed:
                attempt_key = str(fact.attempt_id or "")
                if attempt_key and attempt_key in self._seen_attempt_ids:
                    continue
                if attempt_key:
                    self._seen_attempt_ids.add(attempt_key)
                accepted.append(fact)
                stats = self._stats[fact.endpoint]
                # Censored transport consumes capacity without becoming a
                # completed failure. Yield is success per total worker exposure.
                stats.worker_ms += max(1, int(fact.elapsed_ms))
                if fact.stage in {
                    ValidationStage.FAST_PROBE.value,
                    "fast_http",
                }:
                    stats.fast_entitlement_coverage += 1
                    stats.fast_worker_ms += max(1, int(fact.elapsed_ms))
                    stats.fast_strata.add((root_value, protocol_value))
                    self._fast_completed_entitlement_count += 1
                    if fact.entitlement_exhausted and not fact.completed:
                        stats.fast_entitlement_exhausted += 1
                if not self._attempt_is_endpoint_evidence(fact):
                    continue
                stats.completed += 1
                stats.roots.add(root_value)
                stats.protocols.add(protocol_value)
                stats.strata.add((root_value, protocol_value))
                if fact.outcome == "pass":
                    stats.successes += 1
                if fact.stage in {
                    ValidationStage.FAST_PROBE.value,
                    "fast_http",
                }:
                    stats.fast_completed += 1
                    if fact.outcome == "pass":
                        stats.fast_successes += 1
            if passed_endpoint:
                passed_fact = next(
                    (
                        fact
                        for fact in accepted
                        if fact.endpoint == passed_endpoint
                        or fact.outcome == "pass"
                    ),
                    None,
                )
                for fact in accepted:
                    if not self._attempt_is_endpoint_evidence(fact):
                        continue
                    if fact.endpoint == passed_endpoint or fact.outcome == "pass":
                        break
                    pair_key = None
                    if (
                        fact.attempt_id
                        and passed_fact is not None
                        and passed_fact.attempt_id
                    ):
                        pair_key = (fact.attempt_id, passed_fact.attempt_id)
                    if pair_key is not None:
                        if pair_key in self._seen_pair_ids:
                            continue
                        self._seen_pair_ids.add(pair_key)
                    self._stats[fact.endpoint].paired_fail_then_other_pass += 1
                    pair = self._pair_evidence.setdefault(
                        (fact.endpoint, passed_endpoint),
                        _PairEvidence(),
                    )
                    pair.count += 1
                    pair.roots.add(root_value)
                    pair.protocols.add(protocol_value)
                deep_pass = any(
                    fact.outcome == "pass"
                    and fact.stage == ValidationStage.CANONICAL_DEEP.value
                    and self._attempt_is_endpoint_evidence(fact)
                    for fact in accepted
                )
                if deep_pass and fast_tilt_budget > 0:
                    self._temporary_promoted_endpoint = passed_endpoint
                    self._temporary_fast_decisions_remaining = max(
                        self._temporary_fast_decisions_remaining,
                        int(fast_tilt_budget),
                    )
            promoted, promotion_reason = self._eligible_promotion_locked()
            if promoted != self._promoted_endpoint:
                self._promoted_endpoint = promoted
                self._promotion_reason = promotion_reason
                self._policy_revision += 1
            elif promoted:
                self._promotion_reason = promotion_reason

    @staticmethod
    def _attempt_is_endpoint_evidence(fact: ProbeAttemptFact) -> bool:
        return bool(fact.completed)

    @staticmethod
    def _attempt_counts_for_coverage(fact: ProbeAttemptFact) -> bool:
        return bool(fact.completed or fact.entitlement_exhausted)

    def _eligible_promotion_locked(self) -> tuple[str, str]:
        if self._fast_entitlement_count < 32:
            return "", ""
        eligible: list[tuple[float, int, str]] = []
        for (_failed, passed), evidence in self._pair_evidence.items():
            if (
                evidence.count < 8
                or len(evidence.roots) < 2
                or len(evidence.protocols) < 2
            ):
                continue
            stats = self._stats.get(passed)
            if stats is None or stats.throughput_lcb <= 0.0:
                continue
            eligible.append((stats.throughput_lcb, evidence.count, passed))
        eligible.sort(reverse=True)
        if eligible:
            return eligible[0][2], "paired_candidate_evidence"

        # FAST endpoint assignment is a stable randomized arm selection,
        # independent of source/ranker state. Once the 32-entitlement coverage
        # cohort has completed, worker-second yield can safely choose the best
        # first endpoint while preserving 25% exploration.
        sampled: list[tuple[float, str, _EndpointStats]] = []
        for endpoint in self._endpoints:
            stats = self._stats.get(endpoint)
            if (
                stats is None
                or stats.fast_entitlement_coverage < 4
                or len(stats.fast_strata) < 2
            ):
                continue
            sampled.append((stats.fast_throughput_lcb, endpoint, stats))
        if len(sampled) != len(self._endpoints) or len(sampled) < 2:
            return "", ""
        sampled.sort(reverse=True)
        best_score, best_endpoint, best_stats = sampled[0]
        runner_score, _runner_endpoint, _runner_stats = sampled[1]
        if best_stats.fast_successes >= 1 and best_score > max(runner_score, 0.0):
            return best_endpoint, "fast_worker_second_lcb"
        return "", ""

    def snapshot(self) -> dict[str, dict[str, float | int | str | bool]]:
        with self._lock:
            snapshot: dict[str, dict[str, float | int | str | bool]] = {
                self._diagnostic_label(endpoint): {
                    "completed": stats.completed,
                    "successes": stats.successes,
                    "worker_ms": stats.worker_ms,
                    "root_count": len(stats.roots),
                    "protocol_count": len(stats.protocols),
                    "stratum_count": len(stats.strata),
                    "paired_fail_then_other_pass": stats.paired_fail_then_other_pass,
                    "throughput_lcb": stats.throughput_lcb,
                    "fast_completed": stats.fast_completed,
                    "fast_entitlement_coverage": (
                        stats.fast_entitlement_coverage
                    ),
                    "fast_entitlement_exhausted": (
                        stats.fast_entitlement_exhausted
                    ),
                    "fast_successes": stats.fast_successes,
                    "fast_worker_ms": stats.fast_worker_ms,
                    "fast_stratum_count": len(stats.fast_strata),
                    "fast_throughput_lcb": stats.fast_throughput_lcb,
                }
                for endpoint, stats in self._stats.items()
            }
            snapshot["_policy"] = {
                "revision": self._policy_revision,
                "yield_metric_revision": 2,
                "worker_time_includes_exhausted": True,
                "promoted_endpoint": (
                    self._diagnostic_label(self._promoted_endpoint)
                    if self._promoted_endpoint
                    else ""
                ),
                "configured_endpoint_count": len(self._endpoints),
                "paired_evidence_count": sum(
                    evidence.count for evidence in self._pair_evidence.values()
                ),
                "promotion_reason": self._promotion_reason,
                "temporary_promoted_endpoint": (
                    self._diagnostic_label(self._temporary_promoted_endpoint)
                    if self._temporary_promoted_endpoint
                    else ""
                ),
                "temporary_fast_decisions_remaining": (
                    self._temporary_fast_decisions_remaining
                ),
                "configured_order_default": True,
                "fast_entitlement_count": self._fast_entitlement_count,
                "fast_completed_entitlement_count": (
                    self._fast_completed_entitlement_count
                ),
                "decision_count": self._decision_count,
                "fast_stratified_warmup_complete": (
                    self._fast_coverage_ready_locked()
                ),
            }
            return snapshot

    @staticmethod
    def _diagnostic_label(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        authority = parsed.hostname or "unknown"
        if parsed.port:
            authority = f"{authority}:{parsed.port}"
        digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]
        return f"{parsed.scheme or 'unknown'}://{authority}#{digest}"
