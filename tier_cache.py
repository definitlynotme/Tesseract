from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, TextIO

from atomic_file import replace_with_retry
from models import Node, NodeStatus, utc_now_iso
from tier3_origin import is_tier3_auto_pool_node


HEALTHY_STATUSES = {NodeStatus.ALIVE.value, NodeStatus.SLOW.value}
NEGATIVE_CACHE_STATUSES = {NodeStatus.DEAD.value, NodeStatus.SUSPICIOUS.value}
EVICTED_FROM_POOL_MARKER = "tier3_evicted_from_pool"
EVICTED_FROM_POOL_AT_MARKER = "tier3_evicted_from_pool_at"
EVICTION_FAILURE_COUNT_MARKER = "tier3_eviction_failure_count"
NON_PERSISTENT_NEGATIVE_FAILURE_STAGES = {
    "mihomo_batch_validation_failed",
    "mihomo_delay_failed",
    "mihomo_delay_503",
    "mihomo_delay_504",
    "mihomo_delay_transient",
    "candidate_transient_503",
    "candidate_transient_504",
    "all_probe_transient",
    "full_validation_all_probe_transient",
    "full_validation_probe_deadline",
    "full_validation_executor_deadline",
    "full_validation_facility_deadline",
    "tcp_deferred",
    "tcp_cancelled",
    "validation_deferred",
    "validation_cancelled",
    "validator_facility_failure",
}

PROCESS_LOCAL_SCHEDULER_EXTRA_KEYS = frozenset({
    "tier3_source_key",
    "tier3_source_arm_key",
    "tier3_source_arm",
    "tier3_source_coarse_key",
    "tier3_source_diversity_key",
    "tier3_source_root_key",
    "tier3_source_source_root_key",
})

# One validation lease can produce several kilobytes of attempt metadata. It is
# useful in bounded diagnostics, but it is not part of a durable candidate and
# must not make the observation cache grow on every refresh.
NON_PERSISTENT_OBSERVATION_EXTRA_KEYS = {
    "tier3_admission_commit_elapsed_ms",
    "tier3_admission_commit_identity",
    "tier3_admission_commit_queue_wait_ms",
    "tier3_admission_commit_sequence",
    "tier3_admission_commit_skipped_reason",
    "tier3_admission_pool_commit_elapsed_ms",
    "tier3_admission_pool_revision",
    "tier3_candidate_digest",
    "tier3_candidate_dispatch_ticket",
    "tier3_capacity_epoch_id_at_dispatch",
    "tier3_config_digest",
    "tier3_expansion_generation_id",
    "tier3_probe_attempt_facts",
    "tier3_probe_urls_censored",
    "tier3_probe_urls_completed_all",
    "tier3_probe_urls_remaining",
    "tier3_validator_lease_id",
    "tier3_validator_refresh_epoch",
    "tier3_validator_transaction_id",
    "validation_probe_failures_by_url",
    "validation_probe_urls_attempted",
} | PROCESS_LOCAL_SCHEDULER_EXTRA_KEYS


def _write_observation_payload(handle: TextIO, payload: dict[str, Any]) -> None:
    # A whole-cache C encoder can hold the GIL for over a second. Encode one
    # record at a time so the Pipe reader and TCP deadlines can keep running.
    encode = json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).encode
    fragments: list[str] = []
    pending_chars = 0

    def emit(fragment: str) -> None:
        nonlocal pending_chars
        fragments.append(fragment)
        pending_chars += len(fragment)
        if pending_chars >= 256 * 1024:
            handle.write("".join(fragments))
            fragments.clear()
            pending_chars = 0

    emit("{")
    for field_index, (key, value) in enumerate(payload.items()):
        if field_index:
            emit(",")
        emit(encode(key))
        emit(":")
        if isinstance(value, list):
            emit("[")
            for record_index, record in enumerate(value):
                if record_index:
                    emit(",")
                emit(encode(record))
            emit("]")
        else:
            emit(encode(value))
    emit("}")
    if fragments:
        handle.write("".join(fragments))


@dataclass
class CacheRevivalResult:
    candidates: list[Node]
    scanned: int
    skipped_recently: int = 0


class Tier3ObservationKind(str, Enum):
    """Authority carried by one Tier3 observation cache update."""

    MIXED_PORT_PASS = "mixed_port_pass"
    DETERMINISTIC_VALIDATION_FAIL = "deterministic_validation_fail"
    RIGHT_CENSORED = "right_censored"
    TCP_RESPONSIVE = "tcp_responsive"
    TCP_DEAD = "tcp_dead"
    POOL_HEALTH_PASS = "pool_health_pass"
    POOL_HEALTH_FAIL = "pool_health_fail"


@dataclass(frozen=True, slots=True)
class Tier3ObservationFact:
    node: Node
    kind: Tier3ObservationKind
    observed_at: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Tier3ValidatedReplayCandidate:
    """One bounded, source-authorized candidate for full validation replay."""

    config_digest: str
    source_authority: str
    pass_observed_at: str
    pass_count: int
    contract_matches: bool
    tcp_reprobe_required: bool
    visibility_class: str
    trust_class: str
    node: Node


@dataclass(frozen=True, slots=True)
class Tier3ValidatedReplaySnapshot:
    """Private in-memory replay inventory for one expansion generation."""

    generated_at: str
    source_generated_at: str
    candidates: tuple[Tier3ValidatedReplayCandidate, ...] = ()
    configured_source_authority_count: int = 0
    considered_record_count: int = 0
    source_authority_excluded_count: int = 0
    deterministic_failure_excluded_count: int = 0
    expired_record_count: int = 0
    invalid_record_count: int = 0
    duplicate_digest_count: int = 0
    limit: int = 0
    replay_days: int = 0
    fresh_days: int = 0

    def to_diagnostic_dict(self) -> dict[str, Any]:
        positive = sum(
            1
            for candidate in self.candidates
            if candidate.visibility_class == "validated_positive"
        )
        reprobe = sum(
            1
            for candidate in self.candidates
            if candidate.visibility_class == "validated_reprobe"
        )
        fresh = sum(
            1
            for candidate in self.candidates
            if candidate.trust_class == "fresh_validated"
        )
        historical = sum(
            1
            for candidate in self.candidates
            if candidate.trust_class == "historical_reprobe"
        )
        return {
            "generated_at": self.generated_at,
            "source_generated_at": self.source_generated_at,
            "configured_source_authority_count": int(
                self.configured_source_authority_count
            ),
            "considered_record_count": int(self.considered_record_count),
            "candidate_count": len(self.candidates),
            "validated_positive_count": positive,
            "validated_reprobe_count": reprobe,
            "fresh_validated_count": fresh,
            "historical_reprobe_count": historical,
            "source_authority_excluded_count": int(
                self.source_authority_excluded_count
            ),
            "deterministic_failure_excluded_count": int(
                self.deterministic_failure_excluded_count
            ),
            "expired_record_count": int(self.expired_record_count),
            "invalid_record_count": int(self.invalid_record_count),
            "duplicate_digest_count": int(self.duplicate_digest_count),
            "limit": int(self.limit),
            "replay_days": int(self.replay_days),
            "fresh_days": int(self.fresh_days),
        }


@dataclass(frozen=True, slots=True)
class Tier3StableObservationSnapshot:
    """Process-local exact config evidence for one expansion generation.

    Raw cache identities and node payloads deliberately do not leave the cache
    owner.  The scheduler can only ask whether a candidate's canonical
    validation config digest was recently healthy, previously observed but not
    currently positive, or unknown.
    """

    generated_at: str
    source_generated_at: str
    validated_positive_config_digests: frozenset[str] = frozenset()
    validated_reprobe_config_digests: frozenset[str] = frozenset()
    tcp_only_config_digests: frozenset[str] = frozenset()
    observed_nonpositive_config_digests: frozenset[str] = frozenset()
    observed_record_count: int = 0
    validated_positive_record_count: int = 0
    validated_reprobe_record_count: int = 0
    tcp_only_record_count: int = 0
    transport_failure_reprobe_record_count: int = 0
    expired_record_count: int = 0
    newer_negative_excluded_count: int = 0
    invalid_record_count: int = 0

    @property
    def exact_positive_config_digests(self) -> frozenset[str]:
        """One-schema compatibility alias for validated replay."""

        return self.validated_positive_config_digests

    @property
    def exact_positive_record_count(self) -> int:
        """One-schema compatibility alias for validated replay."""

        return self.validated_positive_record_count

    def classification_for(self, config_digest: str) -> str:
        digest = str(config_digest or "").strip()
        if digest in self.validated_positive_config_digests:
            return "validated_positive"
        if digest in self.validated_reprobe_config_digests:
            return "validated_reprobe"
        if digest in self.tcp_only_config_digests:
            return "tcp_only"
        if digest in self.observed_nonpositive_config_digests:
            return "observed_nonpositive"
        return "unknown"

    def to_diagnostic_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source_generated_at": self.source_generated_at,
            "observed_record_count": int(self.observed_record_count),
            "validated_positive_record_count": int(
                self.validated_positive_record_count
            ),
            "validated_positive_digest_count": len(
                self.validated_positive_config_digests
            ),
            "validated_reprobe_record_count": int(
                self.validated_reprobe_record_count
            ),
            "validated_reprobe_digest_count": len(
                self.validated_reprobe_config_digests
            ),
            "tcp_only_record_count": int(self.tcp_only_record_count),
            "transport_failure_reprobe_record_count": int(
                self.transport_failure_reprobe_record_count
            ),
            "tcp_only_digest_count": len(self.tcp_only_config_digests),
            "observed_nonpositive_digest_count": len(
                self.observed_nonpositive_config_digests
            ),
            # Compatibility aliases are exact copies, not independent truth.
            "exact_positive_record_count": int(
                self.validated_positive_record_count
            ),
            "exact_positive_digest_count": len(
                self.validated_positive_config_digests
            ),
            "expired_record_count": int(self.expired_record_count),
            "newer_negative_excluded_count": int(
                self.newer_negative_excluded_count
            ),
            "invalid_record_count": int(self.invalid_record_count),
        }


@dataclass(frozen=True, slots=True)
class _EntryCooldownSnapshot:
    file_signature: tuple[int, int] | None
    failures: Mapping[str, tuple[datetime, bool]]


class TierObservationCache:
    def __init__(
        self,
        path: str | Path,
        *,
        history_limit: int = 5000,
        dead_evict_threshold: int = 3,
        revive_cooldown_hours: float = 24.0,
    ) -> None:
        self.path = Path(path)
        self.history_limit = max(1, int(history_limit))
        self.dead_evict_threshold = max(1, int(dead_evict_threshold))
        self.revive_cooldown_hours = max(0.0, float(revive_cooldown_hours))
        self._lock = threading.RLock()
        self._cached_file_signature: tuple[int, int] | None = None
        self._cached_payload: dict[str, Any] | None = None
        self._cached_records: dict[str, dict[str, Any]] | None = None
        self._cached_negative_records: dict[str, dict[str, Any]] | None = None
        self._entry_cooldown_snapshot: _EntryCooldownSnapshot | None = None
        self._cache_initialized = False

    def load(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_cached_payload_locked())

    def stable_tier3_observation_snapshot(
        self,
        *,
        days: int,
        fresh_days: int | None = None,
        config_digest_provider: Callable[[Node], str],
        validation_contract_hash: str = "",
        now: datetime | None = None,
    ) -> Tier3StableObservationSnapshot:
        """Build one immutable exact-positive view without reviving nodes."""

        with self._lock:
            payload = self._load_cached_payload_locked()
            records = dict(self._cached_records or {})
            negative_records = dict(self._cached_negative_records or {})

        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        else:
            effective_now = effective_now.astimezone(timezone.utc)
        replay_days = max(1, min(30, int(days)))
        fresh_window_days = max(
            1,
            min(replay_days, int(fresh_days or replay_days)),
        )
        cutoff = effective_now - timedelta(days=replay_days)
        fresh_cutoff = effective_now - timedelta(days=fresh_window_days)
        validated_positive: set[str] = set()
        validated_reprobe: set[str] = set()
        tcp_only: set[str] = set()
        observed_nonpositive: set[str] = set()
        observed_count = 0
        positive_count = 0
        reprobe_count = 0
        tcp_only_count = 0
        transport_failure_reprobe_count = 0
        expired_count = 0
        newer_negative_count = 0
        invalid_count = 0

        for identity, item in records.items():
            if not self._record_is_tier3(item):
                continue
            node_payload = item.get("node")
            if not isinstance(node_payload, dict):
                invalid_count += 1
                continue
            try:
                node = Node.from_dict(node_payload)
            except Exception:
                invalid_count += 1
                continue
            node.tier = "tier3"
            if not is_tier3_auto_pool_node(node):
                continue
            try:
                config_digest = str(config_digest_provider(node) or "").strip()
            except Exception:
                config_digest = ""
            if not config_digest:
                invalid_count += 1
                continue

            observed_count += 1
            authoritative_pass_at = self._record_authoritative_pass_at(item)
            negative = negative_records.get(str(identity or ""))
            deterministic_failed_at, tcp_dead_at = self._record_failure_times(
                item,
                negative if isinstance(negative, dict) else None,
            )
            newer_negative = bool(
                deterministic_failed_at is not None
                and (
                    authoritative_pass_at is None
                    or deterministic_failed_at >= authoritative_pass_at
                )
            )
            replay_eligible = bool(
                authoritative_pass_at is not None
                and authoritative_pass_at >= cutoff
            )
            if replay_eligible and not newer_negative:
                stored_contract = self._record_validation_contract_hash(item)
                contract_matches = bool(
                    not validation_contract_hash
                    or stored_contract == str(validation_contract_hash)
                )
                tcp_reprobe = bool(
                    tcp_dead_at is not None
                    and tcp_dead_at >= authoritative_pass_at
                )
                fresh_validated = bool(
                    authoritative_pass_at is not None
                    and authoritative_pass_at >= fresh_cutoff
                    and contract_matches
                    and not tcp_reprobe
                )
                if fresh_validated:
                    validated_positive.add(config_digest)
                    positive_count += 1
                else:
                    validated_reprobe.add(config_digest)
                    reprobe_count += 1
                continue
            has_authoritative_history = authoritative_pass_at is not None
            has_nonpositive_history = bool(
                has_authoritative_history
                or deterministic_failed_at is not None
                or str(item.get("observation_authority") or "")
                == "legacy_unverified"
            )
            tcp_responsive_at = self._parse_time(str(item.get("last_tcp_responsive_at") or ""))
            if (
                tcp_responsive_at is not None
                and not has_nonpositive_history
                and (tcp_dead_at is None or tcp_responsive_at > tcp_dead_at)
            ):
                tcp_only.add(config_digest)
                tcp_only_count += 1
                continue
            if not has_nonpositive_history:
                # A previous short reachability probe is not a validation
                # verdict. The new session still owes this source candidate a
                # TCP attempt; its digest ledger prevents same-session loops.
                if tcp_dead_at is not None:
                    transport_failure_reprobe_count += 1
                continue
            observed_nonpositive.add(config_digest)
            if has_authoritative_history and not replay_eligible:
                expired_count += 1
            if newer_negative:
                newer_negative_count += 1

        tcp_only.difference_update(validated_positive)
        tcp_only.difference_update(validated_reprobe)
        observed_nonpositive.difference_update(validated_positive)
        observed_nonpositive.difference_update(validated_reprobe)
        observed_nonpositive.difference_update(tcp_only)
        validated_reprobe.difference_update(validated_positive)
        return Tier3StableObservationSnapshot(
            generated_at=effective_now.isoformat(),
            source_generated_at=str(payload.get("generated_at") or ""),
            validated_positive_config_digests=frozenset(validated_positive),
            validated_reprobe_config_digests=frozenset(validated_reprobe),
            tcp_only_config_digests=frozenset(tcp_only),
            observed_nonpositive_config_digests=frozenset(
                observed_nonpositive
            ),
            observed_record_count=observed_count,
            validated_positive_record_count=positive_count,
            validated_reprobe_record_count=reprobe_count,
            tcp_only_record_count=tcp_only_count,
            transport_failure_reprobe_record_count=transport_failure_reprobe_count,
            expired_record_count=expired_count,
            newer_negative_excluded_count=newer_negative_count,
            invalid_record_count=invalid_count,
        )

    def validated_tier3_replay_snapshot(
        self,
        *,
        days: int,
        fresh_days: int | None = None,
        limit: int,
        configured_source_authorities: Iterable[str],
        config_digest_provider: Callable[[Node], str],
        validation_contract_hash: str,
        now: datetime | None = None,
    ) -> Tier3ValidatedReplaySnapshot:
        """Build bounded candidates without admitting or mutating the pool."""

        with self._lock:
            payload = self._load_cached_payload_locked()
            records = dict(self._cached_records or {})
            negative_records = dict(self._cached_negative_records or {})

        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        else:
            effective_now = effective_now.astimezone(timezone.utc)
        replay_days = max(1, min(30, int(days)))
        fresh_window_days = max(
            1,
            min(replay_days, int(fresh_days or replay_days)),
        )
        cutoff = effective_now - timedelta(days=replay_days)
        fresh_cutoff = effective_now - timedelta(days=fresh_window_days)
        bounded_limit = max(0, int(limit))
        configured = {
            str(authority or "").strip()
            for authority in configured_source_authorities
            if str(authority or "").strip()
        }
        considered = 0
        source_excluded = 0
        deterministic_excluded = 0
        expired = 0
        invalid = 0
        ranked: list[
            tuple[
                int,
                int,
                float,
                int,
                int,
                Tier3ValidatedReplayCandidate,
            ]
        ] = []

        for record_order, (identity, item) in enumerate(records.items()):
            if not self._record_is_tier3(item):
                continue
            node_payload = item.get("node")
            if not isinstance(node_payload, dict):
                invalid += 1
                continue
            try:
                node = Node.from_dict(node_payload)
            except Exception:
                invalid += 1
                continue
            node.tier = "tier3"
            if not is_tier3_auto_pool_node(node):
                continue
            considered += 1
            source_authority = self._record_source_authority(item, node)
            if not source_authority or source_authority not in configured:
                source_excluded += 1
                continue
            authoritative_pass_at = self._record_authoritative_pass_at(item)
            if authoritative_pass_at is None or authoritative_pass_at < cutoff:
                expired += 1
                continue
            negative = negative_records.get(str(identity or ""))
            deterministic_failed_at, tcp_dead_at = self._record_failure_times(
                item,
                negative if isinstance(negative, dict) else None,
            )
            if (
                deterministic_failed_at is not None
                and deterministic_failed_at >= authoritative_pass_at
            ):
                deterministic_excluded += 1
                continue
            try:
                config_digest = str(config_digest_provider(node) or "").strip()
            except Exception:
                config_digest = ""
            if not config_digest:
                invalid += 1
                continue
            stored_contract = self._record_validation_contract_hash(item)
            contract_matches = bool(
                validation_contract_hash
                and stored_contract == str(validation_contract_hash)
            )
            tcp_reprobe = bool(
                tcp_dead_at is not None and tcp_dead_at >= authoritative_pass_at
            )
            fresh_validated = bool(
                authoritative_pass_at >= fresh_cutoff
                and contract_matches
                and not tcp_reprobe
            )
            visibility_class = (
                "validated_positive"
                if fresh_validated
                else "validated_reprobe"
            )
            trust_class = (
                "fresh_validated"
                if fresh_validated
                else "historical_reprobe"
            )
            replay_node = node.clone()
            replay_node.status = NodeStatus.UNKNOWN
            replay_node.latency_ms = None
            replay_node.suspicious_reason = None
            replay_node.extra["tier3_validated_replay"] = True
            replay_node.extra["tier3_validated_replay_class"] = visibility_class
            replay_node.extra["tier3_validated_replay_trust_class"] = trust_class
            replay_node.extra["tier3_validated_replay_source_authority"] = (
                source_authority
            )
            replay_node.extra["tier3_outside_lane"] = "validated_replay"
            pass_count = max(
                1,
                int(item.get("mixed_port_pass_count", 0) or 0)
                + int(item.get("pool_health_pass_count", 0) or 0),
            )
            candidate = Tier3ValidatedReplayCandidate(
                config_digest=config_digest,
                source_authority=source_authority,
                pass_observed_at=authoritative_pass_at.isoformat(),
                pass_count=pass_count,
                contract_matches=contract_matches,
                tcp_reprobe_required=tcp_reprobe,
                visibility_class=visibility_class,
                trust_class=trust_class,
                node=replay_node,
            )
            ranked.append(
                (
                    0 if fresh_validated else 1,
                    0 if contract_matches else 1,
                    -authoritative_pass_at.timestamp(),
                    -pass_count,
                    record_order,
                    candidate,
                )
            )

        ranked.sort(key=lambda item: item[:4])
        selected: list[Tier3ValidatedReplayCandidate] = []
        seen_digests: set[str] = set()
        duplicate_digests = 0
        for (
            _trust_rank,
            _contract_rank,
            _pass_rank,
            _count_rank,
            _order,
            candidate,
        ) in ranked:
            if candidate.config_digest in seen_digests:
                duplicate_digests += 1
                continue
            seen_digests.add(candidate.config_digest)
            if len(selected) < bounded_limit:
                selected.append(candidate)

        return Tier3ValidatedReplaySnapshot(
            generated_at=effective_now.isoformat(),
            source_generated_at=str(payload.get("generated_at") or ""),
            candidates=tuple(selected),
            configured_source_authority_count=len(configured),
            considered_record_count=considered,
            source_authority_excluded_count=source_excluded,
            deterministic_failure_excluded_count=deterministic_excluded,
            expired_record_count=expired,
            invalid_record_count=invalid,
            duplicate_digest_count=duplicate_digests,
            limit=bounded_limit,
            replay_days=replay_days,
            fresh_days=fresh_window_days,
        )

    def _load_cached_payload_locked(self) -> dict[str, Any]:
        signature = self._file_signature()
        if self._cache_initialized and signature == self._cached_file_signature:
            return self._cached_payload or self._empty_payload()
        payload = self._read_payload_from_disk()
        self._publish_cached_payload(payload, signature=signature)
        return payload

    def observe(self, tier: str, nodes: Iterable[Node]) -> None:
        tier_id = str(tier or "").strip().lower()
        if tier_id not in {"", "tier3"}:
            return
        with self._lock:
            payload = self._load_cached_payload_locked()
            records = dict(self._cached_records or {})
            negative_records = dict(self._cached_negative_records or {})
            now = utc_now_iso()
            changed = False
            for node in nodes:
                changed = self._observe_node(records, tier_id, node, now) or changed
                if (
                    tier_id in {"", "tier3"}
                    and self._status_value(node.status) in HEALTHY_STATUSES
                ):
                    identity = self._node_identity(node, tier="tier3")
                    record = records.get(identity)
                    if record is not None:
                        self._mark_authoritative_pass(
                            record,
                            Tier3ObservationKind.POOL_HEALTH_PASS,
                            now,
                        )
                changed = (
                    self._observe_negative_record(
                        negative_records,
                        tier_id,
                        node,
                        now,
                    )
                    or changed
                )
            if not changed:
                return
            self._write_records(records, generated_at=now, negative_records=negative_records)

    def observe_tier3_facts(
        self,
        facts: Iterable[Tier3ObservationFact],
    ) -> None:
        """Persist validation and TCP facts without conflating authority."""

        with self._lock:
            self._load_cached_payload_locked()
            records = dict(self._cached_records or {})
            negative_records = dict(self._cached_negative_records or {})
            changed = False
            generated_at = utc_now_iso()
            for fact in facts:
                if not isinstance(fact, Tier3ObservationFact):
                    continue
                observed_at = str(fact.observed_at or generated_at)
                changed = (
                    self._observe_tier3_fact(
                        records,
                        negative_records,
                        fact,
                        observed_at,
                    )
                    or changed
                )
            if not changed:
                return
            self._write_records(
                records,
                generated_at=generated_at,
                negative_records=negative_records,
            )

    def filter_recently_failed_tier3(
        self,
        nodes: Iterable[Node],
        *,
        cooldown_hours: float,
        exempt_identities: set[str] | None = None,
    ) -> tuple[list[Node], int]:
        # A reader may use the last complete generation while a writer builds
        # and fsyncs its replacement. Neither mutable records nor the disk IO
        # lock belong on the streamed source-to-TCP path.
        snapshot = self._entry_cooldown_snapshot
        if snapshot is None or snapshot.file_signature != self._file_signature():
            with self._lock:
                self._load_cached_payload_locked()
                snapshot = self._entry_cooldown_snapshot
        assert snapshot is not None
        now = datetime.now(timezone.utc)
        cooldown = timedelta(hours=max(0.0, float(cooldown_hours)))
        exempt = set(exempt_identities or set())
        kept: list[Node] = []
        skipped = 0
        for node in nodes:
            identity = self._node_identity(node, tier="tier3")
            failure = snapshot.failures.get(identity)
            if identity in exempt or failure is None:
                kept.append(node)
                continue
            failed_at, short_cooldown = failure
            effective_cooldown = (
                min(cooldown, timedelta(minutes=15)) if short_cooldown else cooldown
            )
            if effective_cooldown.total_seconds() > 0 and failed_at >= now - effective_cooldown:
                skipped += 1
            else:
                kept.append(node)
        return kept, skipped

    def _build_entry_cooldown_snapshot(self) -> _EntryCooldownSnapshot:
        negatives = self._cached_negative_records or {}
        observations = self._cached_records or {}
        failures: dict[str, tuple[datetime, bool]] = {}
        for identity in negatives.keys() | observations.keys():
            record = negatives.get(identity)
            failed_at = self._parse_time(str(record.get("last_failed_at") or "")) if record else None
            if not record or self._transport_only_failure(record):
                # Typed validation facts survive older TCP-only overwrites.
                observation = observations.get(identity) or {}
                passed_at = self._record_authoritative_pass_at(observation)
                failed_at, _ = self._record_failure_times(observation, record)
                if failed_at is not None and passed_at is not None and passed_at > failed_at:
                    failed_at = None
            if failed_at is None:
                continue
            record = record or {}
            stage = str(record.get("failure_stage") or "")
            reason = str(record.get("reason") or record.get("failure_reason") or "")
            short_cooldown = (
                stage in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
                or (
                    reason.startswith("mihomo_batch_validation_failed:")
                    and any(f"http error {code}" in reason.lower() for code in range(500, 505))
                )
            )
            failures[identity] = (failed_at, short_cooldown)
        return _EntryCooldownSnapshot(
            self._cached_file_signature, MappingProxyType(failures),
        )

    def _file_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return int(stat.st_mtime_ns), int(stat.st_size)

    def _read_payload_from_disk(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_payload()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty_payload()
        if not isinstance(payload, dict):
            return self._empty_payload()
        if not isinstance(payload.get("nodes"), list) and not isinstance(payload.get("observations"), list):
            payload["nodes"] = []
        if not isinstance(payload.get("negative"), list):
            payload["negative"] = []
        payload["nodes"] = [
            self._normalize_v3_record(item)
            for item in payload.get("nodes", [])
            if isinstance(item, dict)
        ]
        payload["schema_version"] = 3
        return payload

    def _publish_cached_payload(
        self,
        payload: dict[str, Any],
        *,
        signature: tuple[int, int] | None = None,
    ) -> None:
        self._cached_payload = payload
        self._cached_records = self._records_from_payload(payload)
        self._cached_negative_records = self._negative_records_from_payload(payload)
        self._cached_file_signature = self._file_signature() if signature is None else signature
        self._entry_cooldown_snapshot = self._build_entry_cooldown_snapshot()
        self._cache_initialized = True

    def revive_tier3(self, *, days: int, limit: int) -> CacheRevivalResult:
        with self._lock:
            payload = self._load_cached_payload_locked()
            records = dict(self._cached_records or {})
            now = datetime.now(timezone.utc)
            now_text = now.isoformat()
            cutoff = now - timedelta(days=max(1, int(days)))
            revive_cooldown = timedelta(hours=self.revive_cooldown_hours)
            candidates: dict[str, Node] = {}
            scanned = 0
            skipped_recently = 0
            changed = False

            for item in self._records_by_recent_observation(records.values()):
                if item.get("tier") != "tier3":
                    continue
                if not self._record_marked_evicted(item):
                    continue
                if self._record_authoritative_pass_at(item) is None:
                    continue
                observed_at = self._parse_time(
                    str(item.get("last_observed_at") or item.get("last_failed_at") or item.get("last_healthy_at") or "")
                )
                if observed_at and observed_at < cutoff:
                    continue
                revived_at = self._parse_time(str(item.get("last_revived_at") or ""))
                if revived_at and revive_cooldown.total_seconds() > 0 and revived_at >= now - revive_cooldown:
                    skipped_recently += 1
                    continue
                node_payload = item.get("node")
                if not isinstance(node_payload, dict):
                    continue
                try:
                    node = Node.from_dict(node_payload)
                except Exception:
                    continue
                node.tier = "tier3"
                if not is_tier3_auto_pool_node(node):
                    continue
                node.status = NodeStatus.UNKNOWN
                node.latency_ms = None
                node.suspicious_reason = None
                node.extra.pop(EVICTED_FROM_POOL_MARKER, None)
                node.extra.pop(EVICTED_FROM_POOL_AT_MARKER, None)
                node.extra.pop(EVICTION_FAILURE_COUNT_MARKER, None)
                node.extra["revived_from_cache"] = True
                node.extra["cache_revive_count"] = int(item.get("revive_count", 0) or 0) + 1
                key = self._node_identity(node)
                scanned += 1
                if key not in candidates:
                    candidates[key] = node
                    record_key = str(item.get("identity") or key)
                    stored_record = records.get(record_key)
                    record = dict(stored_record) if stored_record is not None else None
                    if record is not None:
                        record["last_revived_at"] = now_text
                        record["revive_count"] = int(record.get("revive_count", 0) or 0) + 1
                        records[record_key] = record
                        changed = True
                if len(candidates) >= limit:
                    break

            if changed:
                self._write_records(
                    records,
                    generated_at=now_text,
                    negative_records=self._negative_records_from_payload(payload),
                )
            return CacheRevivalResult(candidates=list(candidates.values()), scanned=scanned, skipped_recently=skipped_recently)

    def _observe_tier3_fact(
        self,
        records: dict[str, dict[str, Any]],
        negative_records: dict[str, dict[str, Any]],
        fact: Tier3ObservationFact,
        observed_at: str,
    ) -> bool:
        node = fact.node.clone()
        node.tier = "tier3"
        if not is_tier3_auto_pool_node(node):
            return False
        identity = self._node_identity(node, tier="tier3")
        kind = fact.kind

        if kind in {
            Tier3ObservationKind.MIXED_PORT_PASS,
            Tier3ObservationKind.POOL_HEALTH_PASS,
        }:
            if self._status_value(node.status) not in HEALTHY_STATUSES:
                node.status = NodeStatus.ALIVE
            changed = self._observe_node(
                records,
                "tier3",
                node,
                observed_at,
            )
            record = records.get(identity)
            if record is None:
                return changed
            self._mark_authoritative_pass(record, kind, observed_at)
            changed = negative_records.pop(identity, None) is not None or changed
            return True

        record = self._ensure_weak_observation_record(
            records,
            node,
            observed_at,
        )
        if record is None:
            return False
        if kind is Tier3ObservationKind.TCP_RESPONSIVE:
            record["last_tcp_responsive_at"] = observed_at
            record["tcp_responsive_count"] = (
                int(record.get("tcp_responsive_count", 0) or 0) + 1
            )
            if not self._record_has_authoritative_pass(record):
                record["observation_authority"] = "tcp_only"
        elif kind is Tier3ObservationKind.RIGHT_CENSORED:
            record["last_censored_at"] = observed_at
            record["censored_count"] = (
                int(record.get("censored_count", 0) or 0) + 1
            )
            if not self._record_has_authoritative_pass(record):
                record["observation_authority"] = "right_censored"
        else:
            failure_field = (
                "last_tcp_dead_at"
                if kind is Tier3ObservationKind.TCP_DEAD
                else "last_pool_health_fail_at"
                if kind is Tier3ObservationKind.POOL_HEALTH_FAIL
                else "last_deterministic_failure_at"
            )
            count_field = (
                "tcp_dead_count"
                if kind is Tier3ObservationKind.TCP_DEAD
                else "pool_health_fail_count"
                if kind is Tier3ObservationKind.POOL_HEALTH_FAIL
                else "deterministic_validation_fail_count"
            )
            record[failure_field] = observed_at
            record[count_field] = int(record.get(count_field, 0) or 0) + 1
            record["observation_authority"] = kind.value
            if fact.reason and not node.suspicious_reason:
                node.suspicious_reason = str(fact.reason)
            if self._status_value(node.status) not in NEGATIVE_CACHE_STATUSES:
                node.status = NodeStatus.DEAD
            record["status"] = self._status_value(node.status)
            record["node"] = self._stable_node_payload(node)
            failure_stage = kind.value
            if kind is Tier3ObservationKind.TCP_DEAD:
                extra = node.extra if isinstance(node.extra, dict) else {}
                transport_stage = str(extra.get("tier3_failure_stage") or "").strip().lower()
                if (
                    transport_stage.startswith("tcp_")
                    and transport_stage not in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
                ):
                    failure_stage = transport_stage
            self._observe_negative_record(
                negative_records,
                "tier3",
                node,
                observed_at,
                authoritative_stage=failure_stage,
            )
        records[identity] = record
        return True

    def _ensure_weak_observation_record(
        self,
        records: dict[str, dict[str, Any]],
        node: Node,
        observed_at: str,
    ) -> dict[str, Any] | None:
        if not is_tier3_auto_pool_node(node):
            return None
        identity = self._node_identity(node, tier="tier3")
        stored = records.get(identity)
        record = dict(stored) if stored is not None else {
            "identity": identity,
            "tier": "tier3",
            "status": NodeStatus.UNKNOWN.value,
            "first_observed_at": observed_at,
            "status_changed_at": observed_at,
            "observed_count": 0,
            "revive_count": 0,
        }
        record["identity"] = identity
        record["tier"] = "tier3"
        record["last_observed_at"] = observed_at
        record["observed_count"] = int(record.get("observed_count", 0) or 0) + 1
        if stored is None:
            weak_node = node.clone()
            weak_node.status = NodeStatus.UNKNOWN
            weak_node.suspicious_reason = None
            record["node"] = self._stable_node_payload(weak_node)
        records[identity] = record
        return record

    def _mark_authoritative_pass(
        self,
        record: dict[str, Any],
        kind: Tier3ObservationKind,
        observed_at: str,
    ) -> None:
        if kind is Tier3ObservationKind.MIXED_PORT_PASS:
            record["last_mixed_port_pass_at"] = observed_at
            record["mixed_port_pass_count"] = (
                int(record.get("mixed_port_pass_count", 0) or 0) + 1
            )
        else:
            record["last_pool_health_pass_at"] = observed_at
            record["pool_health_pass_count"] = (
                int(record.get("pool_health_pass_count", 0) or 0) + 1
            )
        record["last_healthy_at"] = observed_at
        record["observation_authority"] = kind.value

    def _record_has_authoritative_pass(
        self,
        record: dict[str, Any],
    ) -> bool:
        return self._record_authoritative_pass_at(record) is not None

    def _record_authoritative_pass_at(
        self,
        record: dict[str, Any],
    ) -> datetime | None:
        return self._latest_time(
            record.get("last_mixed_port_pass_at"),
            record.get("last_pool_health_pass_at"),
        )

    def _latest_time(self, *values: Any) -> datetime | None:
        parsed = [
            value
            if isinstance(value, datetime)
            else self._parse_time(str(value or ""))
            for value in values
        ]
        valid = [value for value in parsed if value is not None]
        return max(valid) if valid else None

    def _record_failure_times(
        self,
        record: dict[str, Any],
        negative_record: dict[str, Any] | None,
    ) -> tuple[datetime | None, datetime | None]:
        """Separate validation authority from weak TCP reachability facts."""

        negative = negative_record or {}
        negative_stage = str(negative.get("failure_stage") or "").strip().lower()
        negative_at = self._parse_time(str(negative.get("last_failed_at") or ""))
        negative_tcp_at = (
            negative_at
            if negative_stage.startswith("tcp_")
            and negative_stage not in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
            else None
        )
        negative_deterministic_at = (
            negative_at
            if negative_at is not None
            and bool(negative_stage)
            and not negative_stage.startswith("tcp_")
            and negative_stage not in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
            else None
        )
        deterministic_at = self._latest_time(
            record.get("last_deterministic_failure_at"),
            record.get("last_pool_health_fail_at"),
            negative_deterministic_at,
        )
        tcp_dead_at = self._latest_time(
            record.get("last_tcp_dead_at"),
            negative_tcp_at,
        )
        return deterministic_at, tcp_dead_at

    @staticmethod
    def _record_validation_contract_hash(record: dict[str, Any]) -> str:
        node_payload = record.get("node")
        extra = (
            node_payload.get("extra")
            if isinstance(node_payload, dict)
            else {}
        )
        value = (
            extra.get("tier3_validation_contract_hash")
            if isinstance(extra, dict)
            else ""
        )
        return str(value or "").strip()

    @staticmethod
    def _record_source_authority(record: dict[str, Any], node: Node) -> str:
        extra = node.extra if isinstance(node.extra, dict) else {}
        return str(
            extra.get("tier3_source_root_id")
            or extra.get("source_root_id")
            or extra.get("tier3_source_id")
            or record.get("source_id")
            or ""
        ).strip()

    def _normalize_v3_record(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        record = dict(item)
        node_payload = record.get("node")
        if isinstance(node_payload, dict):
            extra = node_payload.get("extra")
            if isinstance(extra, dict) and PROCESS_LOCAL_SCHEDULER_EXTRA_KEYS.intersection(extra):
                record["node"] = dict(node_payload, extra={
                    key: value for key, value in extra.items()
                    if key not in PROCESS_LOCAL_SCHEDULER_EXTRA_KEYS
                })
        if any(
            record.get(key)
            for key in (
                "last_mixed_port_pass_at",
                "last_pool_health_pass_at",
                "last_tcp_responsive_at",
                "last_censored_at",
                "last_deterministic_failure_at",
                "last_tcp_dead_at",
                "last_pool_health_fail_at",
                "observation_authority",
            )
        ):
            return record
        node_payload = record.get("node")
        extra = (
            node_payload.get("extra")
            if isinstance(node_payload, dict)
            else {}
        )
        extra = extra if isinstance(extra, dict) else {}
        observed_at = str(
            record.get("last_healthy_at")
            or record.get("last_observed_at")
            or record.get("first_observed_at")
            or ""
        )
        if self._legacy_record_has_validated_pass(extra):
            record["last_mixed_port_pass_at"] = observed_at
            record["mixed_port_pass_count"] = max(
                1,
                int(record.get("observed_count", 0) or 0),
            )
            record["observation_authority"] = "migrated_mixed_port_pass"
            failure_field = self._legacy_record_failure_field(record, extra)
            if failure_field:
                failure_at = str(
                    record.get("last_observed_at")
                    or record.get("last_failed_at")
                    or observed_at
                )
                record[failure_field] = failure_at
                record["observation_authority"] = (
                    "migrated_tcp_dead"
                    if failure_field == "last_tcp_dead_at"
                    else "migrated_deterministic_validation_fail"
                )
            return record
        if self._legacy_record_has_tcp_observation(extra):
            record["last_tcp_responsive_at"] = observed_at
            record["tcp_responsive_count"] = max(
                1,
                int(record.get("observed_count", 0) or 0),
            )
            record["observation_authority"] = "tcp_only"
        else:
            record["observation_authority"] = "legacy_unverified"
        record.pop("last_healthy_at", None)
        if str(record.get("status") or "") in HEALTHY_STATUSES:
            record["status"] = NodeStatus.UNKNOWN.value
        if isinstance(node_payload, dict):
            migrated_node = dict(node_payload)
            if str(migrated_node.get("status") or "") in HEALTHY_STATUSES:
                migrated_node["status"] = NodeStatus.UNKNOWN.value
            record["node"] = migrated_node
        return record

    def _legacy_record_has_validated_pass(
        self,
        extra: dict[str, Any],
    ) -> bool:
        purpose = str(extra.get("tier3_validation_purpose") or "").lower()
        clean = bool(extra.get("tier3_validator_clean_confirmed"))
        admission = bool(
            extra.get("tier3_admission_commit_attempted")
            or extra.get("tier3_admission_commit_enqueued")
            or extra.get("tier3_admission_committed")
        )
        validator = str(extra.get("validated_by") or "").lower()
        profile = str(extra.get("validation_profile") or "").lower()
        trusted_validator = bool(
            validator in {
                "tier3_validator_helper",
                "mihomo-batch",
                "mihomo_batch",
            }
            or profile in {
                "endpoint_process_helper",
                "session_batch",
            }
        )
        return bool(
            purpose == "auto_admission"
            and clean
            and admission
            and trusted_validator
        )

    def _legacy_record_has_tcp_observation(
        self,
        extra: dict[str, Any],
    ) -> bool:
        return bool(
            extra.get("tier3_tcp_prefilter_observed")
            or extra.get("tier3_tcp_prefilter_responsive")
        )

    def _legacy_record_failure_field(
        self,
        record: dict[str, Any],
        extra: dict[str, Any],
    ) -> str:
        """Recover a later deterministic failure from a schema-v2 record.

        Old cache rows stored a single mutable Node. A later right-censored or
        facility result could change its display status without revoking an
        earlier PASS, while a real TCP/validation failure must revoke it. The
        stage is therefore authoritative here; status is only the conservative
        fallback when the old row lacks a stage.
        """

        stage = str(extra.get("tier3_failure_stage") or "").strip().lower()
        if stage in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES:
            return ""
        status = str(record.get("status") or "").strip().lower()
        if stage.startswith("tcp_"):
            return "last_tcp_dead_at"
        if stage in {
            "unsupported_proxy",
            "mihomo_direct_failed",
            "gateway_validation_failed",
            "validation_failed",
            "body_mismatch",
            "http_status_mismatch",
        }:
            return "last_deterministic_failure_at"
        if status == NodeStatus.DEAD.value:
            return "last_deterministic_failure_at"
        return ""

    def _observe_node(
        self,
        records: dict[str, dict[str, Any]],
        tier: str,
        node: Node,
        observed_at: str,
    ) -> bool:
        effective_tier = str(tier or node.tier or "").strip().lower()
        if effective_tier != "tier3":
            return False
        if not is_tier3_auto_pool_node(node):
            return False
        identity = self._node_identity(node, tier=effective_tier)
        status = self._status_value(node.status)
        node_payload = self._stable_node_payload(node)
        node_payload["tier"] = effective_tier
        stored_existing = records.get(identity)
        existing = dict(stored_existing) if stored_existing is not None else None
        evicted_from_pool = self._node_marked_evicted(node) or bool(existing and existing.get("evicted_from_pool"))
        if existing is None and effective_tier == "tier3" and self._first_validation_failure(status) and not evicted_from_pool:
            return False
        previous_status = str(existing.get("status") or "") if existing else ""
        dead_streak = 0
        if status == NodeStatus.DEAD.value:
            dead_streak = int(existing.get("dead_streak", 0) or 0) + 1 if existing and previous_status == status else 1
            if dead_streak >= self.dead_evict_threshold and not evicted_from_pool:
                return records.pop(identity, None) is not None

        if existing is None:
            existing = {
                "identity": identity,
                "first_observed_at": observed_at,
                "status_changed_at": observed_at,
                "observed_count": 0,
                "revive_count": 0,
            }
        elif previous_status != status:
            existing["status_changed_at"] = observed_at

        existing.update(
            {
                "identity": identity,
                "tier": effective_tier,
                "status": status,
                "latency_ms": node.latency_ms,
                "last_observed_at": observed_at,
                "dead_streak": dead_streak if status == NodeStatus.DEAD.value else 0,
                "node": node_payload,
            }
        )
        existing["observed_count"] = int(existing.get("observed_count", 0) or 0) + 1
        if status in HEALTHY_STATUSES:
            existing["last_healthy_at"] = observed_at
            existing.pop("evicted_from_pool", None)
            existing.pop("evicted_from_pool_at", None)
            existing.pop("eviction_failure_count", None)
        elif evicted_from_pool:
            existing["evicted_from_pool"] = True
            existing["evicted_from_pool_at"] = (
                node.extra.get(EVICTED_FROM_POOL_AT_MARKER)
                if isinstance(node.extra, dict)
                else None
            ) or existing.get("evicted_from_pool_at") or observed_at
            existing["eviction_failure_count"] = (
                node.extra.get(EVICTION_FAILURE_COUNT_MARKER)
                if isinstance(node.extra, dict)
                else None
            ) or existing.get("eviction_failure_count") or existing.get("failure_count") or 0
        if node.extra.get("revived_from_cache") and not existing.get("last_revived_at"):
            existing["last_revived_at"] = observed_at
        records[identity] = existing
        return True

    def _first_validation_failure(self, status: str) -> bool:
        return status in {NodeStatus.DEAD.value, NodeStatus.SUSPICIOUS.value}

    def _observe_negative_record(
        self,
        records: dict[str, dict[str, Any]],
        tier: str,
        node: Node,
        observed_at: str,
        *,
        authoritative_stage: str | None = None,
    ) -> bool:
        if (tier or node.tier) != "tier3":
            return False
        if not is_tier3_auto_pool_node(node):
            return False
        identity = self._node_identity(node, tier="tier3")
        status = self._status_value(node.status)
        if status in HEALTHY_STATUSES:
            return records.pop(identity, None) is not None
        reason = str(node.suspicious_reason or "")
        cacheable_suspicious = reason.startswith((
            "mihomo_validation_failed:",
            "mihomo_batch_validation_failed:",
            "all_gateways_unavailable:",
        ))
        if status not in NEGATIVE_CACHE_STATUSES or (
            status == NodeStatus.SUSPICIOUS.value
            and not cacheable_suspicious
            and not authoritative_stage
        ):
            return False
        existing = records.get(identity, {})
        extra = node.extra if isinstance(node.extra, dict) else {}
        failure_stage = str(authoritative_stage or extra.get("tier3_failure_stage") or "")
        if not failure_stage:
            if reason.startswith("mihomo_batch_validation_failed:"):
                lowered = reason.lower()
                if "http error 503" in lowered:
                    failure_stage = "mihomo_delay_503"
                elif "http error 504" in lowered:
                    failure_stage = "mihomo_delay_504"
                else:
                    failure_stage = "mihomo_delay_failed"
            elif reason.startswith("mihomo_validation_failed:"):
                failure_stage = "unsupported_proxy" if "unsupported proxy" in reason.lower() else "mihomo_direct_failed"
            elif status == NodeStatus.DEAD.value:
                failure_stage = "tcp_dead"
        if failure_stage in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES:
            return False
        if (
            failure_stage.startswith("tcp_")
            and existing
            and not self._transport_only_failure(existing)
        ):
            return False
        source_type = str(extra.get("source_type") or extra.get("tier3_failure_source_type") or "")
        source_value = str(node.source or extra.get("source") or "")
        source_id = str(extra.get("tier3_failure_source_id") or "")
        if not source_id and source_value:
            source_id = f"{source_type}:{source_value}" if source_type else source_value
        records[identity] = {
            "identity": identity,
            "tier": "tier3",
            "status": status,
            "last_failed_at": observed_at,
            "failure_count": int(existing.get("failure_count", 0) or 0) + 1,
            "reason": reason,
            "failure_stage": failure_stage,
            "entry_cooldown_authority": (
                "transport_only" if failure_stage.startswith("tcp_") else "validation"
            ),
            "failure_reason": str(extra.get("tier3_failure_reason") or reason),
            "source_id": source_id,
            "source_type": source_type,
            "lane": str(extra.get("tier3_outside_lane") or extra.get("tier3_failure_lane") or ""),
            "protocol": node.protocol,
            "port": int(node.port or 0),
        }
        return True

    def _stable_node_payload(self, node: Node) -> dict[str, Any]:
        payload = node.to_dict()
        extra = payload.get("extra")
        if not isinstance(extra, dict):
            return payload
        for key in NON_PERSISTENT_OBSERVATION_EXTRA_KEYS:
            extra.pop(key, None)
        return payload

    def _empty_payload(self) -> dict[str, Any]:
        return {"schema_version": 3, "generated_at": utc_now_iso(), "nodes": [], "negative": []}

    def _write_records(
        self,
        records: dict[str, dict[str, Any]],
        *,
        generated_at: str,
        negative_records: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        pruned = self._prune_records(records)
        payload = {
            "schema_version": 3,
            "generated_at": generated_at,
            "nodes": self._records_by_recent_observation(pruned.values(), reverse=False),
            "negative": self._records_by_recent_failure(
                self._prune_negative_records(negative_records or {}).values(),
                reverse=False,
            ),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                _write_observation_payload(handle, payload)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp_path, self.path)
            self._publish_cached_payload(payload)
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _records_from_payload(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        nodes = payload.get("nodes")
        if isinstance(nodes, list):
            for item in nodes:
                if not isinstance(item, dict):
                    continue
                if not self._record_is_tier3(item):
                    continue
                identity = str(item.get("identity") or "")
                if not identity:
                    continue
                records[identity] = self._normalize_v3_record(item)

        observations = payload.get("observations")
        if isinstance(observations, list):
            for item in observations:
                if not isinstance(item, dict):
                    continue
                node_payload = item.get("node")
                if not isinstance(node_payload, dict):
                    continue
                try:
                    node = Node.from_dict(node_payload)
                except Exception:
                    continue
                observed_at = str(item.get("observed_at") or item.get("last_observed_at") or utc_now_iso())
                node.status = self._node_status(item.get("status") or node.status)
                node.latency_ms = item.get("latency_ms", node.latency_ms)
                self._observe_node(records, str(item.get("tier") or node.tier), node, observed_at)
        records = {
            identity: self._normalize_v3_record(item)
            for identity, item in records.items()
        }
        return self._prune_records(records)

    def _negative_records_from_payload(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        negative = payload.get("negative")
        if isinstance(negative, list):
            for item in negative:
                if not isinstance(item, dict):
                    continue
                if not self._record_is_tier3(item):
                    continue
                identity = str(item.get("identity") or "")
                failure_stage = str(item.get("failure_stage") or "")
                reason = str(item.get("reason") or item.get("failure_reason") or "")
                reason_lower = reason.lower()
                if (
                    failure_stage in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
                    or (
                        reason.startswith("mihomo_batch_validation_failed:")
                        and any(f"http error {code}" in reason_lower for code in range(500, 505))
                    )
                ):
                    continue
                if identity:
                    normalized = dict(item)
                    normalized["entry_cooldown_authority"] = (
                        "transport_only" if self._transport_only_failure(item) else "validation"
                    )
                    records[identity] = normalized
        return records

    def _record_is_tier3(self, item: dict[str, Any]) -> bool:
        tier = str(item.get("tier") or "")
        identity = str(item.get("identity") or "")
        return tier == "tier3" or (not tier and identity.startswith("tier3|"))

    def _record_marked_evicted(self, item: dict[str, Any]) -> bool:
        if item.get("evicted_from_pool"):
            return True
        node_payload = item.get("node")
        if not isinstance(node_payload, dict):
            return False
        extra = node_payload.get("extra")
        return bool(isinstance(extra, dict) and extra.get(EVICTED_FROM_POOL_MARKER))

    def _node_marked_evicted(self, node: Node) -> bool:
        extra = getattr(node, "extra", {})
        return bool(isinstance(extra, dict) and extra.get(EVICTED_FROM_POOL_MARKER))

    def _prune_records(self, records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        kept = self._records_by_retention(records.values())[: self.history_limit]
        return {str(item.get("identity")): item for item in kept if item.get("identity")}

    def _prune_negative_records(self, records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        # A large TCP sweep must not evict the smaller set of authoritative
        # validation failures and thereby accidentally re-enable those nodes.
        ordered = self._records_by_recent_failure(records.values())
        ordered.sort(key=self._transport_only_failure)
        kept = ordered[: self.history_limit]
        return {str(item.get("identity")): item for item in kept if item.get("identity")}

    @staticmethod
    def _transport_only_failure(record: dict[str, Any]) -> bool:
        return str(record.get("failure_stage") or "").strip().lower().startswith("tcp_")

    def _records_by_retention(self, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(records, key=self._retention_key, reverse=True)

    def _records_by_recent_observation(self, records: Iterable[dict[str, Any]], *, reverse: bool = True) -> list[dict[str, Any]]:
        return sorted(records, key=lambda item: self._time_key(str(item.get("last_observed_at") or item.get("last_healthy_at") or "")), reverse=reverse)

    def _records_by_recent_failure(self, records: Iterable[dict[str, Any]], *, reverse: bool = True) -> list[dict[str, Any]]:
        return sorted(records, key=lambda item: self._time_key(str(item.get("last_failed_at") or "")), reverse=reverse)

    def _retention_key(self, item: dict[str, Any]) -> tuple[int, float]:
        authoritative_pass_at = self._record_authoritative_pass_at(item)
        if authoritative_pass_at is not None:
            priority = 3
        elif item.get("last_tcp_responsive_at"):
            priority = 2
        elif item.get("last_censored_at"):
            priority = 1
        else:
            priority = 0
        authoritative_time = (
            authoritative_pass_at.timestamp()
            if authoritative_pass_at is not None
            else 0.0
        )
        return (
            priority,
            max(
                authoritative_time,
                self._time_key(str(item.get("last_observed_at") or "")),
            ),
        )

    def _time_key(self, value: str) -> float:
        parsed = self._parse_time(value)
        return parsed.timestamp() if parsed else 0.0

    def _node_identity(self, node: Node, *, tier: str | None = None) -> str:
        return "|".join([str(tier or node.tier), *node.dedupe_key])

    def _status_value(self, status: NodeStatus | str) -> str:
        if isinstance(status, NodeStatus):
            return status.value
        return str(status or NodeStatus.UNKNOWN.value)

    def _node_status(self, status: Any) -> NodeStatus:
        try:
            return NodeStatus(str(status))
        except ValueError:
            return NodeStatus.UNKNOWN

    def _parse_time(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
