from __future__ import annotations

import threading
import time
import math
import hashlib
import heapq
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from models import Node
from tier3_defaults import (
    DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY,
    tier3_tcp_breadth_per_identity,
    tier3_tcp_breadth_scaled_limit,
)


class Tier3ServiceCohortEligibility(str, Enum):
    """Explicit separation between discovery evidence and production use."""

    OBSERVE = "observe"
    PARENT_DISCOVERY = "parent_discovery"
    LEAF_CONFIRMATION = "leaf_confirmation"
    PARENT_PROBATION = "parent_probation"
    PARENT_PROVEN = "parent_proven"
    LEAF_PROBATION = "leaf_probation"
    PROVEN_50 = "proven_50"
    PROVEN_75 = "proven_75"
    DEMOTED = "demoted"


class Tier3SchedulingOutcome(str, Enum):
    """Typed end-to-end outcome used only by the pre-TCP scheduler."""

    TCP_DEAD = "tcp_dead"
    TCP_CENSORED = "tcp_censored"
    VALIDATION_FAIL = "validation_fail"
    VALIDATION_CENSORED = "validation_censored"
    PASS_PENDING_COMMIT = "pass_pending_commit"
    NET_NEW = "net_new"
    NEUTRAL_SKIP = "neutral_skip"
    CANCELLED = "cancelled"
    FACILITY = "facility"


class Tier3CandidateEvidenceOrigin(str, Enum):
    """Immutable evidence authority for one candidate transaction."""

    FRESH_REPLAY = "fresh_replay"
    HISTORICAL_REPROBE = "historical_reprobe"
    COLD_UNKNOWN = "cold_unknown"
    COLD_DISCOVERY = "cold_discovery"


@dataclass(frozen=True, slots=True)
class Tier3CandidateFeatureKey:
    """Source-independent features used only before TCP handoff."""

    service_family: str
    transport: str
    security: str
    port_bucket: str
    address_bucket: str
    host_suffix: str
    service_profile: str = "none"
    auth_mode: str = "none"
    encryption: str = "none"
    flow: str = "none"
    fingerprint: str = "none"
    path_shape: str = "none"
    service_profiles: tuple[str, ...] = ()

    @staticmethod
    def _normalized(value: Any, fallback: str = "unknown") -> str:
        text = str(value or "").strip().lower()
        return text or fallback

    @classmethod
    def build(
        cls,
        *,
        service_family: Any,
        transport: Any,
        security: Any,
        port_bucket: Any,
        address_bucket: Any,
        host_suffix: Any,
        service_profile: Any = "none",
        auth_mode: Any = "none",
        encryption: Any = "none",
        flow: Any = "none",
        fingerprint: Any = "none",
        path_shape: Any = "none",
        service_profiles: Any = (),
    ) -> "Tier3CandidateFeatureKey":
        normalized_profiles: list[str] = []
        raw_profiles = (
            service_profiles
            if isinstance(service_profiles, (list, tuple))
            else ()
        )
        for value in raw_profiles:
            profile = cls._normalized(value, "none")
            if cls._meaningful(profile) and profile not in normalized_profiles:
                normalized_profiles.append(profile)
        normalized_service_profile = cls._normalized(service_profile, "none")
        if (
            cls._meaningful(normalized_service_profile)
            and normalized_service_profile not in normalized_profiles
        ):
            normalized_profiles.append(normalized_service_profile)
        return cls(
            service_family=cls._normalized(service_family, "other"),
            transport=cls._normalized(transport),
            security=cls._normalized(security),
            port_bucket=cls._normalized(port_bucket),
            address_bucket=cls._normalized(address_bucket),
            host_suffix=cls._normalized(host_suffix, "none"),
            service_profile=(
                normalized_profiles[-1]
                if normalized_profiles
                else normalized_service_profile
            ),
            auth_mode=cls._normalized(auth_mode, "none"),
            encryption=cls._normalized(encryption, "none"),
            flow=cls._normalized(flow, "none"),
            fingerprint=cls._normalized(fingerprint, "none"),
            path_shape=cls._normalized(path_shape, "none"),
            service_profiles=tuple(normalized_profiles),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_family": self.service_family,
            "transport": self.transport,
            "security": self.security,
            "port_bucket": self.port_bucket,
            "address_bucket": self.address_bucket,
            "host_suffix": self.host_suffix,
            "service_profile": self.service_profile,
            "auth_mode": self.auth_mode,
            "encryption": self.encryption,
            "flow": self.flow,
            "fingerprint": self.fingerprint,
            "path_shape": self.path_shape,
            "service_profiles": list(self.service_profiles),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        fallback_family: str = "other",
    ) -> "Tier3CandidateFeatureKey":
        payload = value if isinstance(value, Mapping) else {}
        return cls.build(
            service_family=payload.get("service_family") or fallback_family,
            transport=payload.get("transport"),
            security=payload.get("security"),
            port_bucket=payload.get("port_bucket"),
            address_bucket=payload.get("address_bucket"),
            host_suffix=payload.get("host_suffix"),
            service_profile=payload.get("service_profile"),
            auth_mode=payload.get("auth_mode"),
            encryption=payload.get("encryption"),
            flow=payload.get("flow"),
            fingerprint=payload.get("fingerprint"),
            path_shape=payload.get("path_shape"),
            service_profiles=payload.get("service_profiles"),
        )

    def ordered_service_profiles(self) -> tuple[str, ...]:
        if self.service_profiles:
            return self.service_profiles
        if self._meaningful(self.service_profile):
            return (self.service_profile,)
        return ()

    def has_same_service_profile(self, other: "Tier3CandidateFeatureKey") -> bool:
        return bool(
            self._meaningful(self.service_profile)
            and self.service_profile == other.service_profile
        )

    @staticmethod
    def _meaningful(value: str) -> bool:
        return value not in {"", "unknown", "none", "default", "aggregate"}

    def is_similar_to(self, other: "Tier3CandidateFeatureKey") -> bool:
        if (
            self.service_family != other.service_family
            or self.transport != other.transport
            or self.security != other.security
        ):
            return False
        comparable = 0
        matches = 0
        for left, right in (
            (self.port_bucket, other.port_bucket),
            (self.address_bucket, other.address_bucket),
            (self.host_suffix, other.host_suffix),
        ):
            if not (self._meaningful(left) and self._meaningful(right)):
                continue
            comparable += 1
            if left == right:
                matches += 1
        return comparable >= 2 and matches >= 2


@dataclass(slots=True)
class ScheduledTier3Candidate:
    node: Node
    report: Any
    arm_key: str
    source_root: str
    family: str
    coarse_key: str
    feature_key: Tier3CandidateFeatureKey
    selection_lane: str = "coverage"
    dispatch_ticket: "Tier3CandidateFeedbackTicket | None" = None
    lookahead_sequence: int = 0
    visibility_class: str = "unknown"


@dataclass(frozen=True, slots=True)
class Tier3CandidateLookaheadEvidence:
    """Immutable pre-TCP evidence used by the production lookahead."""

    validated_positive: bool = False
    validated_reprobe: bool = False
    tcp_only: bool = False
    observed_nonpositive: bool = False
    # One-schema compatibility aliases. New owners must use the typed fields.
    stable_tcp_positive: bool = False
    observed: bool = False
    pre_tcp_score: float = 0.0


@dataclass(frozen=True, slots=True)
class Tier3CandidateLookaheadSnapshot:
    indexed_count: int
    eligible_count: int
    examined_total: int
    hit_total: int
    validated_positive_count: int = 0
    validated_reprobe_count: int = 0
    tcp_only_count: int = 0
    unknown_count: int = 0
    observed_nonpositive_count: int = 0
    cursor_wrap_count: int = 0
    max_examined_per_reservation: int = 0

    @property
    def exact_positive_count(self) -> int:
        return self.validated_positive_count


@dataclass(frozen=True, slots=True)
class _Tier3CandidateLookaheadEntry:
    sequence: int
    arm_id: str
    candidate: ScheduledTier3Candidate
    evidence: Tier3CandidateLookaheadEvidence
    visibility_class: str


class Tier3CandidateVisibilityIndex:
    """Bounded, side-effect-free visibility over candidates before TCP.

    Candidates are classified once from the expansion's immutable observation
    snapshot.  Each class owns persistent root/family/coarse cursors, so
    selection is O(handoff budget) instead of sorting the full inventory.
    """

    VALIDATED_POSITIVE = "validated_positive"
    EXACT_POSITIVE = VALIDATED_POSITIVE
    VALIDATED_REPROBE = "validated_reprobe"
    UNKNOWN = "unknown"
    TCP_ONLY = "tcp_only"
    OBSERVED_NONPOSITIVE = "observed_nonpositive"
    CLASSES = (
        VALIDATED_POSITIVE,
        VALIDATED_REPROBE,
        UNKNOWN,
        TCP_ONLY,
        OBSERVED_NONPOSITIVE,
    )

    def __init__(self) -> None:
        self._entries: dict[int, _Tier3CandidateLookaheadEntry] = {}
        self._next_sequence = 0
        self._examined_total = 0
        self._hit_total = 0
        self._cursor_wrap_count = 0
        self._max_examined_per_reservation = 0
        self._class_counts: dict[str, int] = {
            lane: 0 for lane in self.CLASSES
        }
        self._root_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._family_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self._arm_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._root_orders: dict[str, OrderedDict[str, None]] = {
            lane: OrderedDict() for lane in self.CLASSES
        }
        self._family_orders: dict[
            tuple[str, str], OrderedDict[str, None]
        ] = {}
        self._arm_orders: dict[
            tuple[str, str, str], OrderedDict[str, None]
        ] = {}
        self._arm_heaps: dict[
            tuple[str, str], list[tuple[int, int]]
        ] = {}
        self._returned_heaps: dict[
            tuple[str, str, str], list[tuple[int, int]]
        ] = {}

    @classmethod
    def classification_for(
        cls,
        evidence: Tier3CandidateLookaheadEvidence,
    ) -> str:
        if bool(evidence.validated_positive or evidence.stable_tcp_positive):
            return cls.VALIDATED_POSITIVE
        if bool(evidence.validated_reprobe):
            return cls.VALIDATED_REPROBE
        if bool(evidence.tcp_only):
            return cls.TCP_ONLY
        if bool(evidence.observed_nonpositive or evidence.observed):
            return cls.OBSERVED_NONPOSITIVE
        return cls.UNKNOWN

    @staticmethod
    def _bucket(
        entry: _Tier3CandidateLookaheadEntry,
    ) -> tuple[str, str]:
        return (
            str(entry.candidate.source_root),
            str(entry.candidate.family),
        )

    def _index_entry(self, entry: _Tier3CandidateLookaheadEntry) -> None:
        lane = entry.visibility_class
        root, family = self._bucket(entry)
        arm_id = entry.arm_id
        root_key = (lane, root)
        family_key = (lane, root, family)
        arm_key = (lane, arm_id)
        if self._root_counts[root_key] <= 0:
            self._root_orders[lane][root] = None
        if self._family_counts[family_key] <= 0:
            self._family_orders.setdefault(root_key, OrderedDict())[family] = None
        if self._arm_counts[arm_key] <= 0:
            self._arm_orders.setdefault(family_key, OrderedDict())[arm_id] = None
        self._root_counts[root_key] += 1
        self._family_counts[family_key] += 1
        self._arm_counts[arm_key] += 1
        self._class_counts[lane] += 1
        heapq.heappush(
            self._arm_heaps.setdefault(arm_key, []),
            (int(entry.sequence), id(entry.candidate)),
        )

    def _unindex_entry(self, entry: _Tier3CandidateLookaheadEntry) -> None:
        lane = entry.visibility_class
        root, family = self._bucket(entry)
        arm_id = entry.arm_id
        root_key = (lane, root)
        family_key = (lane, root, family)
        arm_key = (lane, arm_id)
        self._class_counts[lane] = max(0, self._class_counts[lane] - 1)
        self._arm_counts[arm_key] = max(0, self._arm_counts[arm_key] - 1)
        self._family_counts[family_key] = max(
            0,
            self._family_counts[family_key] - 1,
        )
        self._root_counts[root_key] = max(0, self._root_counts[root_key] - 1)
        if self._arm_counts[arm_key] <= 0:
            self._arm_counts.pop(arm_key, None)
            order = self._arm_orders.get(family_key)
            if order is not None:
                order.pop(arm_id, None)
                if not order:
                    self._arm_orders.pop(family_key, None)
            self._arm_heaps.pop(arm_key, None)
        if self._family_counts[family_key] <= 0:
            self._family_counts.pop(family_key, None)
            self._returned_heaps.pop(family_key, None)
            order = self._family_orders.get(root_key)
            if order is not None:
                order.pop(family, None)
                if not order:
                    self._family_orders.pop(root_key, None)
        if self._root_counts[root_key] <= 0:
            self._root_counts.pop(root_key, None)
            self._root_orders[lane].pop(root, None)

    def _peek_arm(
        self,
        lane: str,
        arm_id: str,
    ) -> _Tier3CandidateLookaheadEntry | None:
        heap = self._arm_heaps.get((lane, arm_id))
        while heap:
            _sequence, candidate_id = heap[0]
            entry = self._entries.get(candidate_id)
            if (
                entry is not None
                and entry.arm_id == arm_id
                and entry.visibility_class == lane
            ):
                return entry
            heapq.heappop(heap)
        return None

    def _take_eligible_from_arm(
        self,
        lane: str,
        arm_id: str,
        *,
        eligibility: Callable[[_Tier3CandidateLookaheadEntry], bool] | None,
        examination_limit: int,
    ) -> tuple[_Tier3CandidateLookaheadEntry | None, int]:
        """Take the oldest eligible entry without disturbing skipped entries."""

        heap = self._arm_heaps.get((lane, arm_id))
        if not heap:
            return None, 0
        held: list[tuple[int, int]] = []
        examined = 0
        limit = max(1, int(examination_limit))
        selected: _Tier3CandidateLookaheadEntry | None = None
        while heap and examined < limit:
            sequence, candidate_id = heapq.heappop(heap)
            entry = self._entries.get(candidate_id)
            if (
                entry is None
                or entry.arm_id != arm_id
                or entry.visibility_class != lane
            ):
                continue
            examined += 1
            if eligibility is None or eligibility(entry):
                selected = entry
                break
            held.append((sequence, candidate_id))
        for item in held:
            heapq.heappush(heap, item)
        return selected, examined

    def _take_from_bucket(
        self,
        lane: str,
        bucket: tuple[str, str],
        *,
        excluded_arm_ids: set[str],
        eligibility: Callable[[_Tier3CandidateLookaheadEntry], bool] | None = None,
        examination_limit: int = 64,
    ) -> tuple[_Tier3CandidateLookaheadEntry | None, int]:
        root, family = bucket
        family_key = (lane, root, family)
        arms = self._arm_orders.get(family_key)
        if not arms:
            return None, 0
        examined = 0
        # Pre-start cancellation must not send a candidate behind every other
        # coarse arm. Restore FIFO within its existing root/family turn.
        returned = self._returned_heaps.get(family_key)
        held: list[tuple[int, int]] = []
        while returned and examined < max(1, int(examination_limit)):
            sequence, candidate_id = heapq.heappop(returned)
            entry = self._entries.get(candidate_id)
            if (
                entry is None
                or entry.visibility_class != lane
                or self._bucket(entry) != bucket
            ):
                continue
            examined += 1
            if eligibility is not None and not eligibility(entry):
                held.append((sequence, candidate_id))
                continue
            for item in held:
                heapq.heappush(returned, item)
            self._entries.pop(candidate_id, None)
            self._unindex_entry(entry)
            return entry, examined
        for item in held:
            heapq.heappush(returned, item)
        if not returned:
            self._returned_heaps.pop(family_key, None)
        examined_arm_ids: set[str] = set()
        for allow_repeat in (False, True):
            attempts = len(arms)
            for _index in range(attempts):
                if examined >= max(1, int(examination_limit)):
                    return None, examined
                arm_id = next(iter(arms))
                arms.move_to_end(arm_id)
                if not allow_repeat and arm_id in excluded_arm_ids:
                    continue
                if arm_id in examined_arm_ids:
                    continue
                examined_arm_ids.add(arm_id)
                entry, candidate_examined = self._take_eligible_from_arm(
                    lane,
                    arm_id,
                    eligibility=eligibility,
                    examination_limit=max(
                        1,
                        int(examination_limit) - examined,
                    ),
                )
                examined += candidate_examined
                if entry is None:
                    continue
                self._entries.pop(id(entry.candidate), None)
                self._unindex_entry(entry)
                return entry, examined
        return None, examined

    def take(
        self,
        lane: str,
        *,
        required_bucket: tuple[str, str] | None = None,
        excluded_arm_ids: set[str] | None = None,
        eligibility: Callable[[_Tier3CandidateLookaheadEntry], bool] | None = None,
        examination_limit: int = 64,
    ) -> tuple[_Tier3CandidateLookaheadEntry | None, int]:
        if lane not in self.CLASSES or self._class_counts.get(lane, 0) <= 0:
            return None, 0
        excluded = set(excluded_arm_ids or set())
        if required_bucket is not None:
            return self._take_from_bucket(
                lane,
                required_bucket,
                excluded_arm_ids=excluded,
                eligibility=eligibility,
                examination_limit=examination_limit,
            )
        roots = self._root_orders[lane]
        root_attempts = len(roots)
        examined_total = 0
        for root_index in range(root_attempts):
            if examined_total >= max(1, int(examination_limit)):
                break
            root = next(iter(roots))
            roots.move_to_end(root)
            if root_index == root_attempts - 1 and root_attempts > 1:
                self._cursor_wrap_count += 1
            families = self._family_orders.get((lane, root))
            if not families:
                continue
            family_attempts = len(families)
            for _family_index in range(family_attempts):
                family = next(iter(families))
                families.move_to_end(family)
                entry, examined = self._take_from_bucket(
                    lane,
                    (root, family),
                    excluded_arm_ids=excluded,
                    eligibility=eligibility,
                    examination_limit=max(
                        1,
                        int(examination_limit) - examined_total,
                    ),
                )
                examined_total += examined
                if entry is not None:
                    return entry, examined_total
        return None, examined_total

    def add(
        self,
        candidate: ScheduledTier3Candidate,
        *,
        arm_id: str,
        evidence: Tier3CandidateLookaheadEvidence,
        returned: bool = False,
    ) -> None:
        key = id(candidate)
        if key in self._entries:
            return
        sequence = max(0, int(candidate.lookahead_sequence))
        if sequence <= 0:
            self._next_sequence += 1
            sequence = self._next_sequence
            candidate.lookahead_sequence = sequence
        else:
            self._next_sequence = max(self._next_sequence, sequence)
        visibility_class = self.classification_for(evidence)
        candidate.visibility_class = visibility_class
        entry = _Tier3CandidateLookaheadEntry(
            sequence=sequence,
            arm_id=str(arm_id),
            candidate=candidate,
            evidence=evidence,
            visibility_class=visibility_class,
        )
        self._entries[key] = entry
        self._index_entry(entry)
        if returned:
            root, family = self._bucket(entry)
            heapq.heappush(
                self._returned_heaps.setdefault((visibility_class, root, family), []),
                (sequence, key),
            )

    def remove(
        self,
        candidate: ScheduledTier3Candidate,
    ) -> _Tier3CandidateLookaheadEntry | None:
        entry = self._entries.pop(id(candidate), None)
        if entry is not None:
            self._unindex_entry(entry)
        return entry

    def update_evidence(
        self,
        candidate: ScheduledTier3Candidate,
        evidence: Tier3CandidateLookaheadEvidence,
    ) -> None:
        key = id(candidate)
        entry = self._entries.get(key)
        if entry is None:
            return
        self._entries.pop(key, None)
        self._unindex_entry(entry)
        replacement = _Tier3CandidateLookaheadEntry(
            sequence=entry.sequence,
            arm_id=entry.arm_id,
            candidate=entry.candidate,
            evidence=evidence,
            visibility_class=self.classification_for(evidence),
        )
        replacement.candidate.visibility_class = replacement.visibility_class
        self._entries[key] = replacement
        self._index_entry(replacement)

    def entries(self) -> tuple[_Tier3CandidateLookaheadEntry, ...]:
        return tuple(self._entries.values())

    def count(self, lane: str) -> int:
        return max(0, int(self._class_counts.get(str(lane or ""), 0)))

    def record_examined(self, count: int) -> None:
        self._examined_total += max(0, int(count))

    def record_reservation_examined(self, count: int) -> None:
        value = max(0, int(count))
        self.record_examined(value)
        self._max_examined_per_reservation = max(
            self._max_examined_per_reservation,
            value,
        )

    def record_hit(self, count: int = 1) -> None:
        self._hit_total += max(0, int(count))

    def clear(self) -> None:
        self._entries.clear()
        self._class_counts = {lane: 0 for lane in self.CLASSES}
        self._root_counts.clear()
        self._family_counts.clear()
        self._arm_counts.clear()
        self._root_orders = {
            lane: OrderedDict() for lane in self.CLASSES
        }
        self._family_orders.clear()
        self._arm_orders.clear()
        self._arm_heaps.clear()
        self._returned_heaps.clear()

    def snapshot(
        self,
        *,
        eligible_count: int | None = None,
    ) -> Tier3CandidateLookaheadSnapshot:
        indexed = len(self._entries)
        return Tier3CandidateLookaheadSnapshot(
            indexed_count=indexed,
            eligible_count=(
                indexed
                if eligible_count is None
                else max(0, int(eligible_count))
            ),
            examined_total=max(0, int(self._examined_total)),
            hit_total=max(0, int(self._hit_total)),
            validated_positive_count=max(
                0,
                int(self._class_counts[self.VALIDATED_POSITIVE]),
            ),
            validated_reprobe_count=max(
                0,
                int(self._class_counts[self.VALIDATED_REPROBE]),
            ),
            tcp_only_count=max(0, int(self._class_counts[self.TCP_ONLY])),
            unknown_count=max(0, int(self._class_counts[self.UNKNOWN])),
            observed_nonpositive_count=max(
                0,
                int(self._class_counts[self.OBSERVED_NONPOSITIVE]),
            ),
            cursor_wrap_count=max(0, int(self._cursor_wrap_count)),
            max_examined_per_reservation=max(
                0,
                int(self._max_examined_per_reservation),
            ),
        )


# One report schema keeps the old type name importable. Production behavior is
# the persistent visibility cursor above, not the former global sort.
Tier3CandidateLookaheadIndex = Tier3CandidateVisibilityIndex


@dataclass(frozen=True, slots=True)
class Tier3CandidateFeedbackTicket:
    """Immutable feedback identity; it never represents capacity ownership."""

    scheduler_generation: int
    ticket_id: int
    arm_key: str
    source_root: str
    family: str
    coarse_key: str
    selection_reason: str
    dispatched_monotonic_ns: int
    selection_lane: str = ""
    visibility_class: str = "unknown"
    evidence_origin: str = Tier3CandidateEvidenceOrigin.COLD_UNKNOWN.value
    coverage_selected: bool = False
    unknown_exploration: bool = False
    control_revision: int = 0
    reward_grant_id: int = 0
    exact_grant_lease_id: int = 0
    service_cohort_flight_id: int = 0
    similar_cohort_flight_id: int = 0
    reward_scope: str = "none"
    reward_cohort_key: str = ""
    reward_probation: bool = False
    reward_cohort_state: str = ""
    exact_cohort_key: str = ""
    service_cohort_key: str = ""
    service_cohort_keys: tuple[str, ...] = ()
    similar_cohort_key: str = ""
    feature_key: Tier3CandidateFeatureKey = field(
        default_factory=lambda: Tier3CandidateFeatureKey.build(
            service_family="other",
            transport="unknown",
            security="unknown",
            port_bucket="unknown",
            address_bucket="unknown",
            host_suffix="none",
        )
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduler_generation": int(self.scheduler_generation),
            "ticket_id": int(self.ticket_id),
            "arm_key": self.arm_key,
            "source_root": self.source_root,
            "family": self.family,
            "coarse_key": self.coarse_key,
            "selection_reason": self.selection_reason,
            "selection_lane": self.selection_lane,
            "visibility_class": self.visibility_class,
            "evidence_origin": self.evidence_origin,
            "coverage_selected": bool(self.coverage_selected),
            "unknown_exploration": bool(self.unknown_exploration),
            "dispatched_monotonic_ns": int(self.dispatched_monotonic_ns),
            "control_revision": int(self.control_revision),
            "reward_grant_id": int(self.reward_grant_id),
            "exact_grant_lease_id": int(self.exact_grant_lease_id),
            "service_cohort_flight_id": int(self.service_cohort_flight_id),
            "similar_cohort_flight_id": int(self.similar_cohort_flight_id),
            "reward_scope": self.reward_scope,
            "reward_cohort_key": self.reward_cohort_key,
            "reward_probation": bool(self.reward_probation),
            "reward_cohort_state": self.reward_cohort_state,
            "exact_cohort_key": self.exact_cohort_key,
            "service_cohort_key": self.service_cohort_key,
            "service_cohort_keys": list(self.service_cohort_keys),
            "similar_cohort_key": self.similar_cohort_key,
            "feature_key": self.feature_key.to_dict(),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> "Tier3CandidateFeedbackTicket | None":
        if not isinstance(value, Mapping):
            return None
        try:
            generation = int(value.get("scheduler_generation", 0) or 0)
            ticket_id = int(value.get("ticket_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        if generation <= 0 or ticket_id <= 0:
            return None
        return cls(
            scheduler_generation=generation,
            ticket_id=ticket_id,
            arm_key=str(value.get("arm_key") or ""),
            source_root=str(value.get("source_root") or "unknown"),
            family=str(value.get("family") or "other"),
            coarse_key=str(value.get("coarse_key") or "unknown"),
            selection_reason=str(value.get("selection_reason") or "coverage"),
            selection_lane=(
                str(value.get("selection_lane") or "")
                if "selection_lane" in value
                else str(value.get("selection_reason") or "coverage")
            ),
            visibility_class=(
                "validated_positive"
                if str(value.get("visibility_class") or "unknown")
                == "exact_positive"
                else str(value.get("visibility_class") or "unknown")
                if str(value.get("visibility_class") or "unknown")
                in {
                    "validated_positive",
                    "validated_reprobe",
                    "tcp_only",
                    "unknown",
                    "observed_nonpositive",
                }
                else "unknown"
            ),
            evidence_origin=(
                str(value.get("evidence_origin") or "cold_unknown")
                if str(value.get("evidence_origin") or "cold_unknown")
                in {origin.value for origin in Tier3CandidateEvidenceOrigin}
                else Tier3CandidateEvidenceOrigin.COLD_UNKNOWN.value
            ),
            coverage_selected=bool(value.get("coverage_selected", False)),
            unknown_exploration=bool(
                value.get("unknown_exploration", False)
            ),
            dispatched_monotonic_ns=max(
                0,
                int(value.get("dispatched_monotonic_ns", 0) or 0),
            ),
            control_revision=max(
                0,
                int(value.get("control_revision", 0) or 0),
            ),
            reward_grant_id=max(
                0,
                int(value.get("reward_grant_id", 0) or 0),
            ),
            exact_grant_lease_id=max(
                0,
                int(value.get("exact_grant_lease_id", 0) or 0),
            ),
            service_cohort_flight_id=max(
                0,
                int(value.get("service_cohort_flight_id", 0) or 0),
            ),
            similar_cohort_flight_id=max(
                0,
                int(value.get("similar_cohort_flight_id", 0) or 0),
            ),
            reward_scope=(
                str(value.get("reward_scope") or "none")
                if str(value.get("reward_scope") or "none")
                in {"exact", "service", "similar", "shadow_broad", "none"}
                else "none"
            ),
            reward_cohort_key=str(value.get("reward_cohort_key") or ""),
            reward_probation=bool(value.get("reward_probation", False)),
            reward_cohort_state=str(value.get("reward_cohort_state") or ""),
            exact_cohort_key=str(value.get("exact_cohort_key") or ""),
            service_cohort_key=str(value.get("service_cohort_key") or ""),
            service_cohort_keys=tuple(
                str(item)
                for item in (value.get("service_cohort_keys") or ())
                if str(item or "").strip()
            ),
            similar_cohort_key=str(value.get("similar_cohort_key") or ""),
            feature_key=Tier3CandidateFeatureKey.from_mapping(
                value.get("feature_key")
                if isinstance(value.get("feature_key"), Mapping)
                else None,
                fallback_family=str(value.get("family") or "other"),
            ),
        )


TIER3_CANDIDATE_SCHEDULER_CONTRACT_HASH = hashlib.sha256(
    b"tier3-candidate-scheduler-v48|fresh-replay-quota-before-exact-arm-reward|datagram-refusal-is-not-validation-outcome|process-local-dispatch-key-rebuilt|coverage-repays-bucket-debt-no-weak-cache-fill|same-exact-grant-transport-order|transport-failure-reprobe-authority|neutral-prestart-terminal|tcp-hint-exposure-shadow-only|current-replay-bounded-leaf-discovery|rolling-evidence-confirmation-epoch|same-leaf-transport-confirmation|"
    b"tcp-observation-bounded-coverage|"
    b"typed-observation-authority-v3|"
    b"validated-positive-reprobe-tcp-only-visibility|"
    b"bounded-source-authorized-validated-replay|tcp-dead-is-weak-reprobe|"
    b"persistent-root-family-coarse-cursors|bounded-reservation-examination|"
    b"no-arm-wide-tcp-positive-promotion|"
    b"production-adaptive-fresh-historical-replay|"
    b"exact-l3-bounded-1of1-probation-2netnew-guarded-proven|"
    b"stable-cohort-evidence-order|bounded-cold-l3-coverage|"
    b"cold-discovery-borrows-idle-priority-share|"
    b"l2-direct-child-discovery-only|no-parent-production-fallback|"
    b"accepted-fast-observation-truth|"
    b"transient-continuation-preserves-neutral-observation|"
    b"inventory-activated-exact-grant|evidence-qualified-parent-holdout|"
    b"tcp-single-deadline-delivery-grace|"
    b"start-ack-supply-denominator|quota-coverage-handoff-epochs|"
    b"tcp-start-ack-handoff|feedback-only-ticket|"
    b"single-tcp-execution-lease|immutable-wave-outcome-context|"
    b"root-family-hierarchical-drr|source-identity-opaque|no-cross-stage-credit|"
    b"protocol-family|exact-service-similar-reward|separate-cohort-credit|"
    b"adaptive-exploit|independent-exact-grants|structural-service-learning|"
    b"origin-bucket-opportunity-lifetime|bounded-similar-sparse-probation|"
    b"hierarchical-l0-l3-service-profile|ancestor-fast-feedback|"
    b"dirty-cohort-global-reevaluation|service-evidence-before-dispatch|"
    b"bounded-ancestor-discovery|ephemeral-auto-pool-structural-prior|"
    b"leaf-sparse-probation|rolling-completed-sample-evaluation-watermark|"
    b"precise-leaf-production-cohort|independent-service-holdout|"
    b"candidate-bound-feature-attribution|inventory-aware-reward|"
    b"direct-child-discovery-inventory|proven-production-holdout-window|"
    b"bounded-cohort-diagnostics|exact-selection-credit-wall-ttl|"
    b"async-reward-flight-ownership|end-to-end-service-yield|"
    b"evidence-qualified-network-cohort-holdout|"
    b"source-independent-full-l3-leaf-holdout|live-child-inventory-discovery|"
    b"prestart-cancel-requeue-conservation|bounded-leaf-confirmation|"
    b"post-pass-bounded-parallel-leaf-confirmation|"
    b"cold-discovery-deduplicates-all-started-lanes|"
    b"confirmation-shares-discovery-budget-with-unseen-leaves|"
    b"confirmation-cannot-reuse-exhausted-pass-evidence|"
    b"confirmation-late-results-cannot-reset-production|"
    b"probation-cannot-reuse-defeated-training-evidence|"
    b"cold-discovery-covers-unseen-l2-before-repeated-children|"
    b"confirmation-first-attempt-before-retry|"
    b"confirmation-fast-permits-exclude-actor-retained-deep|"
    b"independent-parent-discovery-budget|discovery-inventory-truth|"
    b"adaptive-fresh-historical-replay|exact-l3-production-only|"
    b"bounded-l2-direct-child-discovery|resolved-outcome-lane-budget|"
    b"replay-cold-evidence-authority-isolation|"
    b"rolling-recent-replay-yield|single-class-quota-spillover|"
    b"bounded-cold-l2-unresolved-training-diversity|"
    b"exact-before-structural-reward|network-similar-shadow-only|"
    b"strong-l2-discovery-before-leaf-confirmation"
).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class Tier3TcpHandoffLease:
    """A reversible capacity lease that ends at the TCP outcome boundary."""

    ticket: Tier3CandidateFeedbackTicket
    candidate: ScheduledTier3Candidate
    control_revision: int
    reserved_monotonic_ns: int


# One release keeps the old import names valid while capacity semantics use the
# explicit TCP-only lease contract. The immutable ticket is attribution only.
Tier3CandidateDispatchTicket = Tier3CandidateFeedbackTicket
Tier3CandidateHandoffReservation = Tier3TcpHandoffLease


@dataclass(frozen=True, slots=True)
class Tier3HandoffLaneBudget:
    """Lane debt over the latest committed TCP-start epoch."""

    projected_handoffs: int
    epoch_size: int
    coverage_limit: int
    coverage_remaining: int
    unknown_floor: int
    unknown_remaining: int
    exact_positive_limit: int
    exact_positive_remaining: int
    reward_limit: int
    reward_remaining: int
    validated_replay_limit: int = 0
    validated_replay_remaining: int = 0
    validated_reprobe_limit: int = 0
    validated_reprobe_remaining: int = 0
    fresh_replay_limit: int = 0
    fresh_replay_remaining: int = 0
    historical_reprobe_limit: int = 0
    historical_reprobe_remaining: int = 0
    structural_production_limit: int = 0
    structural_production_remaining: int = 0
    combined_priority_limit: int = 0
    combined_priority_remaining: int = 0
    discovery_limit: int = 0
    discovery_remaining: int = 0
    exact_committed: int = 0
    leaf_confirmation_committed: int = 0
    service_discovery_committed: int = 0
    service_probation_committed: int = 0
    service_proven_committed: int = 0
    ordinary_committed: int = 0


@dataclass(frozen=True, slots=True)
class Tier3CandidateSupplySnapshot:
    """Non-mutating truth about FAST work that can actually be reserved."""

    raw_backlog: int
    normal_reservable_count: int
    reward_reservable_count: int
    escape_eligible_bucket_count: int
    reserved_handoff_count: int
    active_escape_lease_count: int
    fast_supply_state: str
    unique_backlog: int = 0
    duplicate_observation_count: int = 0
    deep_retained_count: int = 0
    deep_retention_limit: int = 0
    deep_retention_overflow_count: int = 0
    handoff_window_limit: int = 0
    tcp_execution_limit: int = 0
    tcp_handoff_lease_count: int = 0
    feedback_only_ticket_count: int = 0
    expected_responsive_arrivals: int = 0
    tcp_responsive_probability_lcb: float = 0.25
    tcp_start_acked_sample_count: int = 0
    tcp_start_acked_responsive_rate: float = 0.0
    tcp_completed_sample_count: int = 0
    tcp_hard_censored_sample_count: int = 0
    tcp_completed_result_rate: float = 0.0
    expected_redeemable_fast_arrivals: int = 0
    free_fast_slots: int = 0
    last_real_wave_fill: int = 0
    last_wave_root_coverage: int = 0
    last_wave_family_coverage: int = 0
    leaf_confirmation_reservable_count: int = 0
    leaf_confirmation_outstanding_count: int = 0
    leaf_confirmation_fast_pending_count: int = 0
    leaf_confirmation_deep_pending_count: int = 0
    parent_discovery_reservable_count: int = 0
    parent_discovery_outstanding_count: int = 0
    parent_discovery_overdue_rounds: int = 0
    lookahead_indexed_count: int = 0
    lookahead_eligible_count: int = 0
    lookahead_examined_total: int = 0
    lookahead_hit_total: int = 0
    lookahead_validated_positive_count: int = 0
    lookahead_validated_reprobe_count: int = 0
    lookahead_tcp_only_count: int = 0
    # One-schema compatibility alias for validated-positive inventory.
    lookahead_exact_positive_count: int = 0
    lookahead_unknown_count: int = 0
    lookahead_observed_nonpositive_count: int = 0
    lookahead_cursor_wrap_count: int = 0
    lookahead_max_examined_per_reservation: int = 0
    lookahead_mode: str = "validated_observation_cursor"


@dataclass(slots=True)
class _Arm:
    arm_id: str
    source_root: str
    family: str
    coarse_key: str
    feature_key: Tier3CandidateFeatureKey
    items: deque[ScheduledTier3Candidate] = field(default_factory=deque)
    active: bool = False
    selected_count: int = 0
    tcp_observed: bool = False
    validation_observed: bool = False
    validation_pending: bool = False
    fairness_debt: int = 0
    fast_outstanding: int = 0
    parked_deep_debt: int = 0
    net_new_commits: int = 0
    credit_exempt: bool = False
    tcp_completed: int = 0
    tcp_responsive: int = 0


@dataclass(slots=True)
class _BucketCredit:
    parked_deep_debt: int = 0
    escape_ticket_id: int = 0
    queued_count: int = 0
    queued_credit_exempt_count: int = 0
    ready_arm_count: int = 0
    ready_credit_exempt_arm_count: int = 0
    committed_handoff_count: int = 0


@dataclass(slots=True)
class _FamilyRewardGrant:
    grant_id: int
    origin_root: str
    origin_family: str
    preferred_arm_id: str
    preferred_feature_key: Tier3CandidateFeatureKey
    exact_handoffs_remaining: int
    service_handoffs_remaining: int
    similar_handoffs_remaining: int
    eligible_handoff_opportunities: int
    service_handoff_opportunities: int
    created_monotonic_ns: int
    inventory_activated_monotonic_ns: int = 0
    last_inventory_seen_monotonic_ns: int = 0
    dormant: bool = True
    used: bool = False
    first_handoff_recorded: bool = False
    expired: bool = False
    expiry_reason: str = ""
    outstanding_handoffs: int = 0
    resolved_handoffs: int = 0
    outstanding_high_water: int = 0
    pass_pending_count: int = 0
    net_new_count: int = 0


@dataclass(slots=True)
class _CohortYieldState:
    scope: str
    first_evidence_sequence: int = 0
    hierarchy_level: int = -1
    parent_key: str = ""
    samples: deque[tuple[float, bool, bool, int]] = field(
        default_factory=lambda: deque(maxlen=64)
    )
    sample_total: int = 0
    completed_total: int = 0
    pass_total: int = 0
    last_evaluated_completions: int = 0
    last_evaluated_reference_completions: int = 0
    winning_windows: int = 0
    losing_windows: int = 0
    exploit_share: float = 0.25
    promoted: bool = False
    promotion_count: int = 0
    demotion_count: int = 0
    zero_pass_demotion_count: int = 0
    advantage_confirmed: bool = False
    discovery: bool = False
    probation: bool = False
    probation_started_completion_total: int = 0
    probation_started_pass_total: int = 0
    leaf_eligible: bool = False
    eligibility: Tier3ServiceCohortEligibility = (
        Tier3ServiceCohortEligibility.OBSERVE
    )
    lifecycle_state: str = "observe"
    prior_support_count: int = 0
    prior_child_count: int = 0
    current_pass_child_keys: set[str] = field(default_factory=set)
    current_pass_root_families: set[str] = field(default_factory=set)
    discovery_selected_child_keys: set[str] = field(default_factory=set)
    discovery_selection_total: int = 0
    discovery_exhausted: bool = False
    production_selection_total: int = 0
    production_completion_total: int = 0
    production_pass_total: int = 0
    production_worker_ms: int = 0
    probation_selection_total: int = 0
    probation_completion_total: int = 0
    probation_pass_total: int = 0
    probation_worker_ms: int = 0
    proven_selection_total: int = 0
    proven_completion_total: int = 0
    proven_pass_total: int = 0
    proven_worker_ms: int = 0
    production_samples: deque[tuple[float, bool, bool, int]] = field(
        default_factory=lambda: deque(maxlen=64)
    )
    production_state_started_completion_total: int = 0
    production_state_started_pass_total: int = 0
    production_state_started_selection_total: int = 0
    production_consecutive_nonpass: int = 0
    production_outstanding_total: int = 0
    production_outstanding_high_water: int = 0
    probation_outstanding_total: int = 0
    probation_outstanding_high_water: int = 0
    proven_outstanding_total: int = 0
    proven_outstanding_high_water: int = 0
    discovery_outstanding_total: int = 0
    discovery_outstanding_high_water: int = 0
    discovery_eligible_handoff_total: int = 0
    discovery_last_selected_handoff_total: int = 0
    confirmation_selection_total: int = 0
    confirmation_completion_total: int = 0
    confirmation_pass_total: int = 0
    confirmation_censored_total: int = 0
    confirmation_outstanding_total: int = 0
    confirmation_outstanding_high_water: int = 0
    confirmation_eligible_handoff_total: int = 0
    confirmation_evidence_pass_total: int = 0
    confirmation_epoch: int = 0
    confirmation_stale_transition_ignored_total: int = 0
    production_resolved_total: int = 0
    production_net_new_total: int = 0
    production_neutral_total: int = 0
    production_censored_total: int = 0
    evaluation_count: int = 0
    last_decision_reason: str = "unobserved"


class _CohortYieldStateMap(dict):
    """``_cohort_yield_states``: a dict that counts its non-append rewrites.

    The scheduler projects this dict incrementally, and production code only
    ever appends a new key (then registers it). Every other mutation - a new
    state object stored at an existing key, a deletion, ``clear``, ``update``
    or ``|=`` - bumps ``rewrite_version``, and the next index read rebuilds the
    projections from the dict. Reads are the plain ``dict`` methods.
    """

    rewrite_version: int = 0

    def _note_rewrite(self) -> None:
        self.rewrite_version += 1

    def __setitem__(self, key: str, value: _CohortYieldState) -> None:
        if dict.get(self, key, value) is not value:
            self._note_rewrite()
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: str) -> None:
        dict.__delitem__(self, key)
        self._note_rewrite()

    def pop(self, *args: Any) -> Any:
        self._note_rewrite()
        return dict.pop(self, *args)

    def popitem(self) -> tuple[str, _CohortYieldState]:
        self._note_rewrite()
        return dict.popitem(self)

    def clear(self) -> None:
        self._note_rewrite()
        dict.clear(self)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._note_rewrite()
        dict.update(self, *args, **kwargs)

    def __ior__(self, other: Any) -> "_CohortYieldStateMap":
        self._note_rewrite()
        dict.update(self, other)
        return self


@dataclass(slots=True)
class _ReplayLeafDiscovery:
    bucket: tuple[str, str]
    created_at: float
    ticket_id: int = 0
    selected: int = 0
    responsive: int = 0
    passed: int = 0
    resolved: int = 0
    net_new: int = 0
    cancelled_before_start: int = 0
    outcome: str = "available"


_COLD_TRAINING_ORIGINS = frozenset(
    {
        Tier3CandidateEvidenceOrigin.COLD_UNKNOWN.value,
        Tier3CandidateEvidenceOrigin.COLD_DISCOVERY.value,
    }
)
_COLD_L2_OUTSTANDING_PHASES = frozenset({"reserved", "tcp_active", "feedback_only"})
_FEEDBACK_ONLY_PHASES = frozenset({"feedback_only", "parked_deep", "terminal_pass"})
_HANDOFF_OWNING_PHASES = frozenset({"reserved", "tcp_active"})

# Each incremental ticket projection and the reference scan it must equal.
# ``_debug_ticket_index_mismatches_unlocked`` compares every pair; tests may
# bind the reference scans to build an unindexed scheduler.
_TICKET_PROJECTION_BRUTEFORCE_METHODS: dict[str, str] = {
    "_cold_l2_training_outstanding_unlocked": (
        "_cold_l2_training_outstanding_bruteforce_unlocked"
    ),
    "_cold_l2_training_outstanding_total_unlocked": (
        "_cold_l2_training_outstanding_total_bruteforce_unlocked"
    ),
    "_cold_l2_training_cohort_count_unlocked": (
        "_cold_l2_training_cohort_count_bruteforce_unlocked"
    ),
    "_cold_l2_training_current_limit_unlocked": (
        "_cold_l2_training_current_limit_bruteforce_unlocked"
    ),
    "_cohort_inventory_unlocked": "_cohort_inventory_bruteforce_unlocked",
    "_cohort_support_strata_unlocked": (
        "_cohort_support_strata_bruteforce_unlocked"
    ),
    "_leaf_confirmation_fast_pending_unlocked": (
        "_leaf_confirmation_fast_pending_bruteforce_unlocked"
    ),
    "_leaf_confirmation_deep_pending_unlocked": (
        "_leaf_confirmation_deep_pending_bruteforce_unlocked"
    ),
    "_replay_discovery_open_count_unlocked": (
        "_replay_discovery_open_count_bruteforce_unlocked"
    ),
    "_feedback_only_ticket_count_unlocked": (
        "_feedback_only_ticket_count_bruteforce_unlocked"
    ),
    "_open_cold_reservation_cohort_keys_unlocked": (
        "_open_cold_reservation_cohort_keys_bruteforce_unlocked"
    ),
    "_reserved_parent_discovery_child_keys_unlocked": (
        "_reserved_parent_discovery_child_keys_bruteforce_unlocked"
    ),
    "_stale_terminal_ticket_ids_unlocked": (
        "_stale_terminal_ticket_ids_bruteforce_unlocked"
    ),
    "_handoff_owning_ticket_ids_unlocked": (
        "_handoff_owning_ticket_ids_bruteforce_unlocked"
    ),
}

# Cohort counters whose per-scope totals the dispatch quotas and reward
# high-water marks read. Their only increments are in
# ``_open_reward_flight_unlocked``, which records the cohort key; every
# decrement clamps at zero.
_COHORT_OUTSTANDING_COUNTERS: tuple[str, ...] = (
    "discovery_outstanding_total",
    "confirmation_outstanding_total",
    "probation_outstanding_total",
    "production_outstanding_total",
)
_STRUCTURAL_PRODUCTION_ELIGIBILITIES = (
    Tier3ServiceCohortEligibility.LEAF_PROBATION,
    Tier3ServiceCohortEligibility.PROVEN_50,
    Tier3ServiceCohortEligibility.PROVEN_75,
)

# Incremental projections of ``_cohort_yield_states`` and the reference scans
# they must equal. A reference iterator yields every cohort; callers keep the
# full filter, so an indexed iterator only has to yield a superset of the
# matching cohorts in ``_cohort_yield_states`` order.
_COHORT_PROJECTION_BRUTEFORCE_METHODS: dict[str, str] = {
    "_cohort_items_with_eligibility_unlocked": (
        "_cohort_items_with_eligibility_bruteforce_unlocked"
    ),
    "_similar_cohort_items_unlocked": "_similar_cohort_items_bruteforce_unlocked",
    "_cohort_counter_total_unlocked": "_cohort_counter_total_bruteforce_unlocked",
    "_cold_leaf_parent_links_unlocked": (
        "_cold_leaf_parent_links_bruteforce_unlocked"
    ),
}


@dataclass(slots=True)
class _TicketState:
    ticket: Tier3CandidateFeedbackTicket
    arm_id: str
    candidate: ScheduledTier3Candidate
    phase: str = "reserved"
    tcp_handed_off: bool = False
    reservation_open: bool = True
    handoff_credit_released: bool = False
    feedback_released: bool = False
    fast_continuation_accepted: bool = False
    reward_flight_open: bool = False
    reward_flight_scope: str = ""
    reward_flight_key: str = ""
    reward_flight_lifecycle: str = ""
    confirmation_epoch: int = 0
    fast_pass_observed: bool = False
    replay_discovery_key: str = ""
    reward_pass_pending_commit: bool = False
    reward_flight_outcome: str = ""
    reward_pending_worker_ms: int = 0
    fairness_peer_arm_ids: tuple[str, ...] = ()
    updated_at: float = field(default_factory=time.monotonic)


class _ColdLeafReservationOrder:
    """Reuse ranking only while the reservation owner holds its scheduler lock.

    Inventory and strata can only decrease, and sampled parents only increase,
    during this scope. Ranking keys therefore only increase. Refreshing a heap
    head before using it preserves sorted order without re-ranking every leaf.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[tuple[int, int, int, int], int, str]] | None = None
        self._suspended: list[tuple[tuple[int, int, int, int], int, str]] = []

    def ordered(
        self,
        initial_inventory: Iterable[tuple[str, int]],
        inventory: Mapping[str, int],
        eligible: Callable[[str], bool],
        priority: Callable[[tuple[str, int]], tuple[int, int, int, int]],
    ) -> Iterable[tuple[str, int]]:
        if self._heap is None:
            self._heap = [
                (priority(item), ordinal, item[0])
                for ordinal, item in enumerate(initial_inventory)
            ]
            heapq.heapify(self._heap)
        for item in self._suspended:
            heapq.heappush(self._heap, item)
        self._suspended.clear()
        while self._heap:
            old_priority, ordinal, cohort_key = heapq.heappop(self._heap)
            if not eligible(cohort_key):
                continue
            count = int(inventory[cohort_key])
            current_priority = priority((cohort_key, count))
            item = (current_priority, ordinal, cohort_key)
            if current_priority != old_priority:
                heapq.heappush(self._heap, item)
                continue
            # A currently inactive arm may activate after another dequeue.
            # Keep unsuccessful probes for the next discovery call in this
            # reservation; they are not permanently removed from the index.
            self._suspended.append(item)
            yield cohort_key, count


class Tier3CandidateScheduler:
    """Bounded scheduler for candidates that have not entered TCP testing."""

    def __init__(
        self,
        *,
        max_active_arms_per_family: int = 16,
        unknown_exploration_ratio: float = 0.15,
        reward_wave_limit: int = 2,
        reward_candidate_limit: int = 8,
        exploit_scan_share: float = 0.25,
        unknown_parked_deep_limit: int = 4,
        work_conserving_parked_deep_limit: int | None = None,
        rewarded_parked_deep_limit: int = 16,
        reward_handoff_opportunity_limit: int = 16,
        learning_window: int = 8,
        score_provider: Callable[[ScheduledTier3Candidate], float] | None = None,
        lookahead_evidence_provider: (
            Callable[
                [ScheduledTier3Candidate],
                Tier3CandidateLookaheadEvidence | Mapping[str, Any],
            ]
            | None
        ) = None,
        _enable_legacy_structural_routing_for_test: bool = False,
    ) -> None:
        self.max_active_arms_per_family = max(1, int(max_active_arms_per_family))
        self.unknown_exploration_ratio = max(
            0.0,
            min(1.0, float(unknown_exploration_ratio)),
        )
        self.reward_wave_limit = max(0, int(reward_wave_limit))
        self.reward_candidate_limit = max(1, int(reward_candidate_limit))
        self.exploit_scan_share = max(0.0, min(0.25, float(exploit_scan_share)))
        self._adaptive_exploit_scan_share = self.exploit_scan_share
        self.unknown_parked_deep_limit = max(
            1,
            int(unknown_parked_deep_limit),
        )
        self.work_conserving_parked_deep_limit = max(
            self.unknown_parked_deep_limit,
            int(
                self.unknown_parked_deep_limit
                if work_conserving_parked_deep_limit is None
                else work_conserving_parked_deep_limit
            ),
        )
        self.rewarded_parked_deep_limit = max(
            self.unknown_parked_deep_limit,
            int(rewarded_parked_deep_limit),
        )
        self.reward_handoff_opportunity_limit = max(
            1,
            int(reward_handoff_opportunity_limit),
        )
        self.learning_window = max(1, min(8, int(learning_window)))
        self.score_provider = score_provider
        self.lookahead_evidence_provider = lookahead_evidence_provider
        self._legacy_structural_routing_for_test = bool(
            _enable_legacy_structural_routing_for_test
        )
        self._lock = threading.Lock()
        self._lookahead_index = Tier3CandidateVisibilityIndex()
        self._arms: dict[str, _Arm] = {}
        self._bucket_credits: dict[tuple[str, str], _BucketCredit] = {}
        self._arm_ids_by_feedback_key: dict[str, set[str]] = {}
        # ``_family_rewards`` remains a one-version compatibility view of the
        # oldest active grant. Actual ownership is multi-grant so a later PASS
        # in the same root/family cannot overwrite an earlier exact arm.
        self._family_rewards: dict[tuple[str, str], _FamilyRewardGrant] = {}
        self._family_reward_grants: dict[
            tuple[str, str], deque[_FamilyRewardGrant]
        ] = {}
        self._reward_grants_by_id: dict[int, _FamilyRewardGrant] = {}
        self._exact_rewards: dict[str, deque[_FamilyRewardGrant]] = {}
        self._active_arms: dict[tuple[str, str], deque[str]] = {}
        self._overflow_arms: dict[tuple[str, str], deque[str]] = {}
        self._root_families: dict[str, deque[str]] = {}
        self._root_order: deque[str] = deque()
        self._active_roots: set[str] = set()
        self._root_backlog_counts: dict[str, int] = {}
        self._quick_metrics_memo: tuple[float, dict[str, Any]] | None = None
        self._backlog_count = 0
        self._active_arm_count = 0
        self._overflow_arm_count = 0
        self._overflow_rotation_count = 0
        self._unknown_overflow_rotation_count = 0
        self._selected_total = 0
        self._reservation_total = 0
        self._reserved_handoff_count = 0
        self._reserved_coverage_count = 0
        self._reserved_exploit_count = 0
        self._reserved_unknown_count = 0
        self._reserved_escape_count = 0
        self._cancelled_reservation_total = 0
        self._tick_count = 0
        self._keys_examined_total = 0
        self._idle_with_backlog_count = 0
        self._last_tick_ms = 0.0
        self._last_tick_selected = 0
        self._last_tick_keys_examined = 0
        self._last_tick_exploit_selected = 0
        self._last_tick_unknown_selected = 0
        self._last_tick_credit_escape_selected = 0
        self._last_tick_exploit_reserved = 0
        self._last_tick_unknown_reserved = 0
        self._last_tick_credit_escape_reserved = 0
        self._last_handoff_count = 0
        self._scheduler_generation = max(1, time.monotonic_ns())
        self._next_ticket_id = 0
        self._next_reward_grant_id = 0
        self._next_cohort_evidence_sequence = 0
        self._tickets: dict[int, _TicketState] = {}
        self._reserved_exact_grant_handoffs: dict[int, int] = {}
        self._tcp_handoff_lease_count = 0
        self._tcp_orphan_reserved_reclaimed_total = 0
        self._tcp_orphan_started_closed_total = 0
        self._parked_deep_debt = 0
        self._duplicate_observation_count = 0
        self._credit_blocked_selection_count = 0
        self._tcp_outcome_count = 0
        self._fast_yield_count = 0
        self._terminal_feedback_count = 0
        self._credit_release_total = 0
        self._net_new_feedback_count = 0
        self._reward_hit_count = 0
        self._reward_created_count = 0
        self._reward_expired_unused_count = 0
        self._reward_expired_partial_count = 0
        self._reward_expired_credit_count = 0
        self._reward_expired_opportunity_count = 0
        self._reward_expired_wall_time_count = 0
        self._reward_expiry_reason_counts: dict[str, int] = defaultdict(int)
        self._reward_exact_hit_count = 0
        self._reward_service_hit_count = 0
        self._reward_similar_hit_count = 0
        self._reward_shadow_broad_handoff_count = 0
        self._reward_exact_tcp_responsive_count = 0
        self._reward_service_tcp_responsive_count = 0
        self._reward_similar_tcp_responsive_count = 0
        self._reward_shadow_broad_tcp_responsive_count = 0
        self._reward_exact_fast_pass_count = 0
        self._reward_service_fast_pass_count = 0
        self._reward_similar_fast_pass_count = 0
        self._reward_shadow_broad_fast_pass_count = 0
        self._reward_exact_net_new_count = 0
        self._reward_service_net_new_count = 0
        self._reward_similar_net_new_count = 0
        self._reward_shadow_broad_net_new_count = 0
        self._reward_exact_worker_ms = 0
        self._reward_service_worker_ms = 0
        self._reward_similar_worker_ms = 0
        self._reward_shadow_broad_worker_ms = 0
        self._reward_first_handoff_latency_last_ms = 0.0
        self._reward_first_handoff_latency_max_ms = 0.0
        self._reward_exact_resolved_count = 0
        self._reward_exact_neutral_count = 0
        self._reward_exact_censored_count = 0
        self._reward_exact_outstanding_high_water = 0
        self._reward_service_resolved_count = 0
        self._reward_service_neutral_count = 0
        self._reward_service_censored_count = 0
        self._reward_service_outstanding_high_water = 0
        self._reward_similar_resolved_count = 0
        self._reward_similar_neutral_count = 0
        self._reward_similar_censored_count = 0
        self._reward_similar_outstanding_high_water = 0
        self._reward_probation_outstanding_high_water = 0
        self._reward_discovery_outstanding_high_water = 0
        self._scheduling_outcome_counts: dict[str, int] = {
            outcome.value: 0 for outcome in Tier3SchedulingOutcome
        }
        self._exploit_selected_total = 0
        self._unknown_selected_total = 0
        self._coverage_selected_total = 0
        self._credit_escape_selected_total = 0
        self._credit_escape_epoch_debt: dict[tuple[str, str], int] = {}
        self._active_helper_count = 16
        self._active_helper_ceiling = 24
        self._handoff_window_limit = 64
        self._tcp_execution_limit = 64
        self._free_fast_slots = 16
        self._active_escape_lease_count = 0
        self._tcp_handoff_total = 0
        self._handoff_lane_history: deque[tuple[bool, bool, bool]] = deque(
            maxlen=16
        )
        self._handoff_selection_history: deque[str] = deque(maxlen=16)
        self._visibility_handoff_history: deque[str] = deque(maxlen=16)
        self._visibility_reserved_open: dict[str, int] = defaultdict(int)
        self._visibility_reserved_total: dict[str, int] = defaultdict(int)
        self._visibility_start_total: dict[str, int] = defaultdict(int)
        self._visibility_responsive_total: dict[str, int] = defaultdict(int)
        self._visibility_fast_pass_total: dict[str, int] = defaultdict(int)
        self._visibility_net_new_total: dict[str, int] = defaultdict(int)
        self._visibility_resolved_total: dict[str, int] = defaultdict(int)
        self._visibility_censored_total: dict[str, int] = defaultdict(int)
        self._fast_visibility_cost: dict[str, Counter[str]] = defaultdict(Counter)
        self._visibility_outcome_windows: dict[str, deque[bool]] = {}
        self._visibility_resolved_ticket_ids: set[int] = set()
        self._session_started_at = time.monotonic()
        self._visibility_inventory_exhausted_at_ms: dict[str, int] = {}
        self._fresh_replay_quota = 12
        self._fresh_replay_winning_windows = 0
        self._fresh_replay_losing_windows = 0
        self._fresh_replay_last_evaluated_resolved = 0
        # Historical entries are less authoritative than current-contract
        # fresh replay. Start below the 4/16 hard ceiling so a mixed cache
        # cannot force an otherwise successful cold suffix into another TCP
        # epoch before it has produced current-generation value.
        self._historical_reprobe_quota = 3
        self._historical_reprobe_last_evaluated_resolved = 0
        self._visibility_budget_adjustment_count = 0
        self._tcp_hint_fast_window: deque[tuple[bool, int]] = deque(maxlen=16)
        self._tcp_hint_yield_limited = False
        self._tcp_hint_limit_count = 0
        self._tcp_hint_restore_count = 0
        self._tcp_outcome_window: deque[tuple[int, str]] = deque(
            maxlen=64
        )
        self._tcp_hard_censored_count = 0
        self._last_wave_root_coverage = 0
        self._last_wave_family_coverage = 0
        self._current_reservation_revision = 0
        self._cold_leaf_reservation_order: _ColdLeafReservationOrder | None = None
        self._cohort_yield_states: dict[str, _CohortYieldState] = (
            _CohortYieldStateMap()
        )
        self._ordinary_ticket_samples: deque[
            tuple[float, bool, bool, int, frozenset[str]]
        ] = deque(maxlen=64)
        self._ordinary_ticket_completion_total = 0
        self._ordinary_e2e_samples: deque[
            tuple[float, bool, bool, int, frozenset[str]]
        ] = deque(maxlen=64)
        self._ordinary_e2e_ticket_ids: set[int] = set()
        self._ordinary_e2e_completion_total = 0
        self._dirty_cohort_keys: set[str] = set()
        self._cohort_evaluation_trigger_count = 0
        self._service_probation_entry_count = 0
        self._service_probation_exit_count = 0
        self._service_discovery_entry_count = 0
        self._service_discovery_exit_count = 0
        self._service_prior_support_count: dict[str, int] = {}
        self._service_prior_child_keys: dict[str, set[str]] = {}
        self._fast_observed_ticket_ids: set[int] = set()
        self._fast_observation_sample_count = 0
        self._fast_observation_completion_count = 0
        self._fast_observation_pass_count = 0
        self._fast_observation_exposure_ms = 0
        self._replay_fast_observation_sample_count = 0
        self._replay_fast_observation_completion_count = 0
        self._replay_fast_observation_pass_count = 0
        self._cold_fast_observation_sample_count = 0
        self._cold_fast_observation_completion_count = 0
        self._cold_fast_observation_pass_count = 0
        self._replay_leaf_discoveries: dict[str, _ReplayLeafDiscovery] = {}
        self._probation_handoff_history: deque[str] = deque(maxlen=16)
        self._reserved_probation_handoffs: dict[str, int] = {}
        self._reserved_service_handoffs: dict[str, int] = {}
        self._reserved_similar_handoffs: dict[str, int] = {}
        self._service_handoff_history: deque[tuple[str, str]] = deque(maxlen=16)
        self._reserved_discovery_handoffs: dict[str, int] = {}
        self._reserved_confirmation_handoffs: dict[str, int] = {}
        self._reserved_cold_leaf_handoffs: dict[str, int] = {}
        self._cold_leaf_started_keys: set[str] = set()
        self._cold_leaf_sampled_keys: set[str] = set()
        self._cold_l2_sampled_keys: set[str] = set()
        self._cold_leaf_coverage_selected_total = 0
        self._cold_leaf_coverage_started_total = 0
        self._cohort_decision_ring: deque[dict[str, Any]] = deque(maxlen=64)
        self._cohort_decisions_by_ticket: dict[int, dict[str, Any]] = {}
        self._cohort_queued_inventory: dict[str, int] = {}
        self._cohort_cold_queued_inventory: dict[str, int] = {}
        self._cohort_unknown_queued_arms: dict[str, dict[str, int]] = {}
        self._cohort_tcp_only_queued_arms: dict[str, dict[str, int]] = {}
        self._exact_queued_inventory: dict[
            tuple[str, Tier3CandidateFeatureKey], int
        ] = {}
        self._cohort_first_inventory_sequence: dict[str, int] = {}
        self._cohort_queued_buckets: dict[
            str,
            dict[tuple[str, str], int],
        ] = {}
        self._cohort_queued_arms: dict[
            str,
            dict[tuple[str, str], dict[str, int]],
        ] = {}
        self._cohort_queued_direct_children: dict[str, dict[str, int]] = {}
        self._origin_structural_cohort_keys: set[str] = set()
        self._cold_unknown_l2_queued_inventory: dict[str, int] = {}
        self._cold_l2_training_deferred_total = 0
        self._cold_l2_training_borrowed_total = 0
        self._cold_l2_training_outstanding_high_water = 0
        # Incremental projections of ``_tickets``. Every mutation of a field
        # they read is followed by ``_reindex_ticket_unlocked`` and every
        # removal goes through ``_forget_ticket_unlocked``.
        self._ticket_index_signatures: dict[int, tuple[Any, ...]] = {}
        self._cold_l2_outstanding: dict[str, int] = {}
        self._cold_l2_outstanding_total = 0
        # Number of L2 keys with cold unknown inventory or cold outstanding.
        self._cold_l2_visible_count = 0
        # (active helpers, visible count, limit) of the last limit computed;
        # ``set_cold_l2_training_breadth`` resets it.
        self._cold_l2_limit_memo: tuple[int, int, int] = (-1, -1, 0)
        # Opt-in TCP breadth (tiers.tier3.tcp_breadth_per_identity). The
        # cold-L2 training limit shares ``ceil(helpers * breadth)`` instead of
        # ``helpers``; 1.0 is the audited default formula.
        self._cold_l2_training_breadth = DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY
        self._open_reservation_ticket_ids: dict[int, None] = {}
        self._terminal_ticket_ids: dict[int, None] = {}
        self._handoff_owning_ticket_ids: dict[int, None] = {}
        self._replay_discovery_open_count = 0
        self._feedback_only_ticket_count = 0
        self._confirmation_fast_pending_ticket_count = 0
        self._confirmation_deep_pending_ticket_count = 0
        self._ticket_cohort_roots: dict[str, dict[tuple[str, str], int]] = {}
        self._ticket_cohort_reserved: dict[str, int] = {}
        self._ticket_cohort_active: dict[str, int] = {}
        self._ticket_service_live_roots: dict[
            str, dict[tuple[str, str], int]
        ] = {}
        # Incremental projections of ``_cohort_yield_states``. Production
        # code only appends cohorts, so an insertion ordinal reproduces dict
        # order. ``_register_cohort_state_unlocked`` indexes each new cohort
        # and ``_set_service_eligibility_unlocked`` moves it between buckets.
        # Anything else is caught before the next read, which then rebuilds
        # the whole index from the dict: a cohort appended without
        # registration changes the dict length, and a state object stored at
        # an existing key (or a deletion, clear or update) bumps the map's
        # ``rewrite_version`` (``_CohortYieldStateMap``).
        self._cohort_index_rewrite_version = 0
        self._cohort_state_ordinals: dict[str, int] = {}
        self._cohort_key_by_state_id: dict[int, str] = {}
        self._cohort_keys_by_eligibility: dict[str, dict[str, None]] = {}
        self._similar_scope_cohort_keys: dict[str, None] = {}
        # Keys whose counter may be positive; zero entries are dropped lazily.
        self._cohort_counter_keys: dict[str, dict[str, None]] = {
            name: {} for name in _COHORT_OUTSTANDING_COUNTERS
        }
        self._cohort_index_dirty = False
        # Bumped whenever ``_cohort_queued_direct_children`` gains or loses a
        # parent or a child key; the cold-leaf lane caches its parent links.
        self._cohort_direct_children_version = 0
        self._cold_leaf_parent_links_cache: (
            tuple[int, dict[str, str], frozenset[str]] | None
        ) = None

    def set_score_provider(
        self,
        provider: Callable[[ScheduledTier3Candidate], float] | None,
    ) -> None:
        with self._lock:
            self.score_provider = provider
            for entry in self._lookahead_index.entries():
                self._lookahead_index.update_evidence(
                    entry.candidate,
                    self._lookahead_evidence_for_candidate_unlocked(
                        entry.candidate
                    ),
                )
            self._rebuild_cold_unknown_l2_inventory_unlocked()

    def set_lookahead_evidence_provider(
        self,
        provider: (
            Callable[
                [ScheduledTier3Candidate],
                Tier3CandidateLookaheadEvidence | Mapping[str, Any],
            ]
            | None
        ),
    ) -> None:
        with self._lock:
            self.lookahead_evidence_provider = provider
            for entry in self._lookahead_index.entries():
                self._lookahead_index.update_evidence(
                    entry.candidate,
                    self._lookahead_evidence_for_candidate_unlocked(
                        entry.candidate
                    ),
                )
            self._rebuild_cold_unknown_l2_inventory_unlocked()

    def _lookahead_evidence_for_candidate_unlocked(
        self,
        candidate: ScheduledTier3Candidate,
    ) -> Tier3CandidateLookaheadEvidence:
        raw: Tier3CandidateLookaheadEvidence | Mapping[str, Any] | None = None
        if self.lookahead_evidence_provider is not None:
            try:
                raw = self.lookahead_evidence_provider(candidate)
            except Exception:
                raw = None
        if isinstance(raw, Tier3CandidateLookaheadEvidence):
            return raw
        if isinstance(raw, Mapping):
            try:
                score = float(raw.get("pre_tcp_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            return Tier3CandidateLookaheadEvidence(
                validated_positive=bool(
                    raw.get("validated_positive", False)
                ),
                validated_reprobe=bool(
                    raw.get("validated_reprobe", False)
                ),
                tcp_only=bool(raw.get("tcp_only", False)),
                observed_nonpositive=bool(
                    raw.get("observed_nonpositive", False)
                ),
                stable_tcp_positive=bool(raw.get("stable_tcp_positive", False)),
                observed=bool(raw.get("observed", False)),
                pre_tcp_score=max(-1.0, min(1.0, score)),
            )
        score = 0.0
        if self.score_provider is not None:
            try:
                score = float(self.score_provider(candidate))
            except Exception:
                score = 0.0
        return Tier3CandidateLookaheadEvidence(
            pre_tcp_score=max(-1.0, min(1.0, score)),
        )

    def seed_service_pool_prior(
        self,
        feature_keys: Iterable[Tier3CandidateFeatureKey],
    ) -> None:
        """Seed weak, process-local structural discovery evidence.

        The prior never contributes a FAST completion or PASS. It can only
        unlock a bounded discovery lane after the current expansion observes
        a real PASS in the same hierarchy.
        """

        with self._lock:
            for feature_key in feature_keys:
                profiles = tuple(feature_key.ordered_service_profiles())
                for index, profile in enumerate(profiles):
                    cohort_key = f"service:{profile}"
                    self._service_prior_support_count[cohort_key] = (
                        int(self._service_prior_support_count.get(cohort_key, 0))
                        + 1
                    )
                    child_key = (
                        f"service:{profiles[index + 1]}"
                        if index + 1 < len(profiles)
                        else f"prior-leaf:{profiles[-1]}"
                    )
                    self._service_prior_child_keys.setdefault(
                        cohort_key,
                        set(),
                    ).add(child_key)
                    state = self._cohort_yield_states.get(cohort_key)
                    if state is not None:
                        state.prior_support_count = int(
                            self._service_prior_support_count[cohort_key]
                        )
                        state.prior_child_count = len(
                            self._service_prior_child_keys[cohort_key]
                        )
                        self._dirty_cohort_keys.add(cohort_key)

    def set_credit_envelope(
        self,
        active_helpers: int,
        *,
        handoff_window: int | None = None,
        tcp_execution_limit: int | None = None,
        free_fast_slots: int | None = None,
        active_helper_ceiling: int | None = None,
    ) -> None:
        """Publish the TCP-only handoff window for the current PI revision."""

        with self._lock:
            # The batch backend carries more identities than processes; the
            # owner passes its identity ceiling so 64 live identities are not
            # clamped to 24.  Default keeps the audited single-slot clamp.
            self._active_helper_ceiling = max(24, int(active_helper_ceiling or 24))
            self._active_helper_count = max(
                1, min(self._active_helper_ceiling, int(active_helpers))
            )
            default_window = 4 * self._active_helper_count
            requested = (
                default_window if handoff_window is None else int(handoff_window)
            )
            self._handoff_window_limit = max(1, requested)
            # ``handoff_window`` is a rolling planning/statistics horizon.  It
            # is deliberately not the number of queue-resident TCP leases.
            # Keep the old implicit behavior for standalone compatibility
            # callers; production publishes the PI-owned execution limit.
            self._tcp_execution_limit = max(
                1,
                int(
                    requested
                    if tcp_execution_limit is None
                    else tcp_execution_limit
                ),
            )
            if free_fast_slots is not None:
                self._free_fast_slots = max(0, int(free_fast_slots))

    def set_cold_l2_training_breadth(self, breadth: float) -> None:
        """Scale the cold-L2 training limit with opt-in TCP breadth.

        ``breadth`` is ``tiers.tier3.tcp_breadth_per_identity`` (clamped to
        [1, 4]; anything unusable is 1.0). The per-L2 limit then shares
        ``ceil(active_helpers * breadth)`` instead of ``active_helpers``:
        ``min(W, max(4, ceil(W / visible_l2_cohorts)))``. Each L2 cohort
        keeps the share of the widened TCP pipeline it had of the identity
        pipeline at 1.0, so one large structural family still cannot take
        more than its fair share of the unresolved cold samples, and the
        limit still holds new samples of a capped L2 until FAST feedback.
        At 1.0 the formula is the audited default exactly.
        """

        normalized = tier3_tcp_breadth_per_identity(breadth)
        with self._lock:
            if normalized != self._cold_l2_training_breadth:
                self._cold_l2_training_breadth = normalized
                self._cold_l2_limit_memo = (-1, -1, 0)

    def cold_l2_training_snapshot(self) -> dict[str, Any]:
        """Exact O(1) view of the cold-L2 training limit; takes the lock.

        Report builders that must not wait on the lock read the same values
        from ``quick_metrics(max_age_seconds=...)`` instead: its
        ``cold_l2_training_*`` keys, plus ``cold_l2_training_breadth`` and
        ``cold_l2_training_capacity`` while the breadth is above 1.0.
        """

        with self._lock:
            return {
                "breadth": float(self._cold_l2_training_breadth),
                "capacity": int(self._cold_l2_training_capacity_unlocked()),
                "current_limit": int(
                    self._cold_l2_training_current_limit_unlocked()
                ),
                "cohort_count": int(
                    self._cold_l2_training_cohort_count_unlocked()
                ),
                "outstanding_count": int(
                    self._cold_l2_training_outstanding_total_unlocked()
                ),
                "outstanding_high_water": int(
                    self._cold_l2_training_outstanding_high_water
                ),
                "deferred_total": int(self._cold_l2_training_deferred_total),
                "borrowed_total": int(self._cold_l2_training_borrowed_total),
            }

    @staticmethod
    def normalize_family(value: str) -> str:
        family = str(value or "").strip().lower()
        family = {"shadowsocks": "ss", "hy2": "hysteria2"}.get(
            family,
            family,
        )
        if not family or family in {"default", "aggregate", "unknown"}:
            return "other"
        return family

    @staticmethod
    def _arm_id(source_root: str, family: str, coarse_key: str) -> str:
        return "\x1f".join((source_root, family, coarse_key))

    def _ensure_root_family(self, source_root: str, family: str) -> None:
        families = self._root_families.setdefault(source_root, deque())
        if family not in families:
            families.append(family)

    def _activate_arm(self, arm: _Arm) -> None:
        bucket = (arm.source_root, arm.family)
        active = self._active_arms.setdefault(bucket, deque())
        if len(active) < self.max_active_arms_per_family:
            active.append(arm.arm_id)
            arm.active = True
            self._active_arm_count += 1
        else:
            self._overflow_arms.setdefault(bucket, deque()).append(arm.arm_id)
            self._overflow_arm_count += 1

    def _family_reward_grant(
        self,
        bucket: tuple[str, str],
    ) -> _FamilyRewardGrant | None:
        grants = self._family_reward_grants.get(bucket)
        if not grants:
            self._family_rewards.pop(bucket, None)
            return None
        for grant in tuple(grants):
            self._refresh_reward_grant_lifetime_unlocked(grant)
            if self._reward_credit_remaining(grant) > 0 and not grant.expired:
                self._family_rewards[bucket] = grant
                return grant
            self._expire_family_reward_unlocked(bucket, grant)
        self._family_rewards.pop(bucket, None)
        return None

    def _refresh_reward_grant_lifetime_unlocked(
        self,
        grant: _FamilyRewardGrant,
    ) -> None:
        if grant.expired:
            return
        if grant.expiry_reason:
            grant.similar_handoffs_remaining = 0
            return
        now_ns = time.monotonic_ns()
        inventory_count = self._grant_exact_inventory_count_unlocked(grant)
        if inventory_count > 0:
            grant.last_inventory_seen_monotonic_ns = now_ns
            if grant.inventory_activated_monotonic_ns <= 0:
                grant.inventory_activated_monotonic_ns = now_ns
                grant.dormant = False
        if grant.inventory_activated_monotonic_ns <= 0:
            if now_ns - int(grant.created_monotonic_ns) >= 60_000_000_000:
                expired_credit = max(0, int(grant.exact_handoffs_remaining))
                self._reward_expired_credit_count += expired_credit
                grant.exact_handoffs_remaining = 0
                grant.service_handoffs_remaining = 0
                grant.expiry_reason = "dormant_inventory_timeout"
                self._reward_expired_wall_time_count += 1
            grant.similar_handoffs_remaining = 0
            return
        wall_time_expired = bool(
            now_ns - int(grant.inventory_activated_monotonic_ns)
            >= 15_000_000_000
        )
        if wall_time_expired:
            expired_credit = (
                max(0, int(grant.exact_handoffs_remaining))
                + max(0, int(grant.service_handoffs_remaining))
            )
            self._reward_expired_credit_count += expired_credit
            grant.exact_handoffs_remaining = 0
            grant.service_handoffs_remaining = 0
            grant.expiry_reason = "active_inventory_wall_time_limit"
            self._reward_expired_wall_time_count += 1
        # Network-similar is a shadow cohort. It must never retain production
        # credit or keep a grant alive after exact/service expiry.
        grant.similar_handoffs_remaining = 0

    def _expire_family_reward_unlocked(
        self,
        bucket: tuple[str, str],
        grant: _FamilyRewardGrant,
    ) -> None:
        if grant.expired:
            if grant.outstanding_handoffs <= 0:
                self._reward_grants_by_id.pop(grant.grant_id, None)
            return
        grant.expired = True
        if not grant.expiry_reason:
            grant.expiry_reason = "selection_credit_exhausted"
        self._reward_expiry_reason_counts[grant.expiry_reason] += 1
        remaining = self._reward_credit_remaining(grant)
        if remaining:
            self._reward_expired_credit_count += remaining
        if grant.used:
            self._reward_expired_partial_count += 1
        else:
            self._reward_expired_unused_count += 1
        grants = self._family_reward_grants.get(bucket)
        if grants is not None:
            try:
                grants.remove(grant)
            except ValueError:
                pass
            if not grants:
                self._family_reward_grants.pop(bucket, None)
        if grant.outstanding_handoffs <= 0:
            self._reward_grants_by_id.pop(grant.grant_id, None)
        exact = self._exact_rewards.get(grant.preferred_arm_id)
        if exact is not None:
            try:
                exact.remove(grant)
            except ValueError:
                pass
            if not exact:
                self._exact_rewards.pop(grant.preferred_arm_id, None)
        replacement = next(
            (
                candidate
                for candidate in self._family_reward_grants.get(bucket, ())
                if not candidate.expired
                and self._reward_credit_remaining(candidate) > 0
            ),
            None,
        )
        if replacement is None:
            self._family_rewards.pop(bucket, None)
        else:
            self._family_rewards[bucket] = replacement

    @staticmethod
    def _reward_credit_remaining(grant: _FamilyRewardGrant) -> int:
        return sum(
            max(0, int(value))
            for value in (
                grant.exact_handoffs_remaining,
                grant.service_handoffs_remaining,
                grant.similar_handoffs_remaining,
            )
        )

    @staticmethod
    def _feature_signature(feature_key: Tier3CandidateFeatureKey) -> str:
        return "\x1f".join(
            (
                feature_key.service_family,
                feature_key.transport,
                feature_key.security,
                feature_key.port_bucket,
                feature_key.address_bucket,
                feature_key.host_suffix,
            )
        )

    def _reward_cohort_key(
        self,
        scope: str,
        grant: _FamilyRewardGrant | None,
        candidate: ScheduledTier3Candidate,
        *,
        require_dispatch_quota: bool = False,
    ) -> str:
        if scope == "exact":
            return f"exact:{grant.preferred_arm_id if grant else candidate.arm_key}"
        if scope == "service":
            profile = self._matching_service_profile_unlocked(
                candidate.feature_key,
                grant=grant,
                require_promoted=grant is None,
                require_dispatch_quota=require_dispatch_quota,
            )
            return f"service:{profile}" if profile else "ordinary"
        if scope == "similar":
            feature = (
                grant.preferred_feature_key
                if grant is not None
                else candidate.feature_key
            )
            return f"similar:{self._feature_signature(feature)}"
        if scope == "shadow_broad":
            return f"broad:{candidate.source_root}\x1f{candidate.family}"
        return "ordinary"

    @staticmethod
    def _service_eligibility_unlocked(
        state: _CohortYieldState | None,
    ) -> Tier3ServiceCohortEligibility:
        if state is None:
            return Tier3ServiceCohortEligibility.OBSERVE
        return state.eligibility

    def _cohort_is_production_eligible_unlocked(self, cohort_key: str) -> bool:
        state = self._cohort_yield_states.get(str(cohort_key or ""))
        return self._service_eligibility_unlocked(state) in {
            Tier3ServiceCohortEligibility.PARENT_PROBATION,
            Tier3ServiceCohortEligibility.PARENT_PROVEN,
            Tier3ServiceCohortEligibility.LEAF_PROBATION,
            Tier3ServiceCohortEligibility.PROVEN_50,
            Tier3ServiceCohortEligibility.PROVEN_75,
        }

    def _candidate_matches_service_cohort_unlocked(
        self,
        candidate: ScheduledTier3Candidate,
        cohort_key: str,
        state: _CohortYieldState | None = None,
    ) -> bool:
        return self._feature_matches_service_cohort_unlocked(
            candidate.feature_key,
            cohort_key,
            state,
        )

    def _feature_matches_service_cohort_unlocked(
        self,
        feature_key: Tier3CandidateFeatureKey,
        cohort_key: str,
        state: _CohortYieldState | None = None,
    ) -> bool:
        key = str(cohort_key or "")
        cohort_keys = tuple(
            f"service:{profile}"
            for profile in feature_key.ordered_service_profiles()
        )
        try:
            index = cohort_keys.index(key)
        except ValueError:
            return False
        state = state or self._cohort_yield_states.get(key)
        if state is None:
            return False
        if index >= len(cohort_keys) - 1 or state.leaf_eligible:
            return True
        direct_child = cohort_keys[index + 1]
        # Parent evidence generalizes by one verified structural edge only.
        # Unseen siblings remain ordinary exploration work.
        return direct_child in state.current_pass_child_keys

    def _matching_service_profile_unlocked(
        self,
        feature_key: Tier3CandidateFeatureKey,
        *,
        grant: _FamilyRewardGrant | None = None,
        require_promoted: bool = False,
        require_dispatch_quota: bool = False,
    ) -> str:
        profiles = tuple(feature_key.ordered_service_profiles())
        if not profiles:
            return ""
        if grant is not None:
            grant_profiles = tuple(
                grant.preferred_feature_key.ordered_service_profiles()
            )
            if not grant_profiles or profiles[-1] != grant_profiles[-1]:
                return ""
            return profiles[-1]
        for profile in reversed(profiles):
            cohort_key = f"service:{profile}"
            state = self._cohort_yield_states.get(cohort_key)
            if (
                require_promoted
                and not self._cohort_is_production_eligible_unlocked(
                    cohort_key
                )
            ):
                continue
            if state is not None and not self._feature_matches_service_cohort_unlocked(
                feature_key,
                cohort_key,
                state,
            ):
                continue
            if (
                require_dispatch_quota
                and not self._service_dispatch_quota_open_unlocked(cohort_key)
            ):
                continue
            return profile
        return ""

    @staticmethod
    def _bump_count(mapping: dict[Any, int], key: Any, delta: int) -> None:
        value = mapping.get(key, 0) + delta
        if value > 0:
            mapping[key] = value
        else:
            mapping.pop(key, None)

    @classmethod
    def _bump_nested_count(
        cls,
        mapping: dict[str, dict[tuple[str, str], int]],
        key: str,
        item: tuple[str, str],
        delta: int,
    ) -> None:
        inner = mapping.get(key)
        if inner is None:
            if delta <= 0:
                return
            inner = mapping[key] = {}
        cls._bump_count(inner, item, delta)
        if not inner:
            mapping.pop(key, None)

    @staticmethod
    def _ticket_cohort_projection(
        ticket: Tier3CandidateFeedbackTicket,
    ) -> tuple[frozenset[str], frozenset[str], tuple[str, str]]:
        service_keys = frozenset(ticket.service_cohort_keys)
        cohort_keys = (
            service_keys | {ticket.similar_cohort_key}
            if ticket.similar_cohort_key
            else service_keys
        )
        return cohort_keys, service_keys, (ticket.source_root, ticket.family)

    @staticmethod
    def _ticket_index_signature(state: _TicketState) -> tuple[Any, ...]:
        """Return every ticket-derived fact the incremental projections use."""

        ticket = state.ticket
        phase = state.phase
        cohort_keys = ticket.service_cohort_keys
        cold_l2_key = (
            cohort_keys[2]
            if len(cohort_keys) >= 3
            and phase in _COLD_L2_OUTSTANDING_PHASES
            and ticket.evidence_origin in _COLD_TRAINING_ORIGINS
            else ""
        )
        reservation_open = bool(state.reservation_open)
        confirmation = bool(
            state.reward_flight_open
            and state.reward_flight_scope == "confirmation"
        )
        continued = bool(state.fast_continuation_accepted)
        return (
            (
                "terminal"
                if phase.startswith("terminal")
                else "reserved"
                if phase == "reserved"
                else "active"
            ),
            cold_l2_key,
            reservation_open,
            bool(reservation_open and state.replay_discovery_key),
            phase in _FEEDBACK_ONLY_PHASES,
            confirmation and not continued,
            confirmation and continued,
            bool(
                not state.handoff_credit_released
                and phase in _HANDOFF_OWNING_PHASES
            ),
        )

    @staticmethod
    def _toggle_ticket_id(
        members: dict[int, None],
        ticket_id: int,
        delta: int,
    ) -> None:
        if delta > 0:
            members[ticket_id] = None
        else:
            members.pop(ticket_id, None)

    def _cold_l2_key_visible_unlocked(self, cohort_key: str) -> bool:
        return (
            cohort_key in self._cold_l2_outstanding
            or cohort_key in self._cold_unknown_l2_queued_inventory
        )

    def _recount_cold_l2_visible_unlocked(self) -> None:
        self._cold_l2_visible_count = len(
            {
                cohort_key
                for cohort_key, count in (
                    self._cold_unknown_l2_queued_inventory.items()
                )
                if int(count) > 0
            }
            | set(self._cold_l2_outstanding)
        )

    def _adjust_cold_l2_outstanding_unlocked(
        self,
        cohort_key: str,
        delta: int,
    ) -> None:
        was_visible = self._cold_l2_key_visible_unlocked(cohort_key)
        self._bump_count(self._cold_l2_outstanding, cohort_key, delta)
        self._cold_l2_outstanding_total += delta
        self._cold_l2_visible_count += int(
            self._cold_l2_key_visible_unlocked(cohort_key)
        ) - int(was_visible)

    def _apply_ticket_signature_unlocked(
        self,
        ticket_id: int,
        ticket: Tier3CandidateFeedbackTicket,
        signature: tuple[Any, ...],
        delta: int,
    ) -> None:
        (
            phase_class,
            cold_l2_key,
            reservation_open,
            replay_open,
            feedback_only,
            confirmation_fast,
            confirmation_deep,
            handoff_owning,
        ) = signature
        if cold_l2_key:
            self._adjust_cold_l2_outstanding_unlocked(cold_l2_key, delta)
        if reservation_open:
            self._toggle_ticket_id(
                self._open_reservation_ticket_ids,
                ticket_id,
                delta,
            )
        if replay_open:
            self._replay_discovery_open_count += delta
        if feedback_only:
            self._feedback_only_ticket_count += delta
        if confirmation_fast:
            self._confirmation_fast_pending_ticket_count += delta
        if confirmation_deep:
            self._confirmation_deep_pending_ticket_count += delta
        if handoff_owning:
            self._toggle_ticket_id(
                self._handoff_owning_ticket_ids,
                ticket_id,
                delta,
            )
        if phase_class == "terminal":
            self._toggle_ticket_id(self._terminal_ticket_ids, ticket_id, delta)
            return
        cohort_keys, service_keys, root_family = (
            self._ticket_cohort_projection(ticket)
        )
        counts = (
            self._ticket_cohort_reserved
            if phase_class == "reserved"
            else self._ticket_cohort_active
        )
        for cohort_key in cohort_keys:
            self._bump_count(counts, cohort_key, delta)
        for cohort_key in service_keys:
            self._bump_nested_count(
                self._ticket_service_live_roots,
                cohort_key,
                root_family,
                delta,
            )

    def _apply_ticket_membership_unlocked(
        self,
        ticket: Tier3CandidateFeedbackTicket,
        delta: int,
    ) -> None:
        cohort_keys, _service_keys, root_family = (
            self._ticket_cohort_projection(ticket)
        )
        for cohort_key in cohort_keys:
            self._bump_nested_count(
                self._ticket_cohort_roots,
                cohort_key,
                root_family,
                delta,
            )

    def _reindex_ticket_unlocked(self, state: _TicketState) -> None:
        """Refresh the projections after a field in the signature changed."""

        ticket_id = state.ticket.ticket_id
        if self._tickets.get(ticket_id) is not state:
            return
        signature = self._ticket_index_signature(state)
        previous = self._ticket_index_signatures.get(ticket_id)
        if previous == signature:
            return
        if previous is None:
            self._apply_ticket_membership_unlocked(state.ticket, 1)
        else:
            self._apply_ticket_signature_unlocked(
                ticket_id,
                state.ticket,
                previous,
                -1,
            )
        self._apply_ticket_signature_unlocked(
            ticket_id,
            state.ticket,
            signature,
            1,
        )
        self._ticket_index_signatures[ticket_id] = signature

    def _forget_ticket_unlocked(self, ticket_id: int) -> None:
        state = self._tickets.pop(ticket_id, None)
        previous = self._ticket_index_signatures.pop(ticket_id, None)
        if state is None or previous is None:
            return
        self._apply_ticket_signature_unlocked(
            ticket_id,
            state.ticket,
            previous,
            -1,
        )
        self._apply_ticket_membership_unlocked(state.ticket, -1)

    def _debug_ticket_index_mismatches_unlocked(self) -> list[str]:
        """Compare every incremental projection with its reference scan.

        Debug and test use only; it rescans the whole ticket table and has
        no side effects on scheduling state.
        """

        mismatches: list[str] = []

        def check(name: str, actual: Any, expected: Any) -> None:
            if actual != expected:
                mismatches.append(f"{name}: {actual!r} != {expected!r}")

        check(
            "signature_ids",
            set(self._ticket_index_signatures),
            set(self._tickets),
        )
        for ticket_id, state in self._tickets.items():
            check(
                f"signature[{ticket_id}]",
                self._ticket_index_signatures.get(ticket_id),
                self._ticket_index_signature(state),
            )
        check(
            "cold_l2_outstanding",
            dict(self._cold_l2_outstanding),
            self._cold_l2_training_outstanding_bruteforce_unlocked(),
        )
        check(
            "cold_l2_visible_count",
            self._cold_l2_visible_count,
            self._cold_l2_training_cohort_count_bruteforce_unlocked(),
        )
        check(
            "open_reservation_ids",
            set(self._open_reservation_ticket_ids),
            {
                ticket_id
                for ticket_id, state in self._tickets.items()
                if state.reservation_open
            },
        )
        check(
            "terminal_ids",
            set(self._terminal_ticket_ids),
            {
                ticket_id
                for ticket_id, state in self._tickets.items()
                if state.phase.startswith("terminal")
            },
        )
        check(
            "handoff_owning_ids",
            sorted(self._handoff_owning_ticket_ids),
            sorted(self._handoff_owning_ticket_ids_bruteforce_unlocked()),
        )
        for fast_name, reference_name in (
            _TICKET_PROJECTION_BRUTEFORCE_METHODS.items()
        ):
            if fast_name in {
                "_cohort_inventory_unlocked",
                "_cohort_support_strata_unlocked",
                "_reserved_parent_discovery_child_keys_unlocked",
                "_stale_terminal_ticket_ids_unlocked",
            }:
                continue
            fast = getattr(self, fast_name)()
            reference = getattr(self, reference_name)()
            if fast_name == "_handoff_owning_ticket_ids_unlocked":
                fast, reference = sorted(fast), sorted(reference)
            check(fast_name, fast, reference)
        reference_roots: dict[str, dict[tuple[str, str], int]] = {}
        reference_reserved: dict[str, int] = {}
        reference_active: dict[str, int] = {}
        reference_live_roots: dict[str, dict[tuple[str, str], int]] = {}
        parent_keys: set[str] = set()
        for state in self._tickets.values():
            cohort_keys, service_keys, root_family = (
                self._ticket_cohort_projection(state.ticket)
            )
            for cohort_key in cohort_keys:
                self._bump_nested_count(
                    reference_roots,
                    cohort_key,
                    root_family,
                    1,
                )
            if (
                state.reservation_open
                and state.ticket.reward_cohort_state == "parent_discovery"
            ):
                parent_keys.add(str(state.ticket.reward_cohort_key or ""))
            if state.phase.startswith("terminal"):
                continue
            counts = (
                reference_reserved
                if state.phase == "reserved"
                else reference_active
            )
            for cohort_key in cohort_keys:
                self._bump_count(counts, cohort_key, 1)
            for cohort_key in service_keys:
                self._bump_nested_count(
                    reference_live_roots,
                    cohort_key,
                    root_family,
                    1,
                )
        check("cohort_roots", self._ticket_cohort_roots, reference_roots)
        check("cohort_reserved", self._ticket_cohort_reserved, reference_reserved)
        check("cohort_active", self._ticket_cohort_active, reference_active)
        check(
            "service_live_roots",
            self._ticket_service_live_roots,
            reference_live_roots,
        )
        # The per-key reference scans are O(tickets) each; the aggregate
        # comparison above already covers every key, so sample the formulas.
        cohort_keys = sorted(
            set(self._cohort_yield_states)
            | set(reference_roots)
            | set(self._ticket_cohort_roots)
        )
        if len(cohort_keys) * max(1, len(self._tickets)) > 2048:
            stride = max(1, len(cohort_keys) // 6)
            cohort_keys = cohort_keys[::stride]
        for cohort_key in sorted(set(cohort_keys) | parent_keys):
            check(
                f"cohort_inventory[{cohort_key}]",
                self._cohort_inventory_unlocked(cohort_key),
                self._cohort_inventory_bruteforce_unlocked(cohort_key),
            )
            yield_state = self._cohort_yield_states.get(cohort_key)
            if yield_state is None:
                yield_state = _CohortYieldState(scope="service")
            check(
                f"cohort_support_strata[{cohort_key}]",
                self._cohort_support_strata_unlocked(cohort_key, yield_state),
                self._cohort_support_strata_bruteforce_unlocked(
                    cohort_key,
                    yield_state,
                ),
            )
            check(
                f"reserved_parent_discovery_child_keys[{cohort_key}]",
                self._reserved_parent_discovery_child_keys_unlocked(cohort_key),
                self._reserved_parent_discovery_child_keys_bruteforce_unlocked(
                    cohort_key
                ),
            )
        for cutoff in (float("inf"), time.monotonic() - 120.0):
            check(
                f"stale_terminal_ids[{cutoff}]",
                self._stale_terminal_ticket_ids_unlocked(cutoff),
                self._stale_terminal_ticket_ids_bruteforce_unlocked(cutoff),
            )
        return mismatches

    def _debug_cohort_index_mismatches_unlocked(self) -> list[str]:
        """Compare the cohort projections with their reference scans.

        Debug and test use only. A pending rebuild - a cohort appended
        without registration, or a state object stored at an existing key, a
        deletion or another rewrite of the dict - is reported first as
        ``cohort_index_registered``; the remaining checks then run on the
        rebuilt index, which is what every indexed read would have used.
        """

        mismatches: list[str] = []

        def check(name: str, actual: Any, expected: Any) -> None:
            if actual != expected:
                mismatches.append(f"{name}: {actual!r} != {expected!r}")

        check(
            "cohort_index_registered",
            (
                bool(self._cohort_index_stale_unlocked()),
                len(self._cohort_state_ordinals),
            ),
            (False, len(self._cohort_yield_states)),
        )
        self._ensure_cohort_index_unlocked()
        states = self._cohort_yield_states
        check(
            "cohort_ordinals",
            self._cohort_state_ordinals,
            {cohort_key: index for index, cohort_key in enumerate(states)},
        )
        check(
            "cohort_state_ids",
            self._cohort_key_by_state_id,
            {id(state): cohort_key for cohort_key, state in states.items()},
        )
        reference_buckets: dict[str, set[str]] = defaultdict(set)
        for cohort_key, state in states.items():
            reference_buckets[state.eligibility].add(cohort_key)
        check(
            "cohort_eligibility_buckets",
            {
                eligibility: set(keys)
                for eligibility, keys in self._cohort_keys_by_eligibility.items()
                if keys
            },
            dict(reference_buckets),
        )
        check(
            "similar_scope_cohort_keys",
            list(self._similar_scope_cohort_keys),
            [
                cohort_key
                for cohort_key, state in states.items()
                if state.scope == "similar"
            ],
        )
        for name in _COHORT_OUTSTANDING_COUNTERS:
            missing = sorted(
                cohort_key
                for cohort_key, state in states.items()
                if int(getattr(state, name)) > 0
                and cohort_key not in self._cohort_counter_keys[name]
            )
            check(f"cohort_counter_keys[{name}]", missing, [])
            for scope in ("service", "similar"):
                check(
                    f"cohort_counter_total[{name},{scope}]",
                    self._cohort_counter_total_unlocked(name, scope),
                    self._cohort_counter_total_bruteforce_unlocked(name, scope),
                )
        for eligibilities in (
            tuple(Tier3ServiceCohortEligibility),
            (Tier3ServiceCohortEligibility.PARENT_DISCOVERY,),
            (Tier3ServiceCohortEligibility.LEAF_CONFIRMATION,),
            (
                Tier3ServiceCohortEligibility.PROVEN_75,
                Tier3ServiceCohortEligibility.PROVEN_50,
                Tier3ServiceCohortEligibility.LEAF_PROBATION,
                Tier3ServiceCohortEligibility.PARENT_PROVEN,
                Tier3ServiceCohortEligibility.PARENT_PROBATION,
            ),
        ):
            check(
                "cohort_items_with_eligibility["
                + ",".join(str(item.value) for item in eligibilities)
                + "]",
                [
                    (cohort_key, id(state))
                    for cohort_key, state in (
                        self._cohort_items_with_eligibility_unlocked(
                            eligibilities
                        )
                    )
                    if state.eligibility in eligibilities
                ],
                [
                    (cohort_key, id(state))
                    for cohort_key, state in (
                        self._cohort_items_with_eligibility_bruteforce_unlocked(
                            eligibilities
                        )
                    )
                    if state.eligibility in eligibilities
                ],
            )
        check(
            "similar_cohort_items",
            [
                (cohort_key, id(state))
                for cohort_key, state in self._similar_cohort_items_unlocked()
                if state.scope == "similar"
            ],
            [
                (cohort_key, id(state))
                for cohort_key, state in (
                    self._similar_cohort_items_bruteforce_unlocked()
                )
                if state.scope == "similar"
            ],
        )
        check(
            "cold_leaf_parent_links",
            self._cold_leaf_parent_links_unlocked(),
            self._cold_leaf_parent_links_bruteforce_unlocked(),
        )
        return mismatches

    def debug_verify_ticket_indices(self) -> None:
        """Raise ``AssertionError`` if any incremental projection drifted.

        Covers the ticket projections and the cohort projections.
        """

        with self._lock:
            mismatches = self._debug_ticket_index_mismatches_unlocked()
            mismatches.extend(self._debug_cohort_index_mismatches_unlocked())
        if mismatches:
            raise AssertionError(
                "scheduler ticket projections drifted: "
                + "; ".join(mismatches[:16])
            )

    def _index_cohort_state_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> None:
        self._cohort_state_ordinals[cohort_key] = len(self._cohort_state_ordinals)
        self._cohort_key_by_state_id[id(state)] = cohort_key
        self._cohort_keys_by_eligibility.setdefault(state.eligibility, {})[
            cohort_key
        ] = None
        if state.scope == "similar":
            self._similar_scope_cohort_keys[cohort_key] = None
        for name, keys in self._cohort_counter_keys.items():
            if int(getattr(state, name)) > 0:
                keys[cohort_key] = None

    def _cohort_index_stale_unlocked(self) -> bool:
        """Whether the cohort projections need a rebuild before the next read.

        True after an explicit ``_cohort_index_dirty`` and after any rewrite
        of ``_cohort_yield_states`` other than an append (see
        ``_CohortYieldStateMap``). An unregistered append is caught by the
        length check in ``_ensure_cohort_index_unlocked``.
        """

        return (
            self._cohort_index_dirty
            or self._cohort_index_rewrite_version
            != self._cohort_yield_states.rewrite_version
        )

    def _register_cohort_state_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> None:
        """Index a cohort just inserted at the end of ``_cohort_yield_states``."""

        if (
            self._cohort_index_stale_unlocked()
            or cohort_key in self._cohort_state_ordinals
            or len(self._cohort_state_ordinals) + 1
            != len(self._cohort_yield_states)
            or self._cohort_yield_states.get(cohort_key) is not state
        ):
            self._cohort_index_dirty = True
            return
        self._index_cohort_state_unlocked(cohort_key, state)

    def _ensure_cohort_index_unlocked(self) -> None:
        states = self._cohort_yield_states
        if (
            not self._cohort_index_dirty
            and self._cohort_index_rewrite_version == states.rewrite_version
            and len(self._cohort_state_ordinals) == len(states)
        ):
            return
        self._cohort_state_ordinals.clear()
        self._cohort_key_by_state_id.clear()
        self._cohort_keys_by_eligibility.clear()
        self._similar_scope_cohort_keys.clear()
        for keys in self._cohort_counter_keys.values():
            keys.clear()
        for cohort_key, state in states.items():
            self._index_cohort_state_unlocked(cohort_key, state)
        self._cohort_index_rewrite_version = states.rewrite_version
        self._cohort_index_dirty = False

    def _move_cohort_eligibility_unlocked(
        self,
        state: _CohortYieldState,
        previous: Tier3ServiceCohortEligibility,
    ) -> None:
        """Re-bucket ``state`` after ``_set_service_eligibility_unlocked``."""

        if previous == state.eligibility or self._cohort_index_stale_unlocked():
            return
        cohort_key = self._cohort_key_by_state_id.get(id(state))
        if (
            cohort_key is None
            or self._cohort_yield_states.get(cohort_key) is not state
        ):
            self._cohort_index_dirty = True
            return
        bucket = self._cohort_keys_by_eligibility.get(previous)
        if bucket is not None:
            bucket.pop(cohort_key, None)
        self._cohort_keys_by_eligibility.setdefault(state.eligibility, {})[
            cohort_key
        ] = None

    def _cohort_items_with_eligibility_unlocked(
        self,
        eligibilities: Iterable[Tier3ServiceCohortEligibility],
    ) -> list[tuple[str, _CohortYieldState]]:
        """Cohorts whose eligibility is listed, in ``_cohort_yield_states`` order.

        Callers keep their full filter; this only skips cohorts it cannot
        match, so it is interchangeable with the reference full iteration.
        """

        self._ensure_cohort_index_unlocked()
        keys: list[str] = []
        for eligibility in dict.fromkeys(eligibilities):
            keys.extend(self._cohort_keys_by_eligibility.get(eligibility, ()))
        if len(keys) > 1:
            keys.sort(key=self._cohort_state_ordinals.__getitem__)
        states = self._cohort_yield_states
        return [(cohort_key, states[cohort_key]) for cohort_key in keys]

    def _cohort_items_with_eligibility_bruteforce_unlocked(
        self,
        eligibilities: Iterable[Tier3ServiceCohortEligibility],
    ) -> list[tuple[str, _CohortYieldState]]:
        del eligibilities
        return list(self._cohort_yield_states.items())

    def _similar_cohort_items_unlocked(
        self,
    ) -> list[tuple[str, _CohortYieldState]]:
        """Similar-scope cohorts in ``_cohort_yield_states`` order."""

        self._ensure_cohort_index_unlocked()
        states = self._cohort_yield_states
        return [
            (cohort_key, states[cohort_key])
            for cohort_key in self._similar_scope_cohort_keys
        ]

    def _similar_cohort_items_bruteforce_unlocked(
        self,
    ) -> list[tuple[str, _CohortYieldState]]:
        return list(self._cohort_yield_states.items())

    def _note_cohort_counter_unlocked(self, name: str, cohort_key: str) -> None:
        """Record that ``name`` of ``cohort_key`` was just incremented."""

        if not self._cohort_index_stale_unlocked():
            self._cohort_counter_keys[name][cohort_key] = None

    def _cohort_counter_total_unlocked(
        self,
        name: str,
        scope: str = "service",
    ) -> int:
        """Sum of ``max(0, counter)`` over the cohorts of one scope."""

        self._ensure_cohort_index_unlocked()
        keys = self._cohort_counter_keys[name]
        states = self._cohort_yield_states
        total = 0
        drained: list[str] = []
        for cohort_key in keys:
            state = states.get(cohort_key)
            value = 0 if state is None else int(getattr(state, name))
            if value <= 0:
                drained.append(cohort_key)
            elif state.scope == scope:
                total += value
        for cohort_key in drained:
            keys.pop(cohort_key, None)
        return total

    def _cohort_counter_total_bruteforce_unlocked(
        self,
        name: str,
        scope: str = "service",
    ) -> int:
        return sum(
            max(0, int(getattr(state, name)))
            for state in self._cohort_yield_states.values()
            if state.scope == scope
        )

    def _cold_leaf_parent_links_unlocked(
        self,
    ) -> tuple[Mapping[str, str], frozenset[str]]:
        """Cached ``_cold_leaf_parent_links_bruteforce_unlocked``; read-only."""

        cache = self._cold_leaf_parent_links_cache
        version = self._cohort_direct_children_version
        if cache is None or cache[0] != version:
            parent_by_child, ambiguous_children = (
                self._cold_leaf_parent_links_bruteforce_unlocked()
            )
            cache = (version, parent_by_child, ambiguous_children)
            self._cold_leaf_parent_links_cache = cache
        return cache[1], cache[2]

    def _cold_leaf_parent_links_bruteforce_unlocked(
        self,
    ) -> tuple[dict[str, str], frozenset[str]]:
        """Last queued parent of each child, and children with several."""

        parent_by_child: dict[str, str] = {}
        ambiguous_children: set[str] = set()
        for parent, children in self._cohort_queued_direct_children.items():
            for child in children:
                if child in parent_by_child and parent_by_child[child] != parent:
                    ambiguous_children.add(child)
                parent_by_child[child] = parent
        return parent_by_child, frozenset(ambiguous_children)

    def _cohort_inventory_unlocked(self, cohort_key: str) -> dict[str, int]:
        key = str(cohort_key or "")
        roots = set(self._cohort_queued_buckets.get(key, {}))
        roots.update(self._ticket_cohort_roots.get(key, ()))
        return {
            "reservable": max(
                0,
                int(self._cohort_queued_inventory.get(key, 0)),
            ),
            "reserved": int(self._ticket_cohort_reserved.get(key, 0)),
            "active": int(self._ticket_cohort_active.get(key, 0)),
            "root_family_count": len(roots),
        }

    def _cohort_inventory_bruteforce_unlocked(
        self,
        cohort_key: str,
    ) -> dict[str, int]:
        key = str(cohort_key or "")
        queued = max(0, int(self._cohort_queued_inventory.get(key, 0)))
        roots = set(self._cohort_queued_buckets.get(key, {}))
        reserved = 0
        active = 0
        for ticket_state in self._tickets.values():
            ticket_cohorts = set(ticket_state.ticket.service_cohort_keys)
            if ticket_state.ticket.similar_cohort_key:
                ticket_cohorts.add(ticket_state.ticket.similar_cohort_key)
            if key not in ticket_cohorts:
                continue
            roots.add(
                (
                    ticket_state.ticket.source_root,
                    ticket_state.ticket.family,
                )
            )
            if ticket_state.phase == "reserved":
                reserved += 1
            elif not ticket_state.phase.startswith("terminal"):
                active += 1
        return {
            "reservable": queued,
            "reserved": reserved,
            "active": active,
            "root_family_count": len(roots),
        }

    def _cohort_support_strata_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> int:
        """Indexed form of ``_cohort_support_strata_bruteforce_unlocked``."""

        key = str(cohort_key or "")
        strata = set(state.current_pass_root_families)
        strata.update(
            f"{source_root}\x1f{family}"
            for source_root, family in self._cohort_queued_buckets.get(
                key,
                {},
            )
        )
        strata.update(
            f"{source_root}\x1f{family}"
            for source_root, family in self._ticket_service_live_roots.get(
                key,
                {},
            )
        )
        return len(strata)

    def _cohort_support_strata_bruteforce_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> int:
        """Count independent evidence/inventory strata for one cohort.

        A single successful candidate should not immediately fan out four
        production flights when every remaining candidate comes from the same
        root/family. Conversely, the same exact L3 structure independently
        present in another current source is useful confirmation and should
        not wait for a redundant serial probe. This remains source-agnostic:
        only opaque root/family identities participate.
        """

        key = str(cohort_key or "")
        strata = set(state.current_pass_root_families)
        strata.update(
            f"{source_root}\x1f{family}"
            for source_root, family in self._cohort_queued_buckets.get(
                key,
                {},
            )
        )
        for ticket_state in self._tickets.values():
            if key not in set(ticket_state.ticket.service_cohort_keys):
                continue
            if ticket_state.phase.startswith("terminal"):
                continue
            strata.add(
                f"{ticket_state.ticket.source_root}\x1f"
                f"{ticket_state.ticket.family}"
            )
        return len(strata)

    def _production_similar_cohorts_with_inventory_unlocked(
        self,
    ) -> list[tuple[str, _CohortYieldState]]:
        # Similarity cohorts remain a shadow signal in the production model.
        # The retained legacy path is callable only by explicit parity tests.
        if not self._legacy_structural_routing_for_test:
            return []
        eligible: list[tuple[str, _CohortYieldState]] = []
        for cohort_key, state in self._cohort_yield_states.items():
            if (
                state.scope != "similar"
                or not state.promoted
                or int(self._cohort_queued_inventory.get(cohort_key, 0)) <= 0
                or not self._similar_dispatch_quota_open_unlocked(cohort_key)
            ):
                continue
            eligible.append((cohort_key, state))
        eligible.sort(
            key=lambda item: (
                0 if item[1].lifecycle_state == "similar_proven" else 1,
                int(item[1].production_selection_total)
                + int(self._reserved_similar_handoffs.get(item[0], 0)),
                self._cohort_stable_sequence_unlocked(item[1]),
            )
        )
        return eligible

    def _cohort_stable_sequence_unlocked(
        self,
        state: _CohortYieldState,
    ) -> int:
        """Return a process-local order derived from evidence arrival.

        Cohort identifiers are session HMACs. Using their lexical value as a
        production tie-break randomized otherwise identical scheduling across
        processes. Evidence order is opaque in reports but stable for the
        actual candidate stream.
        """

        if int(state.first_evidence_sequence) <= 0:
            self._next_cohort_evidence_sequence += 1
            state.first_evidence_sequence = int(
                self._next_cohort_evidence_sequence
            )
        return int(state.first_evidence_sequence)

    def _production_service_cohorts_with_inventory_unlocked(
        self,
    ) -> list[tuple[str, _CohortYieldState]]:
        priority = (
            {
                Tier3ServiceCohortEligibility.PROVEN_75: 0,
                Tier3ServiceCohortEligibility.PROVEN_50: 1,
                Tier3ServiceCohortEligibility.PARENT_PROVEN: 2,
                Tier3ServiceCohortEligibility.LEAF_PROBATION: 3,
                Tier3ServiceCohortEligibility.PARENT_PROBATION: 4,
            }
            if self._legacy_structural_routing_for_test
            else {
                Tier3ServiceCohortEligibility.PROVEN_75: 0,
                Tier3ServiceCohortEligibility.PROVEN_50: 1,
                Tier3ServiceCohortEligibility.LEAF_PROBATION: 2,
            }
        )
        eligible: list[tuple[str, _CohortYieldState]] = []
        for cohort_key, state in self._cohort_items_with_eligibility_unlocked(
            priority
        ):
            eligibility = self._service_eligibility_unlocked(state)
            if eligibility not in priority:
                continue
            if (
                not self._legacy_structural_routing_for_test
                and not state.leaf_eligible
            ):
                continue
            inventory = (
                self._cohort_queued_inventory
                if self._legacy_structural_routing_for_test
                else self._cohort_cold_queued_inventory
            )
            if int(inventory.get(cohort_key, 0)) <= 0:
                continue
            if not self._service_dispatch_quota_open_unlocked(cohort_key):
                continue
            eligible.append((cohort_key, state))

        eligible.sort(
            key=lambda item: (
                (
                    -int(item[1].hierarchy_level)
                    if self._legacy_structural_routing_for_test
                    else 0
                ),
                priority[self._service_eligibility_unlocked(item[1])],
                -float(
                    self._wilson_bound(
                        int(item[1].production_net_new_total),
                        int(item[1].production_resolved_total),
                        lower=True,
                    )
                ),
                int(item[1].production_selection_total)
                + int(self._reserved_service_handoffs.get(item[0], 0)),
                self._cohort_stable_sequence_unlocked(item[1]),
            )
        )
        return eligible

    @staticmethod
    def _candidate_service_cohort_keys(
        candidate: ScheduledTier3Candidate,
    ) -> tuple[str, ...]:
        return tuple(
            f"service:{profile}"
            for profile in candidate.feature_key.ordered_service_profiles()
        )

    @staticmethod
    def _candidate_l2_cohort_key(
        candidate: ScheduledTier3Candidate,
    ) -> str:
        # Same value as _candidate_service_cohort_keys(candidate)[2].
        profiles = candidate.feature_key.ordered_service_profiles()
        return f"service:{profiles[2]}" if len(profiles) >= 3 else ""

    def _rebuild_cold_unknown_l2_inventory_unlocked(self) -> None:
        self._cold_unknown_l2_queued_inventory.clear()
        for entry in self._lookahead_index.entries():
            if entry.visibility_class != Tier3CandidateVisibilityIndex.UNKNOWN:
                continue
            cohort_key = self._candidate_l2_cohort_key(entry.candidate)
            if cohort_key:
                self._cold_unknown_l2_queued_inventory[cohort_key] = (
                    int(
                        self._cold_unknown_l2_queued_inventory.get(
                            cohort_key,
                            0,
                        )
                    )
                    + 1
                )
        self._recount_cold_l2_visible_unlocked()

    def _record_cold_unknown_l2_inventory_unlocked(
        self,
        candidate: ScheduledTier3Candidate,
        delta: int,
    ) -> None:
        if (
            str(candidate.visibility_class or "unknown")
            != Tier3CandidateVisibilityIndex.UNKNOWN
        ):
            return
        cohort_key = self._candidate_l2_cohort_key(candidate)
        if not cohort_key:
            return
        was_visible = self._cold_l2_key_visible_unlocked(cohort_key)
        next_total = max(
            0,
            int(self._cold_unknown_l2_queued_inventory.get(cohort_key, 0))
            + int(delta),
        )
        if next_total:
            self._cold_unknown_l2_queued_inventory[cohort_key] = next_total
        else:
            self._cold_unknown_l2_queued_inventory.pop(cohort_key, None)
        self._cold_l2_visible_count += int(
            self._cold_l2_key_visible_unlocked(cohort_key)
        ) - int(was_visible)

    def _cold_l2_training_outstanding_unlocked(self) -> Mapping[str, int]:
        """Per-L2 cold outstanding counts (reserved, tcp_active, feedback_only).

        Returns the live incremental mapping; callers must not mutate it.
        This method and ``_cold_l2_training_limit_unlocked`` are the policy
        hooks: every cold-L2 read goes through them, and the built-in versions
        answer in O(1) when handed this live mapping. An audit harness that
        replaces either hook (on the class or on one instance) therefore still
        governs the per-L2 check, the limit, the high-water mark and the
        reported fields, exactly as before the counters existed.
        """

        return self._cold_l2_outstanding

    def _cold_l2_training_outstanding_total_unlocked(self) -> int:
        outstanding = self._cold_l2_training_outstanding_unlocked()
        if outstanding is self._cold_l2_outstanding:
            return int(self._cold_l2_outstanding_total)
        return sum(outstanding.values())

    def _cold_l2_training_outstanding_total_bruteforce_unlocked(self) -> int:
        return sum(self._cold_l2_training_outstanding_bruteforce_unlocked().values())

    def _cold_l2_training_cohort_count_unlocked(self) -> int:
        outstanding = self._cold_l2_training_outstanding_unlocked()
        if outstanding is self._cold_l2_outstanding:
            return int(self._cold_l2_visible_count)
        return len(
            {
                *self._cold_unknown_l2_queued_inventory,
                *outstanding,
            }
        )

    def _cold_l2_training_cohort_count_bruteforce_unlocked(self) -> int:
        return len(
            {
                *self._cold_unknown_l2_queued_inventory,
                *self._cold_l2_training_outstanding_bruteforce_unlocked(),
            }
        )

    def _cold_l2_training_current_limit_unlocked(
        self,
        outstanding: Mapping[str, int] | None = None,
    ) -> int:
        """Current per-L2 cold training limit, through the policy hooks.

        ``outstanding`` is the caller's ``_cold_l2_training_outstanding_unlocked``
        result, passed on so a replaced hook is evaluated only once.
        """

        return self._cold_l2_training_limit_unlocked(
            self._cold_l2_training_outstanding_unlocked()
            if outstanding is None
            else outstanding
        )

    def _cold_l2_training_current_limit_bruteforce_unlocked(
        self,
        outstanding: Mapping[str, int] | None = None,
    ) -> int:
        # The reference always rescans the tickets.
        del outstanding
        return self._cold_l2_training_limit_unlocked(
            self._cold_l2_training_outstanding_bruteforce_unlocked()
        )

    def _cold_l2_training_outstanding_bruteforce_unlocked(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        cold_origins = {
            Tier3CandidateEvidenceOrigin.COLD_UNKNOWN.value,
            Tier3CandidateEvidenceOrigin.COLD_DISCOVERY.value,
        }
        for ticket_state in self._tickets.values():
            if ticket_state.phase not in {
                "reserved",
                "tcp_active",
                "feedback_only",
            }:
                continue
            ticket = ticket_state.ticket
            if ticket.evidence_origin not in cold_origins:
                continue
            cohort_key = (
                ticket.service_cohort_keys[2]
                if len(ticket.service_cohort_keys) >= 3
                else ""
            )
            if cohort_key:
                counts[cohort_key] += 1
        return dict(counts)

    def _cold_l2_training_capacity_unlocked(self) -> int:
        """The budget the cold-L2 training limit shares between L2 cohorts.

        At the default breadth this is the active validator identity count.
        The limit is expressed in identities because, at breadth 1.0, the
        TCP pipeline the cold samples enter is exactly that wide: the owner
        sizes its TCP executor and PI in-flight bounds from the identity
        count. Opt-in TCP breadth widens that pipeline to
        ``tier3_tcp_breadth_scaled_limit(identities, breadth)``, the same
        scaling the owner applies, so each L2 keeps the same share of it.
        """

        return tier3_tcp_breadth_scaled_limit(
            max(1, int(self._active_helper_count)),
            self._cold_l2_training_breadth,
        )

    def _cold_l2_training_limit_for_cohorts_unlocked(self, visible: int) -> int:
        """``min(W, max(4, ceil(W / cohorts)))`` with W the capacity above.

        The floor of four pending samples per L2 is absolute and does not
        scale; the fair share ``ceil(W / cohorts)`` does.
        """

        helpers = self._active_helper_count
        memo = self._cold_l2_limit_memo
        if memo[0] == helpers and memo[1] == visible:
            return memo[2]
        capacity = self._cold_l2_training_capacity_unlocked()
        cohort_count = max(1, int(visible))
        limit = min(
            capacity,
            max(4, int(math.ceil(capacity / float(cohort_count)))),
        )
        self._cold_l2_limit_memo = (helpers, visible, limit)
        return limit

    def _cold_l2_training_limit_unlocked(
        self,
        outstanding: Mapping[str, int],
    ) -> int:
        if outstanding is self._cold_l2_outstanding:
            # The live mapping: its visible-cohort count is kept
            # incrementally, so the per-entry check stays O(1).
            return self._cold_l2_training_limit_for_cohorts_unlocked(
                self._cold_l2_visible_count
            )
        visible_cohorts = {
            cohort_key
            for cohort_key, count in self._cold_unknown_l2_queued_inventory.items()
            if int(count) > 0
        }
        visible_cohorts.update(
            cohort_key
            for cohort_key, count in outstanding.items()
            if int(count) > 0
        )
        return self._cold_l2_training_limit_for_cohorts_unlocked(
            len(visible_cohorts)
        )

    def _cold_unknown_entry_dispatchable_unlocked(
        self,
        entry: _Tier3CandidateLookaheadEntry,
    ) -> bool:
        if entry.visibility_class != Tier3CandidateVisibilityIndex.UNKNOWN:
            return True
        cohort_key = self._candidate_l2_cohort_key(entry.candidate)
        if not cohort_key:
            return True
        outstanding = self._cold_l2_training_outstanding_unlocked()
        limit = self._cold_l2_training_limit_unlocked(outstanding)
        if int(outstanding.get(cohort_key, 0)) < limit:
            return True
        self._cold_l2_training_deferred_total += 1
        return False

    @classmethod
    def _candidate_similar_cohort_key(
        cls,
        candidate: ScheduledTier3Candidate,
    ) -> str:
        return f"similar:{cls._feature_signature(candidate.feature_key)}"

    def _record_candidate_cohort_inventory_unlocked(
        self,
        candidate: ScheduledTier3Candidate,
        *,
        arm_id: str,
        delta: int,
    ) -> bool:
        bucket = (candidate.source_root, candidate.family)
        exact_key = (candidate.arm_key, candidate.feature_key)
        exact_count = self._exact_queued_inventory.get(exact_key, 0) + int(delta)
        if exact_count > 0:
            self._exact_queued_inventory[exact_key] = exact_count
        else:
            self._exact_queued_inventory.pop(exact_key, None)
        self._record_cold_unknown_l2_inventory_unlocked(candidate, delta)
        service_cohort_keys = self._candidate_service_cohort_keys(candidate)
        cohort_keys = (
            *service_cohort_keys,
            self._candidate_similar_cohort_key(candidate),
        )
        reevaluate_discovery = False
        for cohort_key in cohort_keys:
            visibility_index = (
                self._cohort_unknown_queued_arms
                if candidate.visibility_class == Tier3CandidateVisibilityIndex.UNKNOWN
                else self._cohort_tcp_only_queued_arms
                if candidate.visibility_class == Tier3CandidateVisibilityIndex.TCP_ONLY
                else None
            )
            if visibility_index is not None:
                visibility_arms = visibility_index.setdefault(cohort_key, {})
                visibility_count = visibility_arms.get(arm_id, 0) + int(delta)
                if visibility_count > 0:
                    visibility_arms[arm_id] = visibility_count
                else:
                    visibility_arms.pop(arm_id, None)
                if not visibility_arms:
                    visibility_index.pop(cohort_key, None)
            if self._candidate_can_confirm_leaf(candidate):
                cold_count = self._cohort_cold_queued_inventory.get(cohort_key, 0) + int(delta)
                if cold_count > 0:
                    self._cohort_cold_queued_inventory[cohort_key] = cold_count
                else:
                    self._cohort_cold_queued_inventory.pop(cohort_key, None)
            if int(delta) > 0:
                sequence = max(1, int(candidate.lookahead_sequence or 1))
                self._cohort_first_inventory_sequence[cohort_key] = min(
                    sequence,
                    int(
                        self._cohort_first_inventory_sequence.get(
                            cohort_key,
                            sequence,
                        )
                    ),
                )
            next_total = max(
                0,
                int(self._cohort_queued_inventory.get(cohort_key, 0)) + int(delta),
            )
            if next_total:
                self._cohort_queued_inventory[cohort_key] = next_total
            else:
                self._cohort_queued_inventory.pop(cohort_key, None)

            bucket_counts = self._cohort_queued_buckets.setdefault(cohort_key, {})
            next_bucket_total = max(
                0,
                int(bucket_counts.get(bucket, 0)) + int(delta),
            )
            if next_bucket_total:
                bucket_counts[bucket] = next_bucket_total
            else:
                bucket_counts.pop(bucket, None)
            if not bucket_counts:
                self._cohort_queued_buckets.pop(cohort_key, None)

            cohort_arms = self._cohort_queued_arms.setdefault(cohort_key, {})
            arm_counts = cohort_arms.setdefault(bucket, {})
            next_arm_total = max(
                0,
                int(arm_counts.get(arm_id, 0)) + int(delta),
            )
            if next_arm_total:
                arm_counts[arm_id] = next_arm_total
            else:
                arm_counts.pop(arm_id, None)
            if not arm_counts:
                cohort_arms.pop(bucket, None)
            if not cohort_arms:
                self._cohort_queued_arms.pop(cohort_key, None)

        for parent_key, child_key in zip(
            service_cohort_keys,
            service_cohort_keys[1:],
        ):
            parent_was_queued = parent_key in self._cohort_queued_direct_children
            child_counts = self._cohort_queued_direct_children.setdefault(
                parent_key,
                {},
            )
            previous_child_count = len(child_counts)
            next_child_total = max(
                0,
                int(child_counts.get(child_key, 0)) + int(delta),
            )
            if next_child_total:
                child_counts[child_key] = next_child_total
            else:
                child_counts.pop(child_key, None)
            if not child_counts:
                self._cohort_queued_direct_children.pop(parent_key, None)
            if len(child_counts) != previous_child_count or parent_was_queued != (
                parent_key in self._cohort_queued_direct_children
            ):
                self._cohort_direct_children_version += 1

            state = self._cohort_yield_states.get(parent_key)
            if (
                int(delta) > 0
                and previous_child_count < 2 <= len(child_counts)
                and state is not None
                and state.scope == "service"
                and state.pass_total >= 1
                and self._service_eligibility_unlocked(state)
                in {
                    Tier3ServiceCohortEligibility.OBSERVE,
                    Tier3ServiceCohortEligibility.DEMOTED,
                }
            ):
                self._dirty_cohort_keys.add(parent_key)
                reevaluate_discovery = True
        if int(delta) > 0 and service_cohort_keys:
            leaf_key = service_cohort_keys[-1]
            leaf_state = self._cohort_yield_states.get(leaf_key)
            if (
                leaf_state is not None
                and leaf_state.scope == "service"
                and leaf_state.leaf_eligible
                and int(self._cohort_queued_inventory.get(leaf_key, 0)) == 1
                and leaf_state.pass_total > max(
                    leaf_state.confirmation_evidence_pass_total,
                    leaf_state.probation_started_pass_total,
                )
                and self._service_eligibility_unlocked(leaf_state)
                in {
                    Tier3ServiceCohortEligibility.OBSERVE,
                    Tier3ServiceCohortEligibility.DEMOTED,
                }
            ):
                self._dirty_cohort_keys.add(leaf_key)
                reevaluate_discovery = True
        return reevaluate_discovery

    @staticmethod
    def _candidate_direct_child_cohort_key(
        candidate: ScheduledTier3Candidate,
        parent_cohort_key: str,
    ) -> str:
        cohort_keys = Tier3CandidateScheduler._candidate_service_cohort_keys(
            candidate
        )
        try:
            parent_index = cohort_keys.index(str(parent_cohort_key or ""))
        except ValueError:
            return ""
        if parent_index >= len(cohort_keys) - 1:
            return ""
        return cohort_keys[parent_index + 1]

    @staticmethod
    def _parent_discovery_child_keys_from_states(
        states: Iterable[_TicketState],
        parent_key: str,
    ) -> set[str]:
        reserved_children: set[str] = set()
        for ticket_state in states:
            ticket = ticket_state.ticket
            if (
                not ticket_state.reservation_open
                or ticket.reward_cohort_state != "parent_discovery"
                or ticket.reward_cohort_key != parent_key
            ):
                continue
            try:
                parent_index = ticket.service_cohort_keys.index(parent_key)
            except ValueError:
                continue
            if parent_index < len(ticket.service_cohort_keys) - 1:
                reserved_children.add(ticket.service_cohort_keys[parent_index + 1])
        return reserved_children

    def _reserved_parent_discovery_child_keys_unlocked(
        self,
        parent_cohort_key: str,
    ) -> set[str]:
        return self._parent_discovery_child_keys_from_states(
            (
                self._tickets[ticket_id]
                for ticket_id in self._open_reservation_ticket_ids
            ),
            str(parent_cohort_key or ""),
        )

    def _reserved_parent_discovery_child_keys_bruteforce_unlocked(
        self,
        parent_cohort_key: str,
    ) -> set[str]:
        reserved_children: set[str] = set()
        parent_key = str(parent_cohort_key or "")
        for ticket_state in self._tickets.values():
            ticket = ticket_state.ticket
            if (
                not ticket_state.reservation_open
                or ticket.reward_cohort_state != "parent_discovery"
                or ticket.reward_cohort_key != parent_key
            ):
                continue
            try:
                parent_index = ticket.service_cohort_keys.index(parent_key)
            except ValueError:
                continue
            if parent_index < len(ticket.service_cohort_keys) - 1:
                reserved_children.add(ticket.service_cohort_keys[parent_index + 1])
        return reserved_children

    def _parent_discovery_quota_open_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> bool:
        if (
            self._service_eligibility_unlocked(state)
            != Tier3ServiceCohortEligibility.PARENT_DISCOVERY
            or state.discovery_exhausted
            or state.discovery_selection_total >= 8
        ):
            return False
        if self._legacy_structural_routing_for_test:
            outstanding = self._cohort_counter_total_unlocked(
                "discovery_outstanding_total"
            )
            reserved = sum(
                max(0, int(value))
                for value in self._reserved_discovery_handoffs.values()
            )
            if outstanding + reserved >= 4:
                return False
            if state.hierarchy_level != 0:
                return True
            l0_count = 0
            active_keys = [
                key
                for key, candidate in self._cohort_yield_states.items()
                if candidate.discovery_outstanding_total > 0
            ] + [
                key
                for key, count in self._reserved_discovery_handoffs.items()
                if int(count) > 0
            ]
            for active_key in active_keys:
                cohort_state = self._cohort_yield_states.get(active_key)
                if (
                    cohort_state is not None
                    and cohort_state.hierarchy_level == 0
                ):
                    l0_count += 1
            return l0_count < 1
        if state.hierarchy_level != 2:
            return False
        reserved_for_parent = max(
            0,
            int(
                self._reserved_discovery_handoffs.get(
                    str(cohort_key or ""),
                    0,
                )
            ),
        )
        if state.discovery_outstanding_total + reserved_for_parent >= 1:
            return False
        outstanding = self._cohort_counter_total_unlocked(
            "discovery_outstanding_total"
        )
        reserved = sum(
            max(0, int(value))
            for value in self._reserved_discovery_handoffs.values()
        )
        return outstanding + reserved < 2

    def _leaf_confirmation_quota_open_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> bool:
        if (
            self._service_eligibility_unlocked(state)
            != Tier3ServiceCohortEligibility.LEAF_CONFIRMATION
            or state.confirmation_selection_total >= 2
        ):
            return False
        reserved_for_cohort = max(
            0,
            int(self._reserved_confirmation_handoffs.get(cohort_key, 0)),
        )
        cohort_limit = self._leaf_confirmation_parallel_limit_unlocked(
            cohort_key,
            state,
        )
        if (
            state.confirmation_outstanding_total + reserved_for_cohort
            >= cohort_limit
        ):
            return False
        return self._leaf_confirmation_fast_pending_unlocked() < 4

    def _leaf_confirmation_open_cohorts_unlocked(
        self,
    ) -> list[tuple[str, _CohortYieldState]]:
        """Confirmation cohorts with open quota and cold inventory."""

        return [
            (cohort_key, state)
            for cohort_key, state in self._cohort_items_with_eligibility_unlocked(
                (Tier3ServiceCohortEligibility.LEAF_CONFIRMATION,)
            )
            if self._leaf_confirmation_quota_open_unlocked(cohort_key, state)
            and int(self._cohort_cold_queued_inventory.get(cohort_key, 0)) > 0
        ]

    def _parent_discovery_open_cohorts_unlocked(
        self,
    ) -> list[tuple[str, _CohortYieldState]]:
        """Discovery parents with open quota and queued inventory."""

        return [
            (cohort_key, state)
            for cohort_key, state in self._cohort_items_with_eligibility_unlocked(
                (Tier3ServiceCohortEligibility.PARENT_DISCOVERY,)
            )
            if self._parent_discovery_quota_open_unlocked(cohort_key, state)
            and int(self._cohort_queued_inventory.get(cohort_key, 0)) > 0
        ]

    def _leaf_confirmation_fast_pending_unlocked(self) -> int:
        return int(self._confirmation_fast_pending_ticket_count) + sum(
            max(0, int(value))
            for value in self._reserved_confirmation_handoffs.values()
        )

    def _leaf_confirmation_deep_pending_unlocked(self) -> int:
        return int(self._confirmation_deep_pending_ticket_count)

    def _leaf_confirmation_fast_pending_bruteforce_unlocked(self) -> int:
        # Retained DEEP still owns its feedback flight and per-leaf limit,
        # but no longer waits for a FAST sample. Actor bounds DEEP retention.
        return sum(
            state.reward_flight_open
            and state.reward_flight_scope == "confirmation"
            and not state.fast_continuation_accepted
            for state in self._tickets.values()
        ) + sum(
            max(0, int(value))
            for value in self._reserved_confirmation_handoffs.values()
        )

    def _leaf_confirmation_deep_pending_bruteforce_unlocked(self) -> int:
        return sum(
            state.reward_flight_open
            and state.reward_flight_scope == "confirmation"
            and state.fast_continuation_accepted
            for state in self._tickets.values()
        )

    def _replay_discovery_open_count_unlocked(self) -> int:
        return int(self._replay_discovery_open_count)

    def _replay_discovery_open_count_bruteforce_unlocked(self) -> int:
        return sum(
            state.reservation_open and bool(state.replay_discovery_key)
            for state in self._tickets.values()
        )

    def _feedback_only_ticket_count_unlocked(self) -> int:
        return int(self._feedback_only_ticket_count)

    def _feedback_only_ticket_count_bruteforce_unlocked(self) -> int:
        return sum(
            1
            for state in self._tickets.values()
            if state.phase in {"feedback_only", "parked_deep", "terminal_pass"}
        )

    @staticmethod
    def _open_cold_reservation_cohort_keys_from_states(
        states: Iterable[_TicketState],
    ) -> tuple[frozenset[str], frozenset[str]]:
        leaf_keys: set[str] = set()
        l2_keys: set[str] = set()
        for state in states:
            ticket = state.ticket
            if (
                not state.reservation_open
                or ticket.evidence_origin not in _COLD_TRAINING_ORIGINS
            ):
                continue
            cohort_keys = ticket.service_cohort_keys
            if cohort_keys:
                leaf_keys.add(cohort_keys[-1])
            if len(cohort_keys) >= 3:
                l2_keys.add(cohort_keys[2])
        return frozenset(leaf_keys), frozenset(l2_keys)

    def _open_cold_reservation_cohort_keys_unlocked(
        self,
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Leaf and L2 keys of open cold reservations."""

        return self._open_cold_reservation_cohort_keys_from_states(
            self._tickets[ticket_id]
            for ticket_id in self._open_reservation_ticket_ids
        )

    def _open_cold_reservation_cohort_keys_bruteforce_unlocked(
        self,
    ) -> tuple[frozenset[str], frozenset[str]]:
        return self._open_cold_reservation_cohort_keys_from_states(
            self._tickets.values()
        )

    def _handoff_owning_ticket_ids_unlocked(self) -> tuple[int, ...]:
        return tuple(self._handoff_owning_ticket_ids)

    def _handoff_owning_ticket_ids_bruteforce_unlocked(self) -> tuple[int, ...]:
        return tuple(
            ticket_id
            for ticket_id, state in self._tickets.items()
            if not state.handoff_credit_released
            and state.phase in {"reserved", "tcp_active"}
        )

    def _stale_terminal_ticket_ids_unlocked(
        self,
        cutoff: float,
    ) -> list[int]:
        # ``_tickets`` inserts in ticket-id order, so sorting reproduces the
        # reference iteration order.
        return sorted(
            ticket_id
            for ticket_id in self._terminal_ticket_ids
            if self._tickets[ticket_id].updated_at < cutoff
        )

    def _stale_terminal_ticket_ids_bruteforce_unlocked(
        self,
        cutoff: float,
    ) -> list[int]:
        return [
            ticket_id
            for ticket_id, state in self._tickets.items()
            if state.phase.startswith("terminal") and state.updated_at < cutoff
        ]

    def _leaf_confirmation_parallel_limit_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> int:
        """Allow a second exact-leaf confirmation when inventory supports it."""

        inventory = self._cohort_inventory_unlocked(cohort_key)
        available = sum(
            int(inventory.get(key, 0))
            for key in ("reservable", "reserved", "active")
        )
        independent_strata = self._cohort_support_strata_unlocked(
            cohort_key,
            state,
        )
        if available >= 8 or (available >= 4 and independent_strata >= 2):
            return 2
        return 1

    def _service_dispatch_quota_open_unlocked(self, cohort_key: str) -> bool:
        state = self._cohort_yield_states.get(str(cohort_key or ""))
        eligibility = self._service_eligibility_unlocked(state)
        if state is None:
            return False
        reserved_for_cohort = max(
            0,
            int(self._reserved_service_handoffs.get(str(cohort_key or ""), 0)),
        )
        if eligibility == Tier3ServiceCohortEligibility.LEAF_PROBATION:
            cohort_unresolved = (
                int(state.probation_outstanding_total) + reserved_for_cohort
            )
            global_unresolved = self._cohort_counter_total_unlocked(
                "probation_outstanding_total"
            ) + sum(
                max(0, int(value))
                for value in self._reserved_probation_handoffs.values()
            )
            return (
                cohort_unresolved < 4
                and global_unresolved < 8
            )
        if (
            self._legacy_structural_routing_for_test
            and eligibility == Tier3ServiceCohortEligibility.PARENT_PROBATION
        ):
            cohort_unresolved = (
                int(state.probation_outstanding_total) + reserved_for_cohort
            )
            global_unresolved = self._cohort_counter_total_unlocked(
                "probation_outstanding_total"
            ) + sum(
                max(0, int(value))
                for value in self._reserved_probation_handoffs.values()
            )
            return cohort_unresolved < 4 and global_unresolved < 8
        if (
            self._legacy_structural_routing_for_test
            and eligibility == Tier3ServiceCohortEligibility.PARENT_PROVEN
        ):
            parent_limit = {0: 4, 1: 6, 2: 8}.get(
                int(state.hierarchy_level),
                4,
            )
            return (
                int(state.proven_outstanding_total) + reserved_for_cohort
                < parent_limit
            )
        if (
            eligibility == Tier3ServiceCohortEligibility.PROVEN_50
            and not self._legacy_structural_routing_for_test
        ):
            confirmation_completions = max(
                0,
                int(state.proven_completion_total)
                - int(state.production_state_started_completion_total),
            )
            confirmation_selections = max(
                0,
                int(state.proven_selection_total)
                - int(state.production_state_started_selection_total),
            )
            if confirmation_completions < 2:
                return (
                    int(state.proven_outstanding_total) + reserved_for_cohort < 4
                    and confirmation_selections + reserved_for_cohort < 4
                )
            return int(state.proven_outstanding_total) + reserved_for_cohort < 8
        if eligibility == Tier3ServiceCohortEligibility.PROVEN_50:
            return int(state.proven_outstanding_total) + reserved_for_cohort < 8
        if eligibility == Tier3ServiceCohortEligibility.PROVEN_75:
            return int(state.proven_outstanding_total) + reserved_for_cohort < 12
        return False

    def _similar_dispatch_quota_open_unlocked(self, cohort_key: str) -> bool:
        state = self._cohort_yield_states.get(str(cohort_key or ""))
        if (
            state is None
            or state.scope != "similar"
            or not state.promoted
            or state.lifecycle_state
            not in {"similar_probation", "similar_proven"}
        ):
            return False
        reserved = max(
            0,
            int(self._reserved_similar_handoffs.get(cohort_key, 0)),
        )
        per_cohort_limit = 2 if state.lifecycle_state == "similar_probation" else 4
        if state.production_outstanding_total + reserved >= per_cohort_limit:
            return False
        global_outstanding = sum(
            max(0, int(candidate.production_outstanding_total))
            for candidate in self._cohort_yield_states.values()
            if candidate.scope == "similar" and candidate.promoted
        ) + sum(
            max(0, int(value))
            for value in self._reserved_similar_handoffs.values()
        )
        global_limit = 4 if state.lifecycle_state == "similar_probation" else 8
        return global_outstanding < global_limit

    def _exact_reward_grant(
        self,
        arm_key: str,
        feature_key: Tier3CandidateFeatureKey,
    ) -> _FamilyRewardGrant | None:
        grants = self._exact_rewards.get(str(arm_key or ""))
        if not grants:
            return None
        for grant in tuple(grants):
            self._refresh_reward_grant_lifetime_unlocked(grant)
            if (
                grant.exact_handoffs_remaining > 0
                and not grant.expired
                and (
                    int(grant.outstanding_handoffs)
                    + int(
                        self._reserved_exact_grant_handoffs.get(
                            int(grant.grant_id),
                            0,
                        )
                    )
                    < 4
                )
                and grant.preferred_feature_key == feature_key
            ):
                return grant
            if self._reward_credit_remaining(grant) <= 0:
                self._expire_family_reward_unlocked(
                    (grant.origin_root, grant.origin_family),
                    grant,
                )
        return None

    def _grant_exact_inventory_count_unlocked(
        self,
        grant: _FamilyRewardGrant,
    ) -> int:
        return self._exact_queued_inventory.get(
            (grant.preferred_arm_id, grant.preferred_feature_key), 0
        )

    def _has_exact_reward_inventory_unlocked(self) -> bool:
        for grants in tuple(self._exact_rewards.values()):
            for grant in tuple(grants):
                self._refresh_reward_grant_lifetime_unlocked(grant)
                if (
                    not grant.expired
                    and self._reward_credit_remaining(grant) > 0
                    and self._grant_exact_inventory_count_unlocked(grant) > 0
                ):
                    return True
        return False

    def _candidate_reward_grant_and_scope(
        self,
        candidate: ScheduledTier3Candidate,
    ) -> tuple[_FamilyRewardGrant | None, str]:
        exact_grant = self._exact_reward_grant(
            candidate.arm_key,
            candidate.feature_key,
        )
        if exact_grant is not None:
            return exact_grant, "exact"
        if self._matching_service_profile_unlocked(
            candidate.feature_key,
            require_promoted=True,
            require_dispatch_quota=True,
        ):
            return None, "service"
        similar_key = self._candidate_similar_cohort_key(candidate)
        similar_state = self._cohort_yield_states.get(similar_key)
        if (
            self._legacy_structural_routing_for_test
            and similar_state is not None
            and similar_state.scope == "similar"
            and similar_state.promoted
            and self._similar_dispatch_quota_open_unlocked(similar_key)
        ):
            return None, "similar"
        return None, "none"

    def _arm_reward_grant_and_scope(
        self,
        arm: _Arm,
    ) -> tuple[_FamilyRewardGrant | None, str]:
        for candidate in arm.items:
            grant, scope = self._candidate_reward_grant_and_scope(candidate)
            if scope != "none":
                return grant, scope
        return None, "none"

    def _arm_reward_active(self, arm: _Arm, now: float | None = None) -> bool:
        del now
        return self._arm_reward_scope(arm) in {"exact", "service", "similar"}

    def _arm_reward_scope(self, arm: _Arm) -> str:
        return self._arm_reward_grant_and_scope(arm)[1]

    def _arm_exact_reward_active(self, arm: _Arm) -> bool:
        return any(
            self._candidate_reward_grant_and_scope(candidate)[1] == "exact"
            for candidate in arm.items
        )

    def _grant_reward_unlocked(
        self,
        arm: _Arm,
        *,
        arm_key: str,
        feature_key: Tier3CandidateFeatureKey,
    ) -> None:
        bucket = (arm.source_root, arm.family)
        self._reward_created_count += 1
        self._next_reward_grant_id += 1
        grant_id = self._next_reward_grant_id
        total_credit = min(
            16,
            max(1, self.reward_wave_limit * self.reward_candidate_limit),
        )
        grant = _FamilyRewardGrant(
            grant_id=grant_id,
            origin_root=arm.source_root,
            origin_family=arm.family,
            preferred_arm_id=str(arm_key or arm.arm_id),
            preferred_feature_key=feature_key,
            exact_handoffs_remaining=total_credit,
            service_handoffs_remaining=0,
            similar_handoffs_remaining=0,
            eligible_handoff_opportunities=0,
            service_handoff_opportunities=0,
            created_monotonic_ns=time.monotonic_ns(),
            used=False,
            first_handoff_recorded=False,
        )
        self._family_reward_grants.setdefault(bucket, deque()).append(grant)
        self._family_rewards.setdefault(bucket, grant)
        self._reward_grants_by_id[grant.grant_id] = grant
        self._exact_rewards.setdefault(grant.preferred_arm_id, deque()).append(grant)

    @staticmethod
    def _arm_tcp_quality_hint(arm: _Arm) -> float:
        """Return a bounded within-family TCP hint after enough observations."""

        trials = max(0, int(arm.tcp_completed))
        successes = max(0, min(trials, int(arm.tcp_responsive)))
        if trials < 8:
            return 0.0
        z = 1.96
        z2 = z * z
        proportion = successes / float(trials)
        denominator = 1.0 + (z2 / trials)
        center = proportion + (z2 / (2.0 * trials))
        margin = z * math.sqrt(
            (proportion * (1.0 - proportion) / trials)
            + (z2 / (4.0 * trials * trials))
        )
        wilson_lcb = max(0.0, (center - margin) / denominator)
        return min(0.1, 0.1 * wilson_lcb)

    @staticmethod
    def _bucket_for_arm(arm: _Arm) -> tuple[str, str]:
        return arm.source_root, arm.family

    def _bucket_credit(self, arm: _Arm) -> _BucketCredit:
        return self._bucket_credit_for(self._bucket_for_arm(arm))

    def _bucket_credit_for(
        self,
        bucket: tuple[str, str],
    ) -> _BucketCredit:
        return self._bucket_credits.setdefault(bucket, _BucketCredit())

    def _record_candidate_enqueued_unlocked(
        self,
        arm: _Arm,
        *,
        arm_was_empty: bool,
    ) -> None:
        credit = self._bucket_credit(arm)
        credit.queued_count += 1
        if arm.credit_exempt:
            credit.queued_credit_exempt_count += 1
        if arm_was_empty:
            credit.ready_arm_count += 1
            if arm.credit_exempt:
                credit.ready_credit_exempt_arm_count += 1

    def _record_candidate_dequeued_unlocked(self, arm: _Arm) -> None:
        credit = self._bucket_credit(arm)
        credit.queued_count = max(0, credit.queued_count - 1)
        if arm.credit_exempt:
            credit.queued_credit_exempt_count = max(
                0,
                credit.queued_credit_exempt_count - 1,
            )
        if not arm.items:
            credit.ready_arm_count = max(0, credit.ready_arm_count - 1)
            if arm.credit_exempt:
                credit.ready_credit_exempt_arm_count = max(
                    0,
                    credit.ready_credit_exempt_arm_count - 1,
                )

    def _promote_arm_to_credit_exempt_unlocked(self, arm: _Arm) -> None:
        if arm.credit_exempt:
            return
        queued_count = len(arm.items)
        arm.credit_exempt = True
        if queued_count <= 0:
            return
        credit = self._bucket_credit(arm)
        credit.queued_credit_exempt_count += queued_count
        credit.ready_credit_exempt_arm_count += 1

    def _bucket_deep_limit_unlocked(
        self,
        bucket: tuple[str, str],
    ) -> int:
        return (
            max(
                self.rewarded_parked_deep_limit,
                self.work_conserving_parked_deep_limit,
            )
            if self._family_reward_grant(bucket) is not None
            else self.work_conserving_parked_deep_limit
        )

    def _bucket_has_normal_dispatch_unlocked(
        self,
        bucket: tuple[str, str],
    ) -> bool:
        if not self._global_normal_dispatch_capacity_available():
            return False
        credit = self._bucket_credits.get(bucket)
        if credit is None or credit.queued_count <= 0:
            return False
        if credit.queued_credit_exempt_count > 0:
            return True
        return self._bucket_has_non_exempt_normal_dispatch_unlocked(bucket)

    def _bucket_has_non_exempt_normal_dispatch_unlocked(
        self,
        bucket: tuple[str, str],
    ) -> bool:
        if not self._global_normal_dispatch_capacity_available():
            return False
        credit = self._bucket_credits.get(bucket)
        if credit is None:
            return False
        queued_non_exempt = max(
            0,
            int(credit.queued_count)
            - int(credit.queued_credit_exempt_count),
        )
        return queued_non_exempt > 0

    def _bucket_reward_candidate_count_unlocked(
        self,
        bucket: tuple[str, str],
    ) -> int:
        """Count production-eligible work without broad family fallback."""

        total = 0
        for arm_id in self._active_arms.get(bucket, ()):
            arm = self._arms.get(arm_id)
            if arm is None or not arm.items:
                continue
            total += sum(
                1
                for candidate in arm.items
                if self._candidate_reward_grant_and_scope(candidate)[1]
                in {"exact", "similar"}
            )
        service_total = 0
        for cohort_key, _state in (
            self._production_service_cohorts_with_inventory_unlocked()
        ):
            service_total += max(
                0,
                int(
                    self._cohort_queued_buckets.get(cohort_key, {}).get(
                        bucket,
                        0,
                    )
                ),
            )
        return total + service_total

    def _first_queued_arm_unlocked(
        self,
        bucket: tuple[str, str],
        *,
        include_credit_exempt: bool,
    ) -> _Arm | None:
        for container in (
            self._active_arms.get(bucket, ()),
            self._overflow_arms.get(bucket, ()),
        ):
            for arm_id in container:
                arm = self._arms.get(arm_id)
                if arm is None or not arm.items:
                    continue
                if not include_credit_exempt and arm.credit_exempt:
                    continue
                return arm
        return None

    def _global_fast_credit_limit(self) -> int:
        return max(1, int(self._tcp_execution_limit))

    def _global_deep_credit_limit(self) -> int:
        return max(8, 8 * int(self._active_helper_count))

    def _global_escape_lease_limit(self) -> int:
        escape_cap = max(8, int(self._active_helper_ceiling) // 2)
        return max(1, min(escape_cap, int(math.ceil(self._active_helper_count / 2.0))))

    def _structural_production_limit_unlocked(self) -> int:
        # A maximum over two disjoint scopes; each pass keeps the reference
        # filter, so iterating only the indexed candidates is equivalent.
        limit = 0
        for _cohort_key, state in self._similar_cohort_items_unlocked():
            if state.scope == "similar" and state.promoted:
                if state.lifecycle_state == "similar_probation":
                    limit = max(limit, 4)
                elif state.lifecycle_state == "similar_proven":
                    limit = max(limit, 8)
        for _cohort_key, state in self._cohort_items_with_eligibility_unlocked(
            _STRUCTURAL_PRODUCTION_ELIGIBILITIES
        ):
            if state.scope != "service" or not state.leaf_eligible:
                continue
            eligibility = self._service_eligibility_unlocked(state)
            if eligibility == Tier3ServiceCohortEligibility.LEAF_PROBATION:
                limit = max(limit, 4)
            elif eligibility == Tier3ServiceCohortEligibility.PROVEN_50:
                limit = max(limit, 8)
            elif eligibility == Tier3ServiceCohortEligibility.PROVEN_75:
                limit = max(limit, 12)
        return limit

    def _ordinary_net_new_rate_unlocked(self) -> float:
        completed = [
            sample
            for sample in self._ordinary_e2e_samples
            if not bool(sample[2])
        ]
        if not completed:
            return 0.0
        return sum(1 for sample in completed if bool(sample[1])) / len(completed)

    def _visibility_reference_ucb_unlocked(self) -> tuple[float, bool]:
        unknown_outcomes = tuple(
            self._visibility_outcome_windows.get("unknown", ())
        )
        unknown_trials = len(unknown_outcomes)
        unknown_successes = sum(1 for passed in unknown_outcomes if passed)
        structural_outcomes = tuple(
            bool(sample[1])
            for state in self._cohort_yield_states.values()
            if state.scope == "service" and state.leaf_eligible
            for sample in state.production_samples
            if not bool(sample[2])
        )[-64:]
        structural_trials = len(structural_outcomes)
        structural_successes = sum(
            1 for passed in structural_outcomes if passed
        )
        bounds = [
            self._wilson_bound(successes, trials, lower=False)
            for successes, trials in (
                (unknown_successes, unknown_trials),
                (structural_successes, structural_trials),
            )
            if trials >= 4
        ]
        return (max(bounds), True) if bounds else (0.0, False)

    def _refresh_visibility_lane_budgets_unlocked(self) -> None:
        fresh_class = Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
        historical_class = Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
        fresh_resolved = max(
            0,
            int(self._visibility_resolved_total.get(fresh_class, 0)),
        )
        fresh_outcomes = tuple(
            self._visibility_outcome_windows.get(fresh_class, ())
        )
        if len(fresh_outcomes) >= 16 and not any(fresh_outcomes[-16:]):
            if self._fresh_replay_quota != 4:
                self._visibility_budget_adjustment_count += 1
            self._fresh_replay_quota = 4
            self._fresh_replay_losing_windows = 0
            self._fresh_replay_winning_windows = 0
        elif (
            fresh_resolved >= 8
            and fresh_resolved - self._fresh_replay_last_evaluated_resolved >= 8
        ):
            reference_ucb, reference_ready = (
                self._visibility_reference_ucb_unlocked()
            )
            self._fresh_replay_last_evaluated_resolved = fresh_resolved
            if reference_ready:
                fresh_window = fresh_outcomes[-8:]
                fresh_lcb = self._wilson_bound(
                    sum(1 for passed in fresh_window if passed),
                    len(fresh_window),
                    lower=True,
                )
                if fresh_lcb > reference_ucb:
                    self._fresh_replay_winning_windows += 1
                    self._fresh_replay_losing_windows = 0
                    if self._fresh_replay_winning_windows >= 2:
                        next_quota = min(12, self._fresh_replay_quota + 4)
                        if next_quota != self._fresh_replay_quota:
                            self._visibility_budget_adjustment_count += 1
                        self._fresh_replay_quota = next_quota
                        self._fresh_replay_winning_windows = 0
                else:
                    self._fresh_replay_winning_windows = 0
                    self._fresh_replay_losing_windows += 1
                    if self._fresh_replay_losing_windows >= 2:
                        next_quota = max(4, self._fresh_replay_quota - 4)
                        if next_quota != self._fresh_replay_quota:
                            self._visibility_budget_adjustment_count += 1
                        self._fresh_replay_quota = next_quota
                        self._fresh_replay_losing_windows = 0

        historical_resolved = max(
            0,
            int(self._visibility_resolved_total.get(historical_class, 0)),
        )
        historical_outcomes = tuple(
            self._visibility_outcome_windows.get(historical_class, ())
        )
        # Historical evidence is deliberately a bounded reprobe, not a warm
        # replay lane. Eight deterministic misses are enough to reduce the
        # lane to a continuous 1/16 recheck opportunity; a later recent
        # net-new result restores its bounded 4/16 quota.
        if (
            len(historical_outcomes) >= 8
            and not any(historical_outcomes[-8:])
        ):
            if self._historical_reprobe_quota != 1:
                self._visibility_budget_adjustment_count += 1
            self._historical_reprobe_quota = 1
        elif any(historical_outcomes[-8:]):
            if self._historical_reprobe_quota != 4:
                self._visibility_budget_adjustment_count += 1
            self._historical_reprobe_quota = 4
        self._historical_reprobe_last_evaluated_resolved = historical_resolved

    def _record_visibility_resolution_unlocked(
        self,
        state: _TicketState,
        outcome: Tier3SchedulingOutcome,
    ) -> None:
        self._resolve_replay_leaf_discovery_unlocked(state, outcome)
        ticket_id = int(state.ticket.ticket_id)
        if ticket_id in self._visibility_resolved_ticket_ids:
            return
        if outcome in {
            Tier3SchedulingOutcome.TCP_CENSORED,
            Tier3SchedulingOutcome.VALIDATION_CENSORED,
            Tier3SchedulingOutcome.CANCELLED,
            Tier3SchedulingOutcome.FACILITY,
        }:
            self._visibility_censored_total[state.ticket.visibility_class] += 1
            return
        if outcome not in {
            Tier3SchedulingOutcome.TCP_DEAD,
            Tier3SchedulingOutcome.VALIDATION_FAIL,
            Tier3SchedulingOutcome.NET_NEW,
            Tier3SchedulingOutcome.NEUTRAL_SKIP,
        }:
            return
        self._visibility_resolved_ticket_ids.add(ticket_id)
        self._visibility_resolved_total[state.ticket.visibility_class] += 1
        net_new = outcome is Tier3SchedulingOutcome.NET_NEW
        self._visibility_outcome_windows.setdefault(
            state.ticket.visibility_class,
            deque(maxlen=64),
        ).append(net_new)
        if net_new:
            self._visibility_net_new_total[state.ticket.visibility_class] += 1
            if state.ticket.visibility_class == Tier3CandidateVisibilityIndex.TCP_ONLY:
                if self._tcp_hint_yield_limited:
                    self._tcp_hint_restore_count += 1
                self._tcp_hint_yield_limited = False
                self._tcp_hint_fast_window.clear()
        self._refresh_visibility_lane_budgets_unlocked()

    def _tcp_hint_coverage_open_unlocked(self) -> bool:
        # Aggregate censor exposure is diagnostic, not evidence about every
        # other leaf. The existing coverage budget still bounds weak history.
        return True

    def _cold_leaf_preferred_visibility_unlocked(
        self, cohort_key: str,
    ) -> tuple[str, Mapping[str, int]]:
        return (
            Tier3CandidateVisibilityIndex.TCP_ONLY,
            self._cohort_tcp_only_queued_arms.get(cohort_key, {}),
        )

    def _refresh_visibility_exhaustion_unlocked(
        self,
        visibility_class: str,
    ) -> None:
        normalized = str(visibility_class or "unknown")
        if (
            self._lookahead_index.count(normalized) > 0
            or int(self._visibility_reserved_open.get(normalized, 0)) > 0
        ):
            self._visibility_inventory_exhausted_at_ms.pop(normalized, None)
            return
        self._visibility_inventory_exhausted_at_ms.setdefault(
            normalized,
            max(
                0,
                int((time.monotonic() - self._session_started_at) * 1000.0),
            ),
        )

    def _handoff_lane_budget_unlocked(
        self,
        prospective_handoffs: int = 1,
    ) -> Tier3HandoffLaneBudget:
        epoch_size = 16
        planned_slots = min(
            epoch_size,
            int(self._reserved_handoff_count)
            + max(1, int(prospective_handoffs)),
        )
        retained_count = min(
            len(self._handoff_lane_history),
            max(0, epoch_size - planned_slots),
        )
        retained_lane_history = list(self._handoff_lane_history)[
            -retained_count:
        ] if retained_count else []
        retained_selection_history = list(self._handoff_selection_history)[
            -retained_count:
        ] if retained_count else []
        retained_visibility_history = list(self._visibility_handoff_history)[
            -retained_count:
        ] if retained_count else []
        projected = min(epoch_size, retained_count + planned_slots)
        committed_coverage = sum(
            1 for coverage, _unknown, _reward in retained_lane_history
            if coverage
        )
        committed_unknown = sum(
            1 for _coverage, unknown, _reward in retained_lane_history
            if unknown
        )
        committed_exact_positive = sum(
            1
            for lane in retained_visibility_history
            if lane == Tier3CandidateVisibilityIndex.EXACT_POSITIVE
        )
        committed_validated_reprobe = sum(
            1
            for lane in retained_visibility_history
            if lane == Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
        )
        committed_reward = sum(
            1 for _coverage, _unknown, reward in retained_lane_history
            if reward
        )
        exact_committed = sum(
            1 for lane in retained_selection_history if lane == "exact"
        )
        service_probation_committed = sum(
            1
            for lane in retained_selection_history
            if lane == "service_probation"
        )
        service_discovery_committed = sum(
            1
            for lane in retained_selection_history
            if lane == "service_discovery"
        )
        leaf_confirmation_committed = sum(
            1
            for lane in retained_selection_history
            if lane == "leaf_confirmation"
        )
        cold_leaf_coverage_committed = sum(
            1
            for lane in retained_selection_history
            if lane in {"cold_leaf_coverage", "replay_leaf_discovery"}
        )
        service_proven_committed = sum(
            1
            for lane in retained_selection_history
            if lane == "service_proven"
        )
        similar_committed = sum(
            1
            for lane in retained_selection_history
            if lane == "network_similar"
        )
        ordinary_committed = sum(
            1 for lane in retained_selection_history if lane == "ordinary"
        )
        coverage_limit = max(1, int(math.ceil(epoch_size * 0.25)))
        unknown_floor = (
            max(
                1,
                int(math.ceil(epoch_size * self.unknown_exploration_ratio)),
            )
            if self.unknown_exploration_ratio > 0.0
            else 0
        )
        combined_priority_limit = min(
            12,
            max(1, int(math.floor(epoch_size * 0.75))),
        )
        reserved_validated_positive = int(
            self._visibility_reserved_open.get(
                Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                0,
            )
        )
        reserved_validated_reprobe = int(
            self._visibility_reserved_open.get(
                Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                0,
            )
        )
        reserved_service = sum(
            max(0, int(value))
            for value in self._reserved_service_handoffs.values()
        )
        reserved_similar = sum(
            max(0, int(value))
            for value in self._reserved_similar_handoffs.values()
        )
        reserved_structural = reserved_service + reserved_similar
        reserved_exact = sum(
            max(0, int(value))
            for value in self._reserved_exact_grant_handoffs.values()
        )
        committed_service = (
            service_probation_committed
            + service_proven_committed
        )
        committed_structural = committed_service + similar_committed
        combined_priority_remaining = max(
            0,
            combined_priority_limit
            - committed_exact_positive
            - committed_validated_reprobe
            - reserved_validated_positive
            - reserved_validated_reprobe
            - exact_committed
            - reserved_exact
            - committed_structural
            - reserved_structural
        )
        fresh_replay_limit = min(
            combined_priority_limit,
            max(4, int(self._fresh_replay_quota)),
        )
        fresh_replay_remaining = min(
            combined_priority_remaining,
            max(
                0,
                fresh_replay_limit
                - committed_exact_positive
                - reserved_validated_positive,
            ),
        )
        historical_reprobe_limit = min(
            combined_priority_limit,
            max(1, int(self._historical_reprobe_quota)),
        )
        historical_reprobe_remaining = min(
            combined_priority_remaining,
            max(
                0,
                historical_reprobe_limit
                - committed_validated_reprobe
                - reserved_validated_reprobe,
            ),
        )
        structural_production_limit = min(
            combined_priority_limit,
            self._structural_production_limit_unlocked(),
        )
        structural_production_remaining = min(
            combined_priority_remaining,
            max(
                0,
                structural_production_limit
                - committed_structural
                - reserved_structural
            ),
        )
        validated_replay_remaining = min(
            combined_priority_remaining,
            fresh_replay_remaining + historical_reprobe_remaining,
        )
        validated_reprobe_limit = historical_reprobe_limit
        validated_reprobe_remaining = historical_reprobe_remaining
        if self._legacy_structural_routing_for_test:
            reward_limit = (
                max(
                    1,
                    int(
                        math.floor(
                            projected * self._adaptive_exploit_scan_share
                        )
                    ),
                )
                if self._has_production_reward_unlocked()
                else 0
            )
            reward_remaining = max(
                0,
                reward_limit
                - committed_reward
                - int(self._reserved_exploit_count),
            )
            discovery_limit = max(1, int(math.ceil(projected * 0.25)))
        else:
            reward_limit = structural_production_limit
            reward_remaining = structural_production_remaining
            replay_inventory = any(
                self._lookahead_index.count(lane)
                for lane in (
                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                )
            )
            # Lend idle ordinary share to cold sampling, never to reward.
            idle_priority_share = (
                not replay_inventory and not self._has_production_reward_unlocked()
            )
            discovery_limit = min(8 if idle_priority_share else 4, epoch_size)
        reserved_discovery = sum(
            max(0, int(value))
            for value in self._reserved_discovery_handoffs.values()
        ) + sum(
            max(0, int(value))
            for value in self._reserved_confirmation_handoffs.values()
        ) + sum(
            max(0, int(value))
            for value in self._reserved_cold_leaf_handoffs.values()
        ) + self._replay_discovery_open_count_unlocked()
        return Tier3HandoffLaneBudget(
            projected_handoffs=projected,
            epoch_size=epoch_size,
            coverage_limit=coverage_limit,
            coverage_remaining=max(
                0,
                coverage_limit
                - committed_coverage
                - int(self._reserved_coverage_count),
            ),
            unknown_floor=unknown_floor,
            unknown_remaining=max(
                0,
                unknown_floor
                - committed_unknown
                - int(self._reserved_unknown_count),
            ),
            # One-schema aliases preserve the old field names while the shared
            # limit now covers both current-contract replay and bounded reprobe.
            exact_positive_limit=combined_priority_limit,
            exact_positive_remaining=validated_replay_remaining,
            reward_limit=reward_limit,
            reward_remaining=reward_remaining,
            validated_replay_limit=combined_priority_limit,
            validated_replay_remaining=validated_replay_remaining,
            validated_reprobe_limit=validated_reprobe_limit,
            validated_reprobe_remaining=validated_reprobe_remaining,
            fresh_replay_limit=fresh_replay_limit,
            fresh_replay_remaining=fresh_replay_remaining,
            historical_reprobe_limit=historical_reprobe_limit,
            historical_reprobe_remaining=historical_reprobe_remaining,
            structural_production_limit=structural_production_limit,
            structural_production_remaining=structural_production_remaining,
            combined_priority_limit=combined_priority_limit,
            combined_priority_remaining=combined_priority_remaining,
            discovery_limit=discovery_limit,
            discovery_remaining=max(
                0,
                discovery_limit
                - service_discovery_committed
                - leaf_confirmation_committed
                - cold_leaf_coverage_committed
                - reserved_discovery,
            ),
            exact_committed=exact_committed,
            leaf_confirmation_committed=leaf_confirmation_committed,
            service_discovery_committed=service_discovery_committed,
            service_probation_committed=service_probation_committed,
            service_proven_committed=service_proven_committed,
            ordinary_committed=ordinary_committed,
        )

    def _has_production_reward_unlocked(self) -> bool:
        for grants in tuple(self._exact_rewards.values()):
            for grant in tuple(grants):
                self._refresh_reward_grant_lifetime_unlocked(grant)
                if (
                    not grant.expired
                    and grant.exact_handoffs_remaining > 0
                    and (
                        int(grant.outstanding_handoffs)
                        + int(self._reserved_exact_grant_handoffs.get(grant.grant_id, 0))
                        < 4
                    )
                    and self._grant_exact_inventory_count_unlocked(grant) > 0
                ):
                    return True
        return bool(
            self._production_service_cohorts_with_inventory_unlocked()
            or self._production_similar_cohorts_with_inventory_unlocked()
        )

    def _global_normal_dispatch_capacity_available(self) -> bool:
        return bool(
            self._tcp_handoff_lease_count < self._global_fast_credit_limit()
        )

    def record_duplicate_observations(self, count: int = 1) -> None:
        with self._lock:
            self._duplicate_observation_count += max(0, int(count))

    def _reward_dispatch_eligible(self, arm: _Arm) -> bool:
        """Allow proven work to use its bounded ordering share."""

        return bool(
            self._arm_reward_active(arm)
            and self._global_normal_dispatch_capacity_available()
        )

    def _arm_dispatch_eligible(
        self,
        arm: _Arm,
        *,
        count_block: bool = True,
    ) -> bool:
        if arm.credit_exempt:
            # Snapshot revalidation may bypass its root/family credit, but it
            # still consumes the same finite TCP/FAST and future DEEP capacity.
            # Letting it bypass the global envelope allowed a 256-item queue
            # reservation to hide new source candidates behind stale work.
            eligible = self._global_normal_dispatch_capacity_available()
            if not eligible and count_block:
                self._credit_blocked_selection_count += 1
            return eligible
        if (
            not arm.validation_observed
            and (arm.validation_pending or arm.fast_outstanding > 0)
        ):
            if count_block:
                self._credit_blocked_selection_count += 1
            return False
        eligible = self._global_normal_dispatch_capacity_available()
        if not eligible and count_block:
            self._credit_blocked_selection_count += 1
        return eligible

    def _activate_dispatchable_overflow(
        self,
        bucket: tuple[str, str],
        *,
        require_unknown: bool = False,
        allow_reward_credit: bool = False,
        require_reward: bool = False,
        require_exact_reward: bool = False,
    ) -> bool:
        """Swap matching overflow work into the bounded active-arm window.

        The active-arm cap is a hot-path complexity bound, not a lease on a
        family's first coarse keys.  Once every active arm has been explored,
        unknown coverage must be able to rotate an unseen overflow arm in
        without waiting for a large source-prefix queue to drain completely.
        """

        # The shared TCP handoff window is the only capacity boundary. If it is
        # full, scanning thousands of coarse overflow arms cannot discover more
        # capacity; validation-stage feedback never participates here.
        if (
            not self._bucket_has_normal_dispatch_unlocked(bucket)
            and not allow_reward_credit
        ):
            return False
        active = self._active_arms.setdefault(bucket, deque())
        overflow = self._overflow_arms.get(bucket)
        if not overflow:
            return False
        selected: _Arm | None = None
        # Keep each attempt bounded. Rotating rejected entries to the tail
        # gives every overflow arm eventual coverage without an O(all-arms)
        # scan on a 250 ms scheduler tick.
        scan_limit = min(
            len(overflow),
            max(self.max_active_arms_per_family, self.learning_window * 2),
        )
        for _index in range(scan_limit):
            arm_id = overflow.popleft()
            arm = self._arms.get(arm_id)
            if arm is None or not arm.items:
                self._overflow_arm_count = max(0, self._overflow_arm_count - 1)
                continue
            normally_eligible = self._arm_dispatch_eligible(
                arm,
                count_block=False,
            )
            reward_eligible = bool(
                allow_reward_credit and self._reward_dispatch_eligible(arm)
            )
            matches = bool(
                (
                    require_unknown
                    and not arm.validation_observed
                    and not arm.validation_pending
                    and not arm.tcp_observed
                    and arm.fast_outstanding <= 0
                )
                or (
                    require_exact_reward
                    and self._arm_exact_reward_active(arm)
                )
                or (
                    require_reward
                    and not require_exact_reward
                    and self._arm_reward_active(arm)
                )
                or (
                    not require_unknown
                    and not require_reward
                    and not require_exact_reward
                )
            )
            if (
                selected is None
                and matches
                and (normally_eligible or reward_eligible)
            ):
                selected = arm
                self._overflow_arm_count = max(0, self._overflow_arm_count - 1)
                break
            overflow.append(arm_id)
        if selected is None:
            return False

        if len(active) >= self.max_active_arms_per_family:
            blocked_id: str | None = None
            for arm_id in active:
                arm = self._arms.get(arm_id)
                if (
                    arm is not None
                    and arm.items
                    and not self._arm_dispatch_eligible(arm, count_block=False)
                ):
                    blocked_id = arm_id
                    break
            if blocked_id is None and require_unknown:
                for arm_id in active:
                    arm = self._arms.get(arm_id)
                    if (
                        arm is not None
                        and arm.items
                        and (
                            arm.validation_observed
                            or arm.validation_pending
                            or arm.tcp_observed
                            or arm.fast_outstanding > 0
                        )
                        and not self._arm_exact_reward_active(arm)
                    ):
                        blocked_id = arm_id
                        break
            if blocked_id is None and require_exact_reward:
                for arm_id in active:
                    arm = self._arms.get(arm_id)
                    if (
                        arm is not None
                        and arm.items
                        and not self._arm_exact_reward_active(arm)
                    ):
                        blocked_id = arm_id
                        break
            if blocked_id is None and require_reward:
                for arm_id in active:
                    arm = self._arms.get(arm_id)
                    if (
                        arm is not None
                        and arm.items
                        and not self._arm_reward_active(arm)
                    ):
                        blocked_id = arm_id
                        break
            if blocked_id is None:
                overflow.appendleft(selected.arm_id)
                self._overflow_arm_count += 1
                return False
            active.remove(blocked_id)
            blocked = self._arms.get(blocked_id)
            if blocked is not None:
                blocked.active = False
            overflow.append(blocked_id)
            self._overflow_arm_count += 1
            self._active_arm_count = max(0, self._active_arm_count - 1)
            self._overflow_rotation_count += 1
            if require_unknown:
                self._unknown_overflow_rotation_count += 1

        selected.active = True
        active.append(selected.arm_id)
        self._active_arm_count += 1
        return True

    def _new_dispatch_ticket(
        self,
        arm: _Arm,
        candidate: ScheduledTier3Candidate,
        *,
        arm_key: str,
        selection_reason: str,
        coverage_selected: bool = False,
        fairness_peer_arm_ids: tuple[str, ...] = (),
        parent_discovery_cohort_key: str = "",
        leaf_confirmation_cohort_key: str = "",
        unknown_exploration_override: bool | None = None,
    ) -> Tier3CandidateFeedbackTicket:
        self._next_ticket_id += 1
        reward_grant, reward_scope = (
            self._candidate_reward_grant_and_scope(candidate)
            if selection_reason == "reward"
            else (None, "none")
        )
        unknown_exploration = (
            bool(unknown_exploration_override)
            if unknown_exploration_override is not None
            else bool(
                parent_discovery_cohort_key
                or leaf_confirmation_cohort_key
                or (
                    not arm.validation_observed
                    and not arm.validation_pending
                    and arm.fast_outstanding <= 0
                )
            )
        )
        service_cohort_keys = tuple(
            f"service:{profile}"
            for profile in candidate.feature_key.ordered_service_profiles()
        )
        reward_cohort_key = self._reward_cohort_key(
            reward_scope,
            reward_grant,
            candidate,
            require_dispatch_quota=selection_reason == "reward",
        )
        reward_probation = bool(
            selection_reason == "reward"
            and reward_scope in {"service", "similar"}
            and (
                state := self._cohort_yield_states.get(reward_cohort_key)
            ) is not None
            and (
                state.probation
                or state.lifecycle_state == "similar_probation"
            )
        )
        reward_cohort_state = ""
        discovery_child_key = ""
        if selection_reason == "reward" and reward_scope == "service":
            reward_state = self._cohort_yield_states.get(reward_cohort_key)
            if reward_state is not None:
                reward_cohort_state = reward_state.eligibility.value
        elif selection_reason == "reward" and reward_scope == "similar":
            reward_state = self._cohort_yield_states.get(reward_cohort_key)
            if reward_state is not None:
                reward_cohort_state = str(reward_state.lifecycle_state)
        elif parent_discovery_cohort_key:
            discovery_child_key = self._candidate_direct_child_cohort_key(
                candidate,
                parent_discovery_cohort_key,
            )
            if discovery_child_key:
                reward_cohort_key = parent_discovery_cohort_key
                reward_cohort_state = (
                    Tier3ServiceCohortEligibility.PARENT_DISCOVERY.value
                )
        elif leaf_confirmation_cohort_key:
            reward_cohort_key = leaf_confirmation_cohort_key
            reward_cohort_state = (
                Tier3ServiceCohortEligibility.LEAF_CONFIRMATION.value
            )
        if (
            str(candidate.visibility_class)
            == Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
        ):
            evidence_origin = Tier3CandidateEvidenceOrigin.FRESH_REPLAY.value
        elif (
            str(candidate.visibility_class)
            == Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
        ):
            evidence_origin = (
                Tier3CandidateEvidenceOrigin.HISTORICAL_REPROBE.value
            )
        elif (
            parent_discovery_cohort_key
            or leaf_confirmation_cohort_key
            or selection_reason == "reward"
            or selection_reason == "replay_leaf_discovery"
        ):
            evidence_origin = Tier3CandidateEvidenceOrigin.COLD_DISCOVERY.value
        else:
            evidence_origin = Tier3CandidateEvidenceOrigin.COLD_UNKNOWN.value
        ticket = Tier3CandidateFeedbackTicket(
            scheduler_generation=self._scheduler_generation,
            ticket_id=self._next_ticket_id,
            arm_key=str(arm_key or arm.coarse_key),
            source_root=arm.source_root,
            family=arm.family,
            coarse_key=arm.coarse_key,
            selection_reason=str(selection_reason or "coverage"),
            dispatched_monotonic_ns=time.monotonic_ns(),
            selection_lane=str(selection_reason or "coverage"),
            visibility_class=str(candidate.visibility_class or "unknown"),
            evidence_origin=evidence_origin,
            coverage_selected=bool(coverage_selected),
            unknown_exploration=unknown_exploration,
            control_revision=max(0, int(self._current_reservation_revision)),
            reward_grant_id=(
                int(reward_grant.grant_id)
                if reward_grant is not None
                else 0
            ),
            exact_grant_lease_id=(
                int(reward_grant.grant_id)
                if reward_grant is not None and reward_scope == "exact"
                else 0
            ),
            service_cohort_flight_id=(
                int(self._next_ticket_id)
                if reward_scope == "service"
                or parent_discovery_cohort_key
                or leaf_confirmation_cohort_key
                else 0
            ),
            similar_cohort_flight_id=(
                int(self._next_ticket_id)
                if reward_scope == "similar"
                else 0
            ),
            reward_scope=reward_scope,
            reward_cohort_key=reward_cohort_key,
            reward_probation=reward_probation,
            reward_cohort_state=reward_cohort_state,
            exact_cohort_key=f"exact:{candidate.arm_key}",
            service_cohort_key=(
                service_cohort_keys[-1]
                if service_cohort_keys
                else ""
            ),
            service_cohort_keys=service_cohort_keys,
            similar_cohort_key=(
                f"similar:{self._feature_signature(candidate.feature_key)}"
            ),
            feature_key=candidate.feature_key,
        )
        arm.fast_outstanding += 1
        bucket_credit = self._bucket_credit(arm)
        self._tcp_handoff_lease_count += 1
        ticket_state = _TicketState(
            ticket=ticket,
            arm_id=arm.arm_id,
            candidate=candidate,
            fairness_peer_arm_ids=tuple(fairness_peer_arm_ids),
            confirmation_epoch=(
                self._cohort_yield_states[reward_cohort_key].confirmation_epoch
                if reward_cohort_state == "leaf_confirmation"
                else 0
            ),
        )
        self._tickets[ticket.ticket_id] = ticket_state
        self._reindex_ticket_unlocked(ticket_state)
        self._cold_l2_training_outstanding_high_water = max(
            self._cold_l2_training_outstanding_high_water,
            self._cold_l2_training_outstanding_total_unlocked(),
        )
        if reward_cohort_key and (
            reward_scope in {"service", "similar"}
            or reward_cohort_state == "parent_discovery"
            or reward_cohort_state == "leaf_confirmation"
        ):
            reward_state = self._cohort_yield_states.get(reward_cohort_key)
            decision = {
                "ticket_id": int(ticket.ticket_id),
                "cohort_id": reward_cohort_key.split(":", 1)[-1],
                "hierarchy_level": int(
                    reward_state.hierarchy_level if reward_state is not None else -1
                ),
                "eligibility": str(reward_cohort_state or "observe"),
                "visibility_class": ticket.visibility_class,
                "remaining_tcp_only": sum(
                    self._cohort_tcp_only_queued_arms.get(reward_cohort_key, {}).values()
                ),
                "remaining_unknown": sum(
                    self._cohort_unknown_queued_arms.get(reward_cohort_key, {}).values()
                ),
                "selection_reason": (
                    "direct_child_discovery"
                    if reward_cohort_state == "parent_discovery"
                    else "leaf_confirmation"
                    if reward_cohort_state == "leaf_confirmation"
                    else "network_cohort_inventory"
                    if reward_scope == "similar"
                    else "leaf_exact_inventory"
                ),
                "direct_child_cohort_id": (
                    discovery_child_key.split(":", 1)[-1]
                    if discovery_child_key
                    else ""
                ),
                "inventory": self._cohort_inventory_unlocked(reward_cohort_key),
                "outcome": "pending",
                "passed": False,
            }
            self._cohort_decision_ring.append(decision)
            self._cohort_decisions_by_ticket[ticket.ticket_id] = decision
        self._reservation_total += 1
        self._reserved_handoff_count += 1
        self._visibility_reserved_open[ticket.visibility_class] += 1
        self._visibility_reserved_total[ticket.visibility_class] += 1
        if coverage_selected:
            self._reserved_coverage_count += 1
        if reward_cohort_state == "parent_discovery" and reward_cohort_key:
            self._reserved_discovery_handoffs[reward_cohort_key] = (
                int(
                    self._reserved_discovery_handoffs.get(
                        reward_cohort_key,
                        0,
                    )
                )
                + 1
            )
        if reward_cohort_state == "leaf_confirmation" and reward_cohort_key:
            self._reserved_confirmation_handoffs[reward_cohort_key] = (
                int(
                    self._reserved_confirmation_handoffs.get(
                        reward_cohort_key,
                        0,
                    )
                )
                + 1
            )
        if selection_reason == "cold_leaf_coverage":
            cohort_key = str(ticket.service_cohort_key or "")
            if cohort_key:
                self._reserved_cold_leaf_handoffs[cohort_key] = (
                    int(self._reserved_cold_leaf_handoffs.get(cohort_key, 0))
                    + 1
                )
                self._cold_leaf_coverage_selected_total += 1
        if selection_reason == "reward":
            self._reserved_exploit_count += 1
        if selection_reason == "reward":
            if reward_scope == "exact" and reward_grant is not None:
                grant_id = int(reward_grant.grant_id)
                self._reserved_exact_grant_handoffs[grant_id] = (
                    int(self._reserved_exact_grant_handoffs.get(grant_id, 0))
                    + 1
                )
            if reward_scope == "service" and reward_cohort_key:
                self._reserved_service_handoffs[reward_cohort_key] = (
                    int(
                        self._reserved_service_handoffs.get(
                            reward_cohort_key,
                            0,
                        )
                    )
                    + 1
                )
            if reward_scope == "similar" and reward_cohort_key:
                self._reserved_similar_handoffs[reward_cohort_key] = (
                    int(
                        self._reserved_similar_handoffs.get(
                            reward_cohort_key,
                            0,
                        )
                    )
                    + 1
                )
            if (
                reward_probation
            ) and reward_cohort_key:
                self._reserved_probation_handoffs[reward_cohort_key] = (
                    int(
                        self._reserved_probation_handoffs.get(
                            reward_cohort_key,
                            0,
                        )
                    )
                    + 1
                )
        if unknown_exploration:
            self._reserved_unknown_count += 1
        if selection_reason == "credit_escape":
            self._reserved_escape_count += 1
            bucket_credit.escape_ticket_id = int(ticket.ticket_id)
            self._active_escape_lease_count += 1
        return ticket

    def _release_handoff_capacity_unlocked(
        self,
        state: _TicketState,
        *,
        cancelled_before_handoff: bool = False,
    ) -> None:
        """Release reservation/TCP capacity without discarding feedback identity."""

        if state.handoff_credit_released:
            return
        reservation_was_open = state.reservation_open
        self._close_reservation_unlocked(state)
        if (
            cancelled_before_handoff
            or (reservation_was_open and not state.tcp_handed_off)
        ):
            self._cancelled_reservation_total += 1
        arm = self._arms.get(state.arm_id)
        bucket_credit = self._bucket_credit(arm) if arm is not None else None
        if arm is not None:
            arm.fast_outstanding = max(0, arm.fast_outstanding - 1)
        self._tcp_handoff_lease_count = max(
            0,
            self._tcp_handoff_lease_count - 1,
        )
        if (
            bucket_credit is not None
            and bucket_credit.escape_ticket_id == state.ticket.ticket_id
        ):
            bucket_credit.escape_ticket_id = 0
            self._active_escape_lease_count = max(
                0,
                self._active_escape_lease_count - 1,
            )
        state.handoff_credit_released = True
        state.updated_at = time.monotonic()
        self._reindex_ticket_unlocked(state)

    def _close_reservation_unlocked(self, state: _TicketState) -> None:
        if not state.reservation_open:
            return
        state.reservation_open = False
        self._reindex_ticket_unlocked(state)
        self._reserved_handoff_count = max(
            0,
            self._reserved_handoff_count - 1,
        )
        visibility_class = str(state.ticket.visibility_class or "unknown")
        self._visibility_reserved_open[visibility_class] = max(
            0,
            int(self._visibility_reserved_open.get(visibility_class, 0)) - 1,
        )
        self._refresh_visibility_exhaustion_unlocked(visibility_class)
        reason = state.ticket.selection_reason
        if state.ticket.coverage_selected:
            self._reserved_coverage_count = max(
                0,
                self._reserved_coverage_count - 1,
            )
        if state.ticket.reward_cohort_state == "parent_discovery":
            cohort_key = str(state.ticket.reward_cohort_key or "")
            remaining = max(
                0,
                int(self._reserved_discovery_handoffs.get(cohort_key, 0)) - 1,
            )
            if remaining:
                self._reserved_discovery_handoffs[cohort_key] = remaining
            else:
                self._reserved_discovery_handoffs.pop(cohort_key, None)
        if state.ticket.reward_cohort_state == "leaf_confirmation":
            cohort_key = str(state.ticket.reward_cohort_key or "")
            remaining = max(
                0,
                int(self._reserved_confirmation_handoffs.get(cohort_key, 0))
                - 1,
            )
            if remaining:
                self._reserved_confirmation_handoffs[cohort_key] = remaining
            else:
                self._reserved_confirmation_handoffs.pop(cohort_key, None)
        if reason == "cold_leaf_coverage":
            cohort_key = str(state.ticket.service_cohort_key or "")
            remaining = max(
                0,
                int(self._reserved_cold_leaf_handoffs.get(cohort_key, 0)) - 1,
            )
            if remaining:
                self._reserved_cold_leaf_handoffs[cohort_key] = remaining
            else:
                self._reserved_cold_leaf_handoffs.pop(cohort_key, None)
        if reason == "reward":
            self._reserved_exploit_count = max(
                0,
                self._reserved_exploit_count - 1,
            )
        if reason == "reward":
            if state.ticket.reward_scope == "exact":
                grant_id = int(
                    state.ticket.exact_grant_lease_id
                    or state.ticket.reward_grant_id
                    or 0
                )
                remaining = max(
                    0,
                    int(self._reserved_exact_grant_handoffs.get(grant_id, 0))
                    - 1,
                )
                if remaining:
                    self._reserved_exact_grant_handoffs[grant_id] = remaining
                else:
                    self._reserved_exact_grant_handoffs.pop(grant_id, None)
            if state.ticket.reward_scope == "service":
                cohort_key = str(state.ticket.reward_cohort_key or "")
                remaining = max(
                    0,
                    int(self._reserved_service_handoffs.get(cohort_key, 0)) - 1,
                )
                if remaining:
                    self._reserved_service_handoffs[cohort_key] = remaining
                else:
                    self._reserved_service_handoffs.pop(cohort_key, None)
            if state.ticket.reward_scope == "similar":
                cohort_key = str(state.ticket.reward_cohort_key or "")
                remaining = max(
                    0,
                    int(self._reserved_similar_handoffs.get(cohort_key, 0)) - 1,
                )
                if remaining:
                    self._reserved_similar_handoffs[cohort_key] = remaining
                else:
                    self._reserved_similar_handoffs.pop(cohort_key, None)
            if state.ticket.reward_probation:
                cohort_key = str(state.ticket.reward_cohort_key or "")
                remaining = max(
                    0,
                    int(self._reserved_probation_handoffs.get(cohort_key, 0))
                    - 1,
                )
                if remaining:
                    self._reserved_probation_handoffs[cohort_key] = remaining
                else:
                    self._reserved_probation_handoffs.pop(cohort_key, None)
        if state.ticket.unknown_exploration:
            self._reserved_unknown_count = max(
                0,
                self._reserved_unknown_count - 1,
            )
        if reason == "credit_escape":
            self._reserved_escape_count = max(
                0,
                self._reserved_escape_count - 1,
            )

    def _resolve_ticket(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
    ) -> _TicketState | None:
        parsed = (
            ticket
            if isinstance(ticket, Tier3CandidateFeedbackTicket)
            else Tier3CandidateFeedbackTicket.from_mapping(ticket)
        )
        if parsed is None or parsed.scheduler_generation != self._scheduler_generation:
            return None
        state = self._tickets.get(parsed.ticket_id)
        if state is None:
            return None
        return state

    def _release_ticket_credit(
        self,
        state: _TicketState,
        *,
        terminal: bool,
        retain_for_commit: bool = False,
        decision_outcome: str = "cancelled_or_unobserved",
    ) -> None:
        if terminal and not retain_for_commit:
            self._resolve_replay_leaf_discovery_unlocked(
                state, Tier3SchedulingOutcome.CANCELLED,
            )
        if state.feedback_released:
            if terminal and not retain_for_commit:
                self._close_cohort_decision_unlocked(
                    state.ticket.ticket_id,
                    outcome=decision_outcome,
                )
                self._forget_ticket_unlocked(state.ticket.ticket_id)
            return
        if terminal and not retain_for_commit and state.reward_flight_open:
            self._resolve_reward_flight_unlocked(
                state,
                Tier3SchedulingOutcome.CANCELLED,
            )
        self._release_handoff_capacity_unlocked(state)
        arm = self._arms.get(state.arm_id)
        bucket_credit = self._bucket_credit(arm) if arm is not None else None
        if arm is not None and not arm.validation_observed:
            arm.validation_pending = False
        if state.phase == "parked_deep":
            if arm is not None:
                arm.parked_deep_debt = max(0, arm.parked_deep_debt - 1)
            if bucket_credit is not None:
                bucket_credit.parked_deep_debt = max(
                    0,
                    bucket_credit.parked_deep_debt - 1,
                )
            self._parked_deep_debt = max(0, self._parked_deep_debt - 1)
        state.feedback_released = True
        self._credit_release_total += 1
        state.updated_at = time.monotonic()
        if terminal:
            state.phase = "terminal_pass" if retain_for_commit else "terminal"
            self._reindex_ticket_unlocked(state)
            if not retain_for_commit:
                self._close_cohort_decision_unlocked(
                    state.ticket.ticket_id,
                    outcome=decision_outcome,
                )
                self._forget_ticket_unlocked(state.ticket.ticket_id)

    def _close_cohort_decision_unlocked(
        self,
        ticket_id: int,
        *,
        outcome: str,
    ) -> None:
        decision = self._cohort_decisions_by_ticket.pop(int(ticket_id), None)
        if decision is not None and decision.get("outcome") in {
            "pending",
            "pass_pending_commit",
        }:
            decision["outcome"] = str(outcome or "cancelled_or_unobserved")

    def _prune_terminal_tickets(self) -> None:
        cutoff = time.monotonic() - 120.0
        stale = self._stale_terminal_ticket_ids_unlocked(cutoff)
        for ticket_id in stale:
            state = self._tickets.get(ticket_id)
            if state is not None:
                self._resolve_reward_flight_unlocked(
                    state,
                    Tier3SchedulingOutcome.CANCELLED,
                )
            self._close_cohort_decision_unlocked(
                ticket_id,
                outcome="terminal_pruned",
            )
            self._forget_ticket_unlocked(ticket_id)

    def enqueue(
        self,
        node: Node,
        report: Any,
        *,
        arm_key: str,
        source_root: str,
        family: str,
        coarse_key: str,
        feature_key: Tier3CandidateFeatureKey | Mapping[str, Any] | None = None,
        origin_service_profile: str = "",
        credit_exempt: bool = False,
    ) -> None:
        source_root = str(source_root or "unknown")
        family = self.normalize_family(family)
        coarse_key = str(coarse_key or "unknown")
        arm_key = str(arm_key or coarse_key)
        normalized_feature = (
            feature_key
            if isinstance(feature_key, Tier3CandidateFeatureKey)
            else Tier3CandidateFeatureKey.from_mapping(
                feature_key,
                fallback_family=family,
            )
        )
        if normalized_feature.service_family != family:
            normalized_feature = Tier3CandidateFeatureKey.build(
                service_family=family,
                transport=normalized_feature.transport,
                security=normalized_feature.security,
                port_bucket=normalized_feature.port_bucket,
                address_bucket=normalized_feature.address_bucket,
                host_suffix=normalized_feature.host_suffix,
                service_profile=normalized_feature.service_profile,
                auth_mode=normalized_feature.auth_mode,
                encryption=normalized_feature.encryption,
                flow=normalized_feature.flow,
                fingerprint=normalized_feature.fingerprint,
                path_shape=normalized_feature.path_shape,
                service_profiles=normalized_feature.service_profiles,
            )
        origin_profile = str(origin_service_profile or "").strip().lower()
        origin_cohort_key = ""
        if Tier3CandidateFeatureKey._meaningful(origin_profile):
            origin_cohort_key = f"service:{origin_profile}"
            profiles = list(normalized_feature.ordered_service_profiles())
            if origin_profile not in profiles:
                profiles.append(origin_profile)
            normalized_feature = Tier3CandidateFeatureKey.build(
                service_family=normalized_feature.service_family,
                transport=normalized_feature.transport,
                security=normalized_feature.security,
                port_bucket=normalized_feature.port_bucket,
                address_bucket=normalized_feature.address_bucket,
                host_suffix=normalized_feature.host_suffix,
                service_profile=profiles[-1],
                auth_mode=normalized_feature.auth_mode,
                encryption=normalized_feature.encryption,
                flow=normalized_feature.flow,
                fingerprint=normalized_feature.fingerprint,
                path_shape=normalized_feature.path_shape,
                service_profiles=tuple(profiles),
            )
        arm_id = self._arm_id(source_root, family, coarse_key)
        candidate = ScheduledTier3Candidate(
            node=node,
            report=report,
            arm_key=arm_key,
            source_root=source_root,
            family=family,
            coarse_key=coarse_key,
            feature_key=normalized_feature,
        )
        with self._lock:
            if origin_cohort_key:
                self._origin_structural_cohort_keys.add(origin_cohort_key)
            root_was_idle = source_root not in self._active_roots
            arm = self._arms.get(arm_id)
            if arm is None:
                arm = _Arm(
                    arm_id=arm_id,
                    source_root=source_root,
                    family=family,
                    coarse_key=coarse_key,
                    feature_key=normalized_feature,
                    credit_exempt=bool(credit_exempt),
                )
                self._arms[arm_id] = arm
                self._ensure_root_family(source_root, family)
                self._activate_arm(arm)
            elif not arm.items and not arm.active:
                # Arms are durable feedback identities. Once an earlier cohort
                # drains, later candidates for the same rewarded arm must make
                # it schedulable again instead of remaining in an inactive
                # historical shell.
                self._activate_arm(arm)
            if credit_exempt:
                self._promote_arm_to_credit_exempt_unlocked(arm)
            arm_was_empty = not arm.items
            arm.items.append(candidate)
            self._lookahead_index.add(
                candidate,
                arm_id=arm.arm_id,
                evidence=self._lookahead_evidence_for_candidate_unlocked(
                    candidate
                ),
            )
            self._visibility_inventory_exhausted_at_ms.pop(
                str(candidate.visibility_class or "unknown"),
                None,
            )
            reevaluate_discovery = self._record_candidate_cohort_inventory_unlocked(
                candidate,
                arm_id=arm.arm_id,
                delta=1,
            )
            self._record_candidate_enqueued_unlocked(
                arm,
                arm_was_empty=arm_was_empty,
            )
            self._arm_ids_by_feedback_key.setdefault(arm_key, set()).add(arm_id)
            self._backlog_count += 1
            self._root_backlog_counts[source_root] = (
                self._root_backlog_counts.get(source_root, 0) + 1
            )
            if root_was_idle:
                self._active_roots.add(source_root)
                self._root_order.append(source_root)
            if reevaluate_discovery:
                self._evaluate_dirty_service_cohorts_unlocked()

    def enqueue_many(
        self,
        candidates: list[tuple[Node, Any, str, str, str, str]],
    ) -> int:
        for node, report, arm_key, source_root, family, coarse_key in candidates:
            self.enqueue(
                node,
                report,
                arm_key=arm_key,
                source_root=source_root,
                family=family,
                coarse_key=coarse_key,
            )
        return len(candidates)

    def _promote_overflow(self, bucket: tuple[str, str]) -> None:
        active = self._active_arms.setdefault(bucket, deque())
        overflow = self._overflow_arms.get(bucket)
        while overflow and len(active) < self.max_active_arms_per_family:
            arm_id = overflow.popleft()
            self._overflow_arm_count = max(0, self._overflow_arm_count - 1)
            arm = self._arms.get(arm_id)
            if arm is None or not arm.items:
                continue
            arm.active = True
            active.append(arm_id)
            self._active_arm_count += 1

    def _pop_matching_arm(
        self,
        bucket: tuple[str, str],
        *,
        require_unknown: bool = False,
        require_reward: bool = False,
        require_exact_reward: bool = False,
        allow_reward: bool = True,
        exclude_reward: bool = False,
        ignore_credits: bool = False,
    ) -> tuple[_Arm | None, int, tuple[str, ...]]:
        active = self._active_arms.get(bucket)
        if not active:
            self._promote_overflow(bucket)
            active = self._active_arms.get(bucket)
        if not active:
            return None, 0, ()
        examined = 0
        allow_reward_credit = bool(
            allow_reward and (require_reward or require_exact_reward)
        )

        def dispatch_eligible(arm: _Arm) -> bool:
            if ignore_credits:
                return True
            normally_eligible = self._arm_dispatch_eligible(
                arm,
                count_block=not allow_reward_credit,
            )
            if normally_eligible:
                return True
            if allow_reward_credit and self._reward_dispatch_eligible(arm):
                return True
            if allow_reward_credit:
                self._credit_blocked_selection_count += 1
            return False

        use_tcp_hint = any(
            (
                (arm := self._arms.get(arm_id)) is not None
                and arm.tcp_completed >= 8
            )
            for arm_id in active
        )
        if self.score_provider is None and not use_tcp_hint:
            tcp_observed_unknown: _Arm | None = None
            for _index in range(len(active)):
                arm_id = active.popleft()
                examined += 1
                arm = self._arms.get(arm_id)
                if arm is None or not arm.items:
                    if arm is not None and arm.active:
                        arm.active = False
                        self._active_arm_count = max(0, self._active_arm_count - 1)
                    self._promote_overflow(bucket)
                    continue
                if not dispatch_eligible(arm):
                    active.append(arm_id)
                    continue
                matches = True
                if require_unknown:
                    matches = bool(
                        not arm.validation_observed
                        and not arm.validation_pending
                        and arm.fast_outstanding <= 0
                    )
                elif require_exact_reward:
                    matches = allow_reward and self._arm_exact_reward_active(arm)
                elif require_reward:
                    matches = allow_reward and self._arm_reward_active(arm)
                elif exclude_reward and self._arm_reward_active(arm):
                    matches = False
                active.append(arm_id)
                if require_unknown and matches and arm.tcp_observed:
                    if tcp_observed_unknown is None:
                        tcp_observed_unknown = arm
                    continue
                if matches:
                    return arm, examined, ()
            if self._activate_dispatchable_overflow(
                bucket,
                require_unknown=require_unknown,
                allow_reward_credit=allow_reward_credit,
                require_reward=require_reward,
                require_exact_reward=require_exact_reward,
            ):
                return self._pop_matching_arm(
                    bucket,
                    require_unknown=require_unknown,
                    require_reward=require_reward,
                    require_exact_reward=require_exact_reward,
                    allow_reward=allow_reward,
                    exclude_reward=exclude_reward,
                    ignore_credits=ignore_credits,
                )
            if tcp_observed_unknown is not None:
                return tcp_observed_unknown, examined, ()
            return None, examined, ()

        original_order = list(active)
        candidates: list[tuple[_Arm, float]] = []
        for _index in range(len(active)):
            arm_id = active.popleft()
            examined += 1
            arm = self._arms.get(arm_id)
            if arm is None or not arm.items:
                if arm is not None and arm.active:
                    arm.active = False
                    self._active_arm_count = max(0, self._active_arm_count - 1)
                self._promote_overflow(bucket)
                continue
            if not dispatch_eligible(arm):
                active.append(arm_id)
                continue
            matches = True
            if require_unknown:
                matches = bool(
                    not arm.validation_observed
                    and not arm.validation_pending
                    and arm.fast_outstanding <= 0
                )
            elif require_exact_reward:
                matches = allow_reward and self._arm_exact_reward_active(arm)
            elif require_reward:
                matches = allow_reward and self._arm_reward_active(arm)
            elif exclude_reward and self._arm_reward_active(arm):
                matches = False
            active.append(arm_id)
            if matches:
                score = self._arm_tcp_quality_hint(arm)
                if self.score_provider is not None:
                    try:
                        score += max(
                            -0.2,
                            min(0.2, float(self.score_provider(arm.items[0]))),
                        )
                    except Exception:
                        pass
                candidates.append((arm, score))
                if len(candidates) >= self.learning_window:
                    break
        if not candidates:
            if self._activate_dispatchable_overflow(
                bucket,
                require_unknown=require_unknown,
                allow_reward_credit=allow_reward_credit,
                require_reward=require_reward,
                require_exact_reward=require_exact_reward,
            ):
                return self._pop_matching_arm(
                    bucket,
                    require_unknown=require_unknown,
                    require_reward=require_reward,
                    require_exact_reward=require_exact_reward,
                    allow_reward=allow_reward,
                    exclude_reward=exclude_reward,
                    ignore_credits=ignore_credits,
                )
            return None, examined, ()

        if (
            require_unknown
            and all(arm.tcp_observed for arm, _score in candidates)
            and self._activate_dispatchable_overflow(
                bucket,
                require_unknown=True,
                allow_reward_credit=allow_reward_credit,
            )
        ):
            return self._pop_matching_arm(
                bucket,
                require_unknown=True,
                allow_reward=allow_reward,
                ignore_credits=ignore_credits,
            )

        overdue = [item for item in candidates if item[0].fairness_debt >= 2]
        if overdue:
            selected = overdue[0][0]
        else:
            first_score = candidates[0][1]
            if all(abs(score - first_score) < 1e-12 for _arm, score in candidates[1:]):
                selected = candidates[0][0]
            else:
                selected = max(candidates, key=lambda item: item[1])[0]
        fairness_peer_arm_ids = tuple(arm.arm_id for arm, _score in candidates)
        surviving_order = [
            arm_id
            for arm_id in original_order
            if (self._arms.get(arm_id) is not None and self._arms[arm_id].items)
        ]
        active.clear()
        active.extend(surviving_order)
        while active and active[0] != selected.arm_id:
            active.rotate(-1)
        if active:
            active.rotate(-1)
        return selected, examined, fairness_peer_arm_ids

    def _pop_from_bucket(
        self,
        bucket: tuple[str, str],
        *,
        prefer_unknown: bool,
        allow_reward: bool,
        ignore_credits: bool = False,
        force_selection_reason: str | None = None,
        coverage_selected: bool = False,
    ) -> tuple[ScheduledTier3Candidate | None, int, str, str | None]:
        examined = 0
        arm: _Arm | None = None
        lane = "coverage"
        fairness_peer_arm_ids: tuple[str, ...] = ()
        reward_available = bool(
            allow_reward
            and self._bucket_reward_candidate_count_unlocked(bucket) > 0
        )
        if prefer_unknown:
            arm, count, fairness_peer_arm_ids = self._pop_matching_arm(
                bucket,
                require_unknown=True,
                ignore_credits=ignore_credits,
            )
            examined += count
            if arm is not None:
                lane = "exploration"
        if arm is None and reward_available:
            arm, count, fairness_peer_arm_ids = self._pop_matching_arm(
                bucket,
                require_exact_reward=True,
                allow_reward=allow_reward,
                ignore_credits=ignore_credits,
            )
            examined += count
            if arm is not None:
                lane = "reward"
        if arm is None and reward_available:
            arm, count, fairness_peer_arm_ids = self._pop_matching_arm(
                bucket,
                require_reward=True,
                allow_reward=allow_reward,
                ignore_credits=ignore_credits,
            )
            examined += count
            if arm is not None:
                lane = "reward"
        if arm is None:
            arm, count, fairness_peer_arm_ids = self._pop_matching_arm(
                bucket,
                # A reward grant only controls whether this dispatch is
                # charged to the bounded exploit share. It must not hide the
                # same root/family from ordinary DRR once that share is full.
                exclude_reward=False,
                ignore_credits=ignore_credits,
            )
            examined += count
            lane = (
                "coverage"
                if arm is not None
                and not arm.validation_observed
                and not arm.validation_pending
                else "balanced"
            )
        if arm is None:
            return None, examined, lane, None
        candidate = self._dequeue_from_arm(
            arm,
            selection_reason=force_selection_reason or lane,
            coverage_selected=coverage_selected,
            fairness_peer_arm_ids=fairness_peer_arm_ids,
        )
        return candidate, examined, lane, arm.arm_id

    def _dequeue_from_arm(
        self,
        arm: _Arm,
        *,
        selection_reason: str,
        coverage_selected: bool = False,
        fairness_peer_arm_ids: tuple[str, ...] = (),
        required_reward_cohort_key: str = "",
        required_service_cohort_key: str = "",
        parent_discovery_cohort_key: str = "",
        leaf_confirmation_cohort_key: str = "",
        preferred_candidate: ScheduledTier3Candidate | None = None,
        unknown_exploration_override: bool | None = None,
    ) -> ScheduledTier3Candidate:
        bucket = (arm.source_root, arm.family)
        candidate_index = 0
        if preferred_candidate is not None:
            candidate_index = -1
            for index, queued in enumerate(arm.items):
                if queued is preferred_candidate:
                    candidate_index = index
                    break
            if candidate_index < 0:
                raise RuntimeError(
                    "lookahead candidate diverged from arm queue"
                )
        elif required_service_cohort_key:
            candidate_index = -1
            for index, queued in enumerate(arm.items):
                if parent_discovery_cohort_key:
                    matches_required = (
                        self._candidate_direct_child_cohort_key(
                            queued,
                            parent_discovery_cohort_key,
                        )
                        == required_service_cohort_key
                    )
                else:
                    matches_required = required_service_cohort_key in (
                        self._candidate_service_cohort_keys(queued)
                    )
                if matches_required:
                    candidate_index = index
                    break
            if candidate_index < 0:
                raise RuntimeError(
                    "required service cohort inventory diverged from arm queue"
                )
        elif selection_reason == "reward":
            for index, queued in enumerate(arm.items):
                _grant, scope = self._candidate_reward_grant_and_scope(queued)
                if required_reward_cohort_key.startswith("similar:"):
                    queued_cohort_key = self._candidate_similar_cohort_key(
                        queued
                    )
                else:
                    matched_profile = self._matching_service_profile_unlocked(
                        queued.feature_key,
                        require_promoted=True,
                        require_dispatch_quota=True,
                    )
                    queued_cohort_key = (
                        f"service:{matched_profile}" if matched_profile else ""
                    )
                if (
                    (
                        scope
                        == (
                            "similar"
                            if required_reward_cohort_key.startswith("similar:")
                            else "service"
                        )
                        if required_reward_cohort_key
                        else scope != "none"
                    )
                    and (
                        not required_reward_cohort_key
                        or queued_cohort_key == required_reward_cohort_key
                    )
                ):
                    candidate_index = index
                    if (
                        scope == "exact"
                        and _grant is not None
                        and not self._legacy_structural_routing_for_test
                        and not required_reward_cohort_key
                    ):
                        # A current net-new owns this exact grant. Transport
                        # history orders only its precise matching inventory;
                        # it cannot create reward for another arm or feature.
                        candidate_index = next(
                            (
                                hint_index
                                for hint_index, hinted in enumerate(arm.items)
                                if hinted.visibility_class
                                == Tier3CandidateVisibilityIndex.TCP_ONLY
                                and hinted.arm_key == queued.arm_key
                                and hinted.feature_key == queued.feature_key
                            ),
                            index,
                        )
                    break
        if candidate_index:
            candidate = arm.items[candidate_index]
            del arm.items[candidate_index]
        else:
            candidate = arm.items.popleft()
        self._lookahead_index.remove(candidate)
        self._record_candidate_cohort_inventory_unlocked(
            candidate,
            arm_id=arm.arm_id,
            delta=-1,
        )
        self._record_candidate_dequeued_unlocked(arm)
        self._backlog_count = max(0, self._backlog_count - 1)
        self._root_backlog_counts[arm.source_root] = max(
            0,
            self._root_backlog_counts.get(arm.source_root, 0) - 1,
        )
        if self._root_backlog_counts[arm.source_root] <= 0:
            self._active_roots.discard(arm.source_root)
        candidate.selection_lane = selection_reason
        candidate.dispatch_ticket = self._new_dispatch_ticket(
            arm,
            candidate,
            arm_key=candidate.arm_key,
            selection_reason=selection_reason,
            coverage_selected=coverage_selected,
            fairness_peer_arm_ids=fairness_peer_arm_ids,
            parent_discovery_cohort_key=parent_discovery_cohort_key,
            leaf_confirmation_cohort_key=leaf_confirmation_cohort_key,
            unknown_exploration_override=unknown_exploration_override,
        )
        if not arm.items and arm.active:
            active = self._active_arms.get(bucket)
            if active is not None:
                try:
                    active.remove(arm.arm_id)
                except ValueError:
                    pass
            arm.active = False
            self._active_arm_count = max(0, self._active_arm_count - 1)
            self._promote_overflow(bucket)
        elif not arm.items:
            overflow = self._overflow_arms.get(bucket)
            if overflow is not None:
                try:
                    overflow.remove(arm.arm_id)
                except ValueError:
                    pass
                else:
                    self._overflow_arm_count = max(
                        0,
                        self._overflow_arm_count - 1,
                    )
        return candidate

    def _pop_from_root(
        self,
        source_root: str,
        *,
        prefer_unknown: bool,
        allow_reward: bool,
        ignore_credits: bool = False,
    ) -> tuple[ScheduledTier3Candidate | None, int, str | None]:
        families = self._root_families.get(source_root)
        if not families:
            return None, 0, None
        examined = 0
        for _index in range(len(families)):
            family = families.popleft()
            families.append(family)
            candidate, count, _lane, arm_id = self._pop_from_bucket(
                (source_root, family),
                prefer_unknown=prefer_unknown,
                allow_reward=allow_reward,
                ignore_credits=ignore_credits,
            )
            examined += count + 1
            if candidate is not None:
                return candidate, examined, arm_id
        return None, examined, None

    def _ordered_ready_buckets_unlocked(self) -> list[tuple[str, str]]:
        buckets: list[tuple[str, str]] = []
        for source_root in self._root_order:
            if source_root not in self._active_roots:
                continue
            for family in self._root_families.get(source_root, ()):
                bucket = (source_root, family)
                credit = self._bucket_credits.get(bucket)
                if credit is not None and int(credit.queued_count) > 0:
                    buckets.append(bucket)
        return buckets

    @staticmethod
    def _candidate_leaf_service_cohort_key(
        candidate: ScheduledTier3Candidate,
    ) -> str:
        profiles = candidate.feature_key.ordered_service_profiles()
        return f"service:{profiles[-1]}" if profiles else ""

    @staticmethod
    def _candidate_can_confirm_leaf(candidate: ScheduledTier3Candidate) -> bool:
        # A current-session PASS can request bounded revalidation of a TCP-only
        # peer. Weak TCP evidence is neither trusted replay nor a validation
        # failure. Known negatives and replay never enter this cold flight.
        return candidate.visibility_class in {
            Tier3CandidateVisibilityIndex.UNKNOWN,
            Tier3CandidateVisibilityIndex.TCP_ONLY,
        }

    def _pop_leaf_confirmation_candidate_unlocked(
        self,
        *,
        ignore_credits: bool,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        examined = 0
        ready_bucket_order = self._ordered_ready_buckets_unlocked()
        confirmation_states = self._leaf_confirmation_open_cohorts_unlocked()
        confirmation_states.sort(
            key=lambda item: (
                int(item[1].confirmation_selection_total)
                + max(0, int(self._reserved_confirmation_handoffs.get(item[0], 0))),
                -int(
                    max(
                        0,
                        int(self._tcp_handoff_total)
                        - int(item[1].confirmation_eligible_handoff_total),
                    )
                    // 16
                ),
                int(item[1].confirmation_eligible_handoff_total),
                self._cohort_stable_sequence_unlocked(item[1]),
            )
        )
        for cohort_key, _state in confirmation_states:
            bucket_arms = self._cohort_queued_arms.get(cohort_key, {})
            preferred_visibility, preferred_arms = (
                self._cold_leaf_preferred_visibility_unlocked(cohort_key)
            )
            for bucket in ready_bucket_order:
                arm_counts = bucket_arms.get(bucket, {})
                peer_arm_ids = tuple(sorted(
                    (
                        arm_id
                        for arm_id, count in arm_counts.items()
                        if int(count) > 0
                        and (arm := self._arms.get(arm_id)) is not None
                        and arm.items
                    ),
                    # Removing and restoring a reservation must not move its
                    # coarse arm behind peers through dict insertion order.
                    key=lambda arm_id: (
                        int(arm_id not in preferred_arms),
                        self._arms[arm_id].items[0].lookahead_sequence,
                    ),
                ))
                for arm_id in peer_arm_ids:
                    examined += 1
                    arm = self._arms.get(arm_id)
                    if arm is None or not arm.items:
                        continue
                    if (
                        not ignore_credits
                        and not self._arm_dispatch_eligible(
                            arm,
                            count_block=False,
                        )
                    ):
                        continue
                    # Transport evidence orders only this exact-leaf sample,
                    # within the current fair bucket. It is not a PASS prior.
                    visibility = preferred_visibility if arm_id in preferred_arms else (
                        Tier3CandidateVisibilityIndex.TCP_ONLY
                        if preferred_visibility == Tier3CandidateVisibilityIndex.UNKNOWN
                        else Tier3CandidateVisibilityIndex.UNKNOWN
                    )
                    preferred = next(
                        (queued for queued in arm.items
                         if queued.visibility_class == visibility
                         and self._candidate_leaf_service_cohort_key(queued) == cohort_key),
                        None,
                    )
                    if preferred is None:
                        continue
                    candidate = self._dequeue_from_arm(
                        arm,
                        selection_reason="exploration",
                        fairness_peer_arm_ids=peer_arm_ids,
                        preferred_candidate=preferred,
                        leaf_confirmation_cohort_key=cohort_key,
                        unknown_exploration_override=(
                            preferred.visibility_class == Tier3CandidateVisibilityIndex.UNKNOWN
                        ),
                    )
                    self._rotate_after_bucket_selection_unlocked(bucket)
                    return candidate, examined
        return None, examined

    def _pop_balanced_leaf_discovery_candidate_unlocked(
        self,
        *,
        lane_budget: Tier3HandoffLaneBudget,
        ignore_credits: bool,
        provisionally_selected_arms: set[str],
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        confirmation_used = lane_budget.leaf_confirmation_committed + sum(
            max(0, int(count)) for count in self._reserved_confirmation_handoffs.values()
        )
        examined = 0
        if confirmation_used >= max(1, lane_budget.discovery_limit // 2):
            candidate, examined = self._pop_cold_leaf_coverage_candidate_unlocked(
                ignore_credits=ignore_credits,
                provisionally_selected_arms=provisionally_selected_arms,
            )
            if candidate is not None:
                return candidate, examined
        # If no unseen leaf is executable, confirmation borrows the unused
        # share. No reservation or quota is consumed by the failed lookup.
        candidate, confirmation_examined = self._pop_leaf_confirmation_candidate_unlocked(
            ignore_credits=ignore_credits,
        )
        examined += confirmation_examined
        if candidate is None:
            candidate, replay_examined = self._pop_replay_leaf_discovery_candidate_unlocked(
                ignore_credits=ignore_credits,
            )
            examined += replay_examined
        return candidate, examined

    def _open_replay_leaf_discovery_unlocked(self, state: _TicketState) -> None:
        ticket = state.ticket
        if (
            self._legacy_structural_routing_for_test
            or not state.fast_pass_observed
            or ticket.evidence_origin not in {
                Tier3CandidateEvidenceOrigin.FRESH_REPLAY.value,
                Tier3CandidateEvidenceOrigin.HISTORICAL_REPROBE.value,
            }
            or not ticket.service_cohort_keys
        ):
            return
        # Current PASS + net-new can request one test, not train the cold
        # learner. Repeated warm survivors cannot replenish this trial.
        self._replay_leaf_discoveries.setdefault(
            ticket.service_cohort_keys[-1],
            _ReplayLeafDiscovery(
                bucket=(ticket.source_root, ticket.family),
                created_at=time.monotonic(),
            ),
        )

    def _pop_replay_leaf_discovery_candidate_unlocked(
        self, *, ignore_credits: bool,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        if not self._replay_leaf_discoveries or sum(
            bool(signal.ticket_id) for signal in self._replay_leaf_discoveries.values()
        ) >= 2:
            return None, 0
        examined = 0
        now = time.monotonic()
        for bucket in self._ordered_ready_buckets_unlocked():
            for key, signal in self._replay_leaf_discoveries.items():
                if signal.bucket != bucket or signal.ticket_id or signal.selected:
                    continue
                if now - signal.created_at >= 60.0:
                    signal.outcome = "expired"
                    continue
                learned = self._cohort_yield_states.get(key)
                if learned is not None and learned.pass_total > 0:
                    continue
                arm_counts = self._cohort_queued_arms.get(key, {}).get(bucket, {})
                preferred_visibility, preferred_arms = (
                    self._cold_leaf_preferred_visibility_unlocked(key)
                )
                peer_ids = tuple(
                    sorted(
                        (
                            arm_id for arm_id, count in arm_counts.items()
                            if count > 0 and self._arms[arm_id].items
                        ),
                        key=lambda arm_id: (
                            int(arm_id not in preferred_arms),
                            self._arms[arm_id].items[0].lookahead_sequence,
                        ),
                    )
                )
                for arm_id in peer_ids:
                    examined += 1
                    arm = self._arms[arm_id]
                    if not ignore_credits and not self._arm_dispatch_eligible(
                        arm, count_block=False,
                    ):
                        continue
                    preferred = next(
                        (
                            item for item in arm.items
                            if self._candidate_can_confirm_leaf(item)
                            and self._candidate_leaf_service_cohort_key(item) == key
                            and (
                                arm_id not in preferred_arms
                                or item.visibility_class == preferred_visibility
                            )
                        ),
                        None,
                    )
                    if preferred is None:
                        continue
                    candidate = self._dequeue_from_arm(
                        arm, selection_reason="replay_leaf_discovery",
                        fairness_peer_arm_ids=peer_ids, preferred_candidate=preferred,
                        unknown_exploration_override=(
                            preferred.visibility_class == Tier3CandidateVisibilityIndex.UNKNOWN
                        ),
                    )
                    ticket = candidate.dispatch_ticket
                    replay_state = self._tickets[ticket.ticket_id]
                    replay_state.replay_discovery_key = key
                    self._reindex_ticket_unlocked(replay_state)
                    signal.ticket_id = ticket.ticket_id
                    signal.outcome = "reserved"
                    decision = {
                        "ticket_id": ticket.ticket_id,
                        "cohort_id": key.split(":", 1)[-1],
                        "hierarchy_level": len(ticket.service_cohort_keys) - 1,
                        "eligibility": "replay_leaf_discovery",
                        "visibility_class": ticket.visibility_class,
                        "selection_reason": "current_replay_net_new_trial",
                        "outcome": "pending",
                        "passed": False,
                    }
                    self._cohort_decision_ring.append(decision)
                    self._cohort_decisions_by_ticket[ticket.ticket_id] = decision
                    self._rotate_after_bucket_selection_unlocked(bucket)
                    return candidate, examined
        return None, examined

    def _resolve_replay_leaf_discovery_unlocked(
        self, state: _TicketState, outcome: Tier3SchedulingOutcome,
    ) -> None:
        signal = self._replay_leaf_discoveries.get(state.replay_discovery_key)
        if signal is None or signal.ticket_id != state.ticket.ticket_id:
            return
        if outcome is Tier3SchedulingOutcome.PASS_PENDING_COMMIT:
            return
        signal.ticket_id = 0
        decision = self._cohort_decisions_by_ticket.pop(state.ticket.ticket_id, None)
        if decision is not None:
            decision["outcome"] = outcome.value
            decision["net_new"] = outcome is Tier3SchedulingOutcome.NET_NEW
        if not state.tcp_handed_off:
            signal.cancelled_before_start += 1
            signal.outcome = "available"
            return
        signal.resolved += 1
        signal.net_new += int(outcome is Tier3SchedulingOutcome.NET_NEW)
        signal.outcome = outcome.value

    def _replay_leaf_discovery_snapshot_unlocked(self) -> dict[str, Any]:
        signals = tuple(self._replay_leaf_discoveries.values())
        return {
            "policy": "current_fast_net_new_same_root_leaf_one_trial",
            "donor_leaf_count": len(signals),
            "outstanding": sum(bool(signal.ticket_id) for signal in signals),
            **{
                key: sum(getattr(signal, key) for signal in signals)
                for key in (
                    "selected", "responsive", "passed", "resolved",
                    "net_new", "cancelled_before_start",
                )
            },
            "expired": sum(signal.outcome == "expired" for signal in signals),
            "trains_cold_from_replay": False,
        }

    def _pop_cold_leaf_coverage_candidate_unlocked(
        self,
        *,
        ignore_credits: bool,
        provisionally_selected_arms: set[str],
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        """Sample one candidate from each unresolved structural leaf.

        This uses the bounded discovery budget, including idle share, and is charged
        to unknown exploration. It does not promote the leaf or grant reward;
        it only prevents a large first-arriving structural bucket from hiding
        every later leaf during a cold expansion. Additional same-leaf work is
        reserved for the explicit confirmation path after a real PASS.
        """

        if not self._cohort_unknown_queued_arms:
            return None, 0
        examined = 0
        ready_bucket_order = self._ordered_ready_buckets_unlocked()
        cold_l2_outstanding = self._cold_l2_training_outstanding_unlocked()
        cold_l2_limit = self._cold_l2_training_current_limit_unlocked(
            cold_l2_outstanding
        )
        reserved_keys = {
            key
            for key, count in self._reserved_cold_leaf_handoffs.items()
            if int(count) > 0
        }
        # A coverage/ordinary reservation already owns the first sample too.
        # Only real starts enter the durable set; cancellation stays neutral.
        open_cold_leaf_keys, open_cold_l2_keys = (
            self._open_cold_reservation_cohort_keys_unlocked()
        )
        reserved_keys.update(open_cold_leaf_keys)
        sampled_parents = self._cold_l2_sampled_keys | open_cold_l2_keys
        # Shared, read-only until the queued parent/child structure changes.
        parent_by_child, ambiguous_children = (
            self._cold_leaf_parent_links_unlocked()
        )

        def cold_leaf_priority(
            item: tuple[str, int],
        ) -> tuple[int, int, int, int]:
            cohort_key, inventory_count = item
            independent_strata = len(
                self._cohort_queued_buckets.get(cohort_key, {})
            )
            return (
                int(parent_by_child.get(cohort_key, "") in sampled_parents),
                -int(independent_strata),
                -max(0, int(inventory_count)),
                int(
                    self._cohort_first_inventory_sequence.get(
                        cohort_key,
                        2**63 - 1,
                    )
                ),
            )

        # Ancestors, learned leaves and reserved first samples cannot be
        # selected here; do not compute their ranking or sort them each tick.
        def eligible_leaf(cohort_key: str) -> bool:
            return (
                cohort_key.startswith("service:")
                and bool(self._cohort_unknown_queued_arms.get(cohort_key))
                and cohort_key not in self._cohort_queued_direct_children
                and cohort_key not in self._cold_leaf_started_keys
                and cohort_key not in self._cold_leaf_sampled_keys
                and cohort_key not in reserved_keys
                and (
                    (state := self._cohort_yield_states.get(cohort_key)) is None
                    or int(state.completed_total) <= 0
                )
            )

        eligible_inventory = (
            (cohort_key, self._cohort_queued_inventory[cohort_key])
            for cohort_key in self._cohort_unknown_queued_arms
            if eligible_leaf(cohort_key)
        )
        order = self._cold_leaf_reservation_order
        # Canonical profiles have one parent. Keep the reference ordering for
        # malformed/test profiles whose parent could change after a dequeue.
        if order is None or any(eligible_leaf(key) for key in ambiguous_children):
            cohort_inventory = sorted(eligible_inventory, key=cold_leaf_priority)
        else:
            cohort_inventory = order.ordered(
                eligible_inventory,
                self._cohort_queued_inventory,
                eligible_leaf,
                cold_leaf_priority,
            )
        for cohort_key, inventory_count in cohort_inventory:
            if (
                not cohort_key.startswith("service:")
                or int(inventory_count) <= 0
                or cohort_key in self._cohort_queued_direct_children
                or cohort_key in self._cold_leaf_started_keys
                or cohort_key in self._cold_leaf_sampled_keys
                or cohort_key in reserved_keys
            ):
                continue
            state = self._cohort_yield_states.get(cohort_key)
            if state is not None and int(state.completed_total) > 0:
                continue
            unknown_arms = self._cohort_unknown_queued_arms.get(cohort_key, {})
            bucket_arms = self._cohort_queued_arms.get(cohort_key, {})
            for bucket in (
                candidate_bucket
                for candidate_bucket in ready_bucket_order
                if candidate_bucket in bucket_arms
            ):
                arm_counts = bucket_arms.get(bucket, {})
                peer_arm_ids = tuple(
                    arm_id
                    for arm_id, count in arm_counts.items()
                    if int(count) > 0
                )
                for arm_id in peer_arm_ids:
                    # TCP history may share the profile but cannot provide
                    # the unknown-only first sample requested by this lane.
                    if arm_id not in unknown_arms:
                        continue
                    examined += 1
                    if arm_id in provisionally_selected_arms:
                        continue
                    arm = self._arms.get(arm_id)
                    if arm is None or not arm.items:
                        continue
                    if (
                        not ignore_credits
                        and not self._arm_dispatch_eligible(
                            arm,
                            count_block=False,
                        )
                    ):
                        continue
                    preferred = next(
                        (
                            queued
                            for queued in arm.items
                            if queued.visibility_class
                            == Tier3CandidateVisibilityIndex.UNKNOWN
                            and self._candidate_leaf_service_cohort_key(queued)
                            == cohort_key
                        ),
                        None,
                    )
                    if preferred is None:
                        continue
                    l2_key = self._candidate_l2_cohort_key(preferred)
                    if (
                        l2_key
                        and int(cold_l2_outstanding.get(l2_key, 0))
                        >= cold_l2_limit
                    ):
                        self._cold_l2_training_deferred_total += 1
                        continue
                    candidate = self._dequeue_from_arm(
                        arm,
                        selection_reason="cold_leaf_coverage",
                        fairness_peer_arm_ids=peer_arm_ids,
                        preferred_candidate=preferred,
                        unknown_exploration_override=True,
                    )
                    provisionally_selected_arms.add(arm_id)
                    self._rotate_after_bucket_selection_unlocked(bucket)
                    return candidate, examined
        return None, examined

    def _parent_discovery_priority_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> tuple[Any, ...]:
        completed, passes = self._cohort_window_counts(state)
        evidence_lcb = self._wilson_bound(
            passes,
            len(completed),
            lower=True,
        )
        last_service = max(
            int(state.discovery_eligible_handoff_total),
            int(state.discovery_last_selected_handoff_total),
        )
        wait_handoffs = max(0, int(self._tcp_handoff_total) - last_service)
        inventory = self._cohort_inventory_unlocked(cohort_key)
        direct_children = len(
            self._cohort_queued_direct_children.get(cohort_key, {})
        )
        return (
            -int(wait_handoffs // 16),
            -int(state.hierarchy_level),
            -float(evidence_lcb),
            -int(passes),
            -int(direct_children),
            -int(inventory.get("root_family_count", 0)),
            -int(inventory.get("reservable", 0)),
            int(state.discovery_selection_total)
            + int(self._reserved_discovery_handoffs.get(cohort_key, 0)),
            int(state.discovery_eligible_handoff_total),
            self._cohort_stable_sequence_unlocked(state),
        )

    def _pop_parent_discovery_candidate_unlocked(
        self,
        *,
        ignore_credits: bool,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        """Select an executable direct child inside the exploration lane.

        Parent evidence may request bounded child coverage, but it never turns
        the whole parent into a production reward cohort. The queued cohort
        index lets discovery reach a child hidden behind unrelated coarse-arm
        prefixes without weakening root/family fairness.
        """

        examined = 0
        ready_bucket_order = self._ordered_ready_buckets_unlocked()
        parent_states = self._parent_discovery_open_cohorts_unlocked()
        parent_states.sort(
            key=lambda item: self._parent_discovery_priority_unlocked(
                item[0],
                item[1],
            )
        )
        for parent_key, state in parent_states:
            bucket_arms = self._cohort_queued_arms.get(parent_key, {})
            candidate_buckets = [
                bucket for bucket in ready_bucket_order if bucket in bucket_arms
            ]
            candidate_buckets.sort(
                key=lambda bucket: (
                    -int(self._credit_escape_epoch_debt.get(bucket, 0)),
                    ready_bucket_order.index(bucket),
                )
            )
            require_unseen_child = bool(
                not self._legacy_structural_routing_for_test
                or len(state.discovery_selected_child_keys) < 2
            )
            already_known_children = (
                set(state.current_pass_child_keys)
                | set(state.discovery_selected_child_keys)
                | self._reserved_parent_discovery_child_keys_unlocked(parent_key)
            )
            for bucket in candidate_buckets:
                arm_counts = bucket_arms.get(bucket, {})
                peer_arm_ids = tuple(
                    arm_id
                    for arm_id, count in arm_counts.items()
                    if int(count) > 0
                )
                for arm_id in peer_arm_ids:
                    examined += 1
                    arm = self._arms.get(arm_id)
                    if arm is None or not arm.items:
                        continue
                    if (
                        not ignore_credits
                        and not self._arm_dispatch_eligible(
                            arm,
                            count_block=False,
                        )
                    ):
                        continue
                    direct_child_key = ""
                    preferred = None
                    for queued in arm.items:
                        if (
                            queued.visibility_class
                            != Tier3CandidateVisibilityIndex.UNKNOWN
                        ):
                            continue
                        queued_child_key = (
                            self._candidate_direct_child_cohort_key(
                                queued,
                                parent_key,
                            )
                        )
                        if not queued_child_key:
                            continue
                        if (
                            require_unseen_child
                            and queued_child_key in already_known_children
                        ):
                            continue
                        direct_child_key = queued_child_key
                        preferred = queued
                        break
                    if not direct_child_key or preferred is None:
                        continue
                    candidate = self._dequeue_from_arm(
                        arm,
                        selection_reason="exploration",
                        fairness_peer_arm_ids=peer_arm_ids,
                        preferred_candidate=preferred,
                        parent_discovery_cohort_key=parent_key,
                    )
                    self._rotate_after_bucket_selection_unlocked(bucket)
                    return candidate, examined
        return None, examined

    def _pop_service_reward_candidate_unlocked(
        self,
        *,
        ignore_credits: bool,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        """Redeem an eligible leaf directly from its indexed inventory.

        The active-arm window bounds ordinary DRR work. It must not hide an
        already-proven leaf behind an unrelated coarse-arm prefix.
        """

        examined = 0
        ready_bucket_order = self._ordered_ready_buckets_unlocked()
        for cohort_key, _state in (
            self._production_service_cohorts_with_inventory_unlocked()
        ):
            bucket_arms = self._cohort_queued_arms.get(cohort_key, {})
            preferred_visibility, preferred_arms = (
                self._cold_leaf_preferred_visibility_unlocked(cohort_key)
            )
            if self._legacy_structural_routing_for_test:
                preferred_arms = {}
            candidate_buckets = [
                bucket for bucket in ready_bucket_order if bucket in bucket_arms
            ]
            candidate_buckets.sort(
                key=lambda bucket: (
                    -int(self._credit_escape_epoch_debt.get(bucket, 0)),
                    ready_bucket_order.index(bucket),
                )
            )
            for bucket in candidate_buckets:
                arm_counts = bucket_arms.get(bucket, {})
                peer_arm_ids = tuple(
                    arm_id
                    for arm_id, count in arm_counts.items()
                    if int(count) > 0
                )
                # Transport evidence only orders this eligible leaf inside
                # its existing fair bucket. It supplies no holdout PASS.
                ordered_arm_ids = peer_arm_ids
                if not self._legacy_structural_routing_for_test:
                    ordered_arm_ids = sorted(
                        (value for value in peer_arm_ids
                         if (arm := self._arms.get(value)) is not None and arm.items),
                        key=lambda value: (
                            value not in preferred_arms,
                            self._arms[value].items[0].lookahead_sequence,
                        ),
                    )
                for arm_id in ordered_arm_ids:
                    examined += 1
                    arm = self._arms.get(arm_id)
                    if arm is None or not arm.items:
                        continue
                    if (
                        not ignore_credits
                        and not self._arm_dispatch_eligible(
                            arm,
                            count_block=False,
                        )
                    ):
                        continue
                    preferred = next(
                        (
                            queued
                            for queued in arm.items
                            if (
                                self._legacy_structural_routing_for_test
                                or self._candidate_can_confirm_leaf(queued)
                            )
                            and (
                                arm_id not in preferred_arms
                                or queued.visibility_class == preferred_visibility
                            )
                            and self._candidate_leaf_service_cohort_key(queued)
                            == cohort_key
                            and self._candidate_reward_grant_and_scope(queued)[1]
                            == "service"
                        ),
                        None,
                    )
                    if preferred is None:
                        continue
                    candidate = self._dequeue_from_arm(
                        arm,
                        selection_reason="reward",
                        fairness_peer_arm_ids=peer_arm_ids,
                        preferred_candidate=preferred,
                    )
                    self._rotate_after_bucket_selection_unlocked(bucket)
                    return candidate, examined
        return None, examined

    def _pop_similar_reward_candidate_unlocked(
        self,
        *,
        ignore_credits: bool,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        """Redeem only an independently qualified network cohort."""

        examined = 0
        ready_bucket_order = self._ordered_ready_buckets_unlocked()
        for cohort_key, _state in (
            self._production_similar_cohorts_with_inventory_unlocked()
        ):
            bucket_arms = self._cohort_queued_arms.get(cohort_key, {})
            for bucket in (
                candidate_bucket
                for candidate_bucket in ready_bucket_order
                if candidate_bucket in bucket_arms
            ):
                arm_counts = bucket_arms.get(bucket, {})
                peer_arm_ids = tuple(
                    arm_id
                    for arm_id, count in arm_counts.items()
                    if int(count) > 0
                )
                for arm_id in peer_arm_ids:
                    examined += 1
                    arm = self._arms.get(arm_id)
                    if arm is None or not arm.items:
                        continue
                    if (
                        not ignore_credits
                        and not self._arm_dispatch_eligible(
                            arm,
                            count_block=False,
                        )
                    ):
                        continue
                    preferred = next(
                        (
                            queued
                            for queued in arm.items
                            if queued.visibility_class
                            not in {
                                Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                                Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                            }
                            and self._candidate_similar_cohort_key(queued)
                            == cohort_key
                            and self._candidate_reward_grant_and_scope(queued)[1]
                            == "similar"
                        ),
                        None,
                    )
                    if preferred is None:
                        continue
                    candidate = self._dequeue_from_arm(
                        arm,
                        selection_reason="reward",
                        fairness_peer_arm_ids=peer_arm_ids,
                        required_reward_cohort_key=cohort_key,
                        preferred_candidate=preferred,
                    )
                    self._rotate_after_bucket_selection_unlocked(bucket)
                    return candidate, examined
        return None, examined

    def _rotate_after_bucket_selection_unlocked(
        self,
        bucket: tuple[str, str],
    ) -> None:
        source_root, family = bucket
        while self._root_order and self._root_order[0] != source_root:
            self._root_order.rotate(-1)
        if self._root_order:
            self._root_order.rotate(-1)
        families = self._root_families.get(source_root)
        if families is None:
            return
        while families and families[0] != family:
            families.rotate(-1)
        if families:
            families.rotate(-1)

    def _pop_global_matching_candidate_unlocked(
        self,
        *,
        require_unknown: bool = False,
        require_exact_reward: bool = False,
        require_reward: bool = False,
        ignore_credits: bool = False,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        if require_reward and not require_exact_reward and not require_unknown:
            candidate, examined = self._pop_service_reward_candidate_unlocked(
                ignore_credits=ignore_credits,
            )
            if candidate is not None:
                return candidate, examined
            similar, similar_examined = (
                self._pop_similar_reward_candidate_unlocked(
                    ignore_credits=ignore_credits,
                )
            )
            examined += similar_examined
            if similar is not None:
                return similar, examined
        else:
            examined = 0
        for bucket in self._ordered_ready_buckets_unlocked():
            arm, count, fairness_peer_arm_ids = self._pop_matching_arm(
                bucket,
                require_unknown=require_unknown,
                require_exact_reward=require_exact_reward,
                require_reward=require_reward,
                allow_reward=bool(require_exact_reward or require_reward),
                ignore_credits=ignore_credits,
            )
            examined += count + 1
            if arm is None:
                continue
            lane = "exploration" if require_unknown else "reward"
            candidate = self._dequeue_from_arm(
                arm,
                selection_reason=lane,
                fairness_peer_arm_ids=fairness_peer_arm_ids,
            )
            self._rotate_after_bucket_selection_unlocked(bucket)
            return candidate, examined
        return None, examined

    def _pop_fairness_candidate_unlocked(
        self,
        *,
        provisionally_covered: set[tuple[str, str]],
        allow_reward: bool,
        ignore_credits: bool,
    ) -> tuple[ScheduledTier3Candidate | None, int]:
        examined = 0
        ready_buckets = self._ordered_ready_buckets_unlocked()
        for newcomer_only in (True, False):
            for bucket in ready_buckets:
                if bucket in provisionally_covered:
                    continue
                credit = self._bucket_credit_for(bucket)
                if newcomer_only:
                    if int(credit.committed_handoff_count) > 0:
                        continue
                elif int(self._credit_escape_epoch_debt.get(bucket, 0)) < 2:
                    continue
                candidate, count, _lane, _arm_id = self._pop_from_bucket(
                    bucket,
                    prefer_unknown=True,
                    allow_reward=allow_reward,
                    ignore_credits=ignore_credits,
                    force_selection_reason="coverage",
                    coverage_selected=True,
                )
                examined += count + 1
                if candidate is None:
                    continue
                self._rotate_after_bucket_selection_unlocked(bucket)
                return candidate, examined
        return None, examined

    def _advance_credit_escape_debt(
        self,
        selected_buckets: set[tuple[str, str]],
    ) -> None:
        """Count ready root/families skipped by real handoff epochs."""

        blocked_buckets = {
            bucket
            for bucket, credit in self._bucket_credits.items()
            if int(credit.queued_count) > 0
        }
        for bucket in tuple(self._credit_escape_epoch_debt):
            if bucket not in blocked_buckets:
                self._credit_escape_epoch_debt.pop(bucket, None)
        for bucket in blocked_buckets:
            if bucket in selected_buckets:
                self._credit_escape_epoch_debt[bucket] = 0
            else:
                self._credit_escape_epoch_debt[bucket] = min(
                    3,
                    int(self._credit_escape_epoch_debt.get(bucket, 0)) + 1,
                )

    def _lookahead_status_unlocked(
        self,
        entry: _Tier3CandidateLookaheadEntry,
    ) -> tuple[bool, bool]:
        return (
            entry.visibility_class
            == Tier3CandidateVisibilityIndex.EXACT_POSITIVE,
            entry.visibility_class
            != Tier3CandidateVisibilityIndex.UNKNOWN,
        )

    def _take_visibility_entry_unlocked(
        self,
        lanes: Iterable[str],
        *,
        provisionally_selected_arms: set[str],
        required_bucket: tuple[str, str] | None = None,
    ) -> tuple[_Tier3CandidateLookaheadEntry | None, int]:
        examined = 0
        for lane in lanes:
            entry, count = self._lookahead_index.take(
                lane,
                required_bucket=required_bucket,
                excluded_arm_ids=provisionally_selected_arms,
                eligibility=self._cold_unknown_entry_dispatchable_unlocked,
                examination_limit=64,
            )
            examined += count
            if entry is not None:
                return entry, examined
        return None, examined

    def _take_visibility_coverage_entry_unlocked(
        self,
        lanes: Iterable[str],
        *,
        provisionally_covered: set[tuple[str, str]],
        provisionally_selected_arms: set[str],
    ) -> tuple[_Tier3CandidateLookaheadEntry | None, int]:
        examined = 0
        ready_buckets = self._ordered_ready_buckets_unlocked()
        for newcomer_only in (True, False):
            for bucket in ready_buckets:
                if bucket in provisionally_covered:
                    continue
                credit = self._bucket_credit_for(bucket)
                if newcomer_only:
                    if int(credit.committed_handoff_count) > 0:
                        continue
                elif int(self._credit_escape_epoch_debt.get(bucket, 0)) < 2:
                    continue
                entry, count = self._take_visibility_entry_unlocked(
                    lanes,
                    provisionally_selected_arms=provisionally_selected_arms,
                    required_bucket=bucket,
                )
                examined += count
                if entry is not None:
                    return entry, examined
        return None, examined

    def _dequeue_lookahead_entry_unlocked(
        self,
        entry: _Tier3CandidateLookaheadEntry,
        *,
        selection_reason: str,
        coverage_selected: bool,
        unknown_exploration: bool,
    ) -> ScheduledTier3Candidate:
        arm = self._arms.get(entry.arm_id)
        if arm is None or not arm.items:
            raise RuntimeError("lookahead arm disappeared before reservation")
        outstanding = self._cold_l2_training_outstanding_unlocked()
        l2_key = self._candidate_l2_cohort_key(entry.candidate)
        if (
            entry.visibility_class == Tier3CandidateVisibilityIndex.UNKNOWN
            and l2_key
            and int(outstanding.get(l2_key, 0)) >= 4
            and self._cold_l2_training_current_limit_unlocked(outstanding) > 4
        ):
            self._cold_l2_training_borrowed_total += 1
        bucket = (arm.source_root, arm.family)
        peer_arm_ids = tuple(
            candidate_arm_id
            for candidate_arm_id in self._active_arms.get(bucket, ())
            if (
                (candidate_arm := self._arms.get(candidate_arm_id)) is not None
                and candidate_arm.items
            )
        )
        candidate = self._dequeue_from_arm(
            arm,
            selection_reason=selection_reason,
            coverage_selected=coverage_selected,
            fairness_peer_arm_ids=peer_arm_ids,
            preferred_candidate=entry.candidate,
            unknown_exploration_override=unknown_exploration,
        )
        self._lookahead_index.record_hit()
        self._rotate_after_bucket_selection_unlocked(bucket)
        return candidate

    def reserve_handoffs(
        self,
        limit: int,
        *,
        _ignore_credits: bool = False,
        escape_budget: int = 0,
        control_revision: int = 0,
    ) -> list[Tier3CandidateHandoffReservation]:
        """Reserve exactly the candidates that may enter the TCP queue.

        Reservations consume only the shared TCP handoff window so concurrent
        feeders cannot select the same capacity twice. They do not consume
        validation-stage credit or count as a handoff until ``commit_handoff``
        is called by the TCP worker boundary.
        """

        limit = max(0, int(limit))
        if limit <= 0:
            return []
        started = time.perf_counter()
        selected: list[ScheduledTier3Candidate] = []
        keys_examined = 0
        unknown_selected = 0
        exploit_selected = 0
        escape_selected = 0
        revision = max(1, int(control_revision))
        with self._lock:
            previous_revision = self._current_reservation_revision
            previous_cold_leaf_order = self._cold_leaf_reservation_order
            self._cold_leaf_reservation_order = _ColdLeafReservationOrder()
            self._current_reservation_revision = revision
            try:
                self._prune_terminal_tickets()
                # The TCP lease window is the only handoff capacity boundary.
                # Root/family DRR provides coverage directly, so the legacy
                # cross-revision credit escape must not create a second lane.
                # Keep the input for one compatibility schema version only.
                del escape_budget
                provisionally_covered: set[tuple[str, str]] = set()
                provisionally_selected_arms: set[str] = set()
                reservation_examination_limit = max(64, 8 * limit)

                while (
                    len(selected) < limit
                    and self._backlog_count > 0
                    and self._root_order
                ):
                    # This is the non-bypassable global ownership boundary.
                    # Per-arm and root/family checks decide who may use a slot;
                    # they must never make more slots exist.
                    if (
                        not _ignore_credits
                        and not self._global_normal_dispatch_capacity_available()
                    ):
                        break
                    candidate = None
                    selected_arm_id: str | None = None
                    lane_budget = self._handoff_lane_budget_unlocked(
                        max(1, limit - len(selected))
                    )
                    prefer_unknown = lane_budget.unknown_remaining > 0
                    if not self._legacy_structural_routing_for_test:
                        entry = None
                        coverage_selected = False
                        discovery_open = lane_budget.discovery_remaining > 0
                        reward_open = bool(
                            exploit_selected < self.reward_candidate_limit
                            and lane_budget.combined_priority_remaining > 0
                        )
                        exact_open = bool(
                            reward_open
                            and self._has_exact_reward_inventory_unlocked()
                        )
                        service_open = bool(
                            reward_open
                            and lane_budget.structural_production_remaining > 0
                        )
                        coverage_lanes = (
                            [Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE]
                            if lane_budget.fresh_replay_remaining > 0
                            else []
                        ) + (
                            [Tier3CandidateVisibilityIndex.VALIDATED_REPROBE]
                            if lane_budget.historical_reprobe_remaining > 0
                            else []
                        )
                        # Coverage repays root/family debt, not cache-class
                        # debt. Prefer unknown inventory in the same bucket;
                        # weak history still serves otherwise-uncovered roots.
                        coverage_lanes.append(Tier3CandidateVisibilityIndex.UNKNOWN)
                        tcp_history_coverage_open = (
                            self._lookahead_index.count(
                                Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
                            ) <= 0
                            and self._tcp_hint_coverage_open_unlocked()
                        )
                        if tcp_history_coverage_open:
                            coverage_lanes.append(
                                Tier3CandidateVisibilityIndex.TCP_ONLY
                            )
                        preferred_inventory = sum(
                            self._lookahead_index.count(lane)
                            for lane in (
                                Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                                Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                                Tier3CandidateVisibilityIndex.UNKNOWN,
                            )
                        )
                        if preferred_inventory <= 0:
                            if self._lookahead_index.count(
                                Tier3CandidateVisibilityIndex.TCP_ONLY
                            ) <= 0:
                                coverage_lanes.append(
                                    Tier3CandidateVisibilityIndex.OBSERVED_NONPOSITIVE
                                )
                        if lane_budget.coverage_remaining > 0:
                            entry, examined = (
                                self._take_visibility_coverage_entry_unlocked(
                                coverage_lanes,
                                provisionally_covered=provisionally_covered,
                                provisionally_selected_arms=(
                                    provisionally_selected_arms
                                ),
                            )
                            )
                            keys_examined += examined
                            coverage_selected = entry is not None
                        if entry is not None:
                            is_unknown = bool(
                                entry.visibility_class
                                == Tier3CandidateVisibilityIndex.UNKNOWN
                            )
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="coverage",
                                coverage_selected=True,
                                unknown_exploration=is_unknown,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        if candidate is None and prefer_unknown and discovery_open:
                            candidate, examined = (
                                self._pop_balanced_leaf_discovery_candidate_unlocked(
                                    lane_budget=lane_budget,
                                    ignore_credits=_ignore_credits,
                                    provisionally_selected_arms=provisionally_selected_arms,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and prefer_unknown and discovery_open:
                            candidate, examined = (
                                self._pop_parent_discovery_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and prefer_unknown and discovery_open:
                            candidate, examined = (
                                self._pop_cold_leaf_coverage_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                    provisionally_selected_arms=(
                                        provisionally_selected_arms
                                    ),
                                )
                            )
                            keys_examined += examined
                        if candidate is None and prefer_unknown:
                            entry, examined = self._take_visibility_entry_unlocked(
                                [Tier3CandidateVisibilityIndex.UNKNOWN],
                                provisionally_selected_arms=(
                                    provisionally_selected_arms
                                ),
                            )
                            keys_examined += examined
                        if entry is not None and candidate is None:
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="exploration",
                                coverage_selected=False,
                                unknown_exploration=True,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        # Trusted per-config evidence owns its existing replay
                        # quota before a successful arm generalizes to siblings.
                        if (
                            candidate is None
                            and lane_budget.fresh_replay_remaining > 0
                        ):
                            entry, examined = self._take_visibility_entry_unlocked(
                                [Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE],
                                provisionally_selected_arms=provisionally_selected_arms,
                            )
                            keys_examined += examined
                            if entry is not None:
                                candidate = self._dequeue_lookahead_entry_unlocked(
                                    entry, selection_reason="balanced",
                                    coverage_selected=False, unknown_exploration=False,
                                )
                                provisionally_selected_arms.add(entry.arm_id)
                        if candidate is None and exact_open:
                            candidate, examined = (
                                self._pop_global_matching_candidate_unlocked(
                                    require_exact_reward=True,
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and service_open:
                            candidate, examined = (
                                self._pop_service_reward_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if (
                            candidate is None
                            and entry is None
                            and lane_budget.historical_reprobe_remaining > 0
                        ):
                            entry, examined = self._take_visibility_entry_unlocked(
                                [Tier3CandidateVisibilityIndex.VALIDATED_REPROBE],
                                provisionally_selected_arms=(
                                    provisionally_selected_arms
                                ),
                            )
                            keys_examined += examined
                        if entry is not None and candidate is None:
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="balanced",
                                coverage_selected=False,
                                unknown_exploration=False,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        if candidate is None and discovery_open:
                            candidate, examined = (
                                self._pop_balanced_leaf_discovery_candidate_unlocked(
                                    lane_budget=lane_budget,
                                    ignore_credits=_ignore_credits,
                                    provisionally_selected_arms=provisionally_selected_arms,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and discovery_open:
                            candidate, examined = (
                                self._pop_parent_discovery_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and discovery_open:
                            candidate, examined = (
                                self._pop_cold_leaf_coverage_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                    provisionally_selected_arms=(
                                        provisionally_selected_arms
                                    ),
                                )
                            )
                            keys_examined += examined
                        if candidate is None:
                            entry, examined = self._take_visibility_entry_unlocked(
                                [Tier3CandidateVisibilityIndex.UNKNOWN],
                                provisionally_selected_arms=(
                                    provisionally_selected_arms
                                ),
                            )
                            keys_examined += examined
                        if entry is not None and candidate is None:
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="balanced",
                                coverage_selected=False,
                                unknown_exploration=True,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        if candidate is None:
                            trusted_or_unknown = sum(
                                self._lookahead_index.count(lane)
                                for lane in (
                                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                                    Tier3CandidateVisibilityIndex.UNKNOWN,
                                )
                            )
                            if trusted_or_unknown <= 0:
                                entry, examined = self._take_visibility_entry_unlocked(
                                    [Tier3CandidateVisibilityIndex.TCP_ONLY],
                                    provisionally_selected_arms=(
                                        provisionally_selected_arms
                                    ),
                                )
                                keys_examined += examined
                        if entry is not None and candidate is None:
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="balanced",
                                coverage_selected=False,
                                unknown_exploration=False,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        if candidate is None and self._backlog_count > 0:
                            other_inventory = sum(
                                self._lookahead_index.count(lane)
                                for lane in (
                                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                                    Tier3CandidateVisibilityIndex.UNKNOWN,
                                    Tier3CandidateVisibilityIndex.TCP_ONLY,
                                )
                            )
                            if other_inventory <= 0:
                                entry, examined = self._take_visibility_entry_unlocked(
                                    [
                                        Tier3CandidateVisibilityIndex.OBSERVED_NONPOSITIVE
                                    ],
                                    provisionally_selected_arms=(
                                        provisionally_selected_arms
                                    ),
                                )
                                keys_examined += examined
                        if entry is not None and candidate is None:
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="balanced",
                                coverage_selected=False,
                                unknown_exploration=False,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        if candidate is None and self._backlog_count > 0:
                            # Lane ceilings divide a full epoch; they are not
                            # permission to idle. If every preferred/unknown
                            # lane is empty, spend the otherwise-unused slot on
                            # another class that still owns quota. Coverage is
                            # orthogonal to visibility: it must never let
                            # historical reprobes bypass their 4/16 or 1/16
                            # ceiling.
                            spillover_lanes: list[str] = []
                            if (
                                lane_budget.fresh_replay_remaining > 0
                                and self._lookahead_index.count(
                                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
                                )
                                > 0
                            ):
                                spillover_lanes.append(
                                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
                                )
                            if (
                                lane_budget.historical_reprobe_remaining > 0
                                and self._lookahead_index.count(
                                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
                                )
                                > 0
                            ):
                                spillover_lanes.append(
                                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
                                )
                            unknown_inventory = self._lookahead_index.count(
                                Tier3CandidateVisibilityIndex.UNKNOWN
                            )
                            if not spillover_lanes and unknown_inventory <= 0:
                                # A class quota reserves space for competing
                                # work; it is not a reason to deadlock a
                                # single-class inventory. Once unknown/cold
                                # work is absent, return that unused capacity
                                # to ordinary DRR while retaining the original
                                # visibility label for yield accounting.
                                if self._lookahead_index.count(
                                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
                                ) > 0:
                                    spillover_lanes.append(
                                        Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE
                                    )
                                elif self._lookahead_index.count(
                                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
                                ) > 0:
                                    spillover_lanes.append(
                                        Tier3CandidateVisibilityIndex.VALIDATED_REPROBE
                                    )
                            preferred_inventory = sum(
                                self._lookahead_index.count(lane)
                                for lane in (
                                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                                    Tier3CandidateVisibilityIndex.UNKNOWN,
                                )
                            )
                            if preferred_inventory <= 0:
                                spillover_lanes.extend(
                                    [
                                        Tier3CandidateVisibilityIndex.TCP_ONLY,
                                        Tier3CandidateVisibilityIndex.OBSERVED_NONPOSITIVE,
                                    ]
                                )
                            entry, examined = self._take_visibility_entry_unlocked(
                                spillover_lanes,
                                provisionally_selected_arms=(
                                    provisionally_selected_arms
                                ),
                            )
                            keys_examined += examined
                        if entry is not None and candidate is None:
                            candidate = self._dequeue_lookahead_entry_unlocked(
                                entry,
                                selection_reason="ordinary_spillover",
                                coverage_selected=False,
                                unknown_exploration=False,
                            )
                            provisionally_selected_arms.add(entry.arm_id)
                        if (
                            keys_examined > reservation_examination_limit
                            and candidate is None
                        ):
                            break
                    else:
                        discovery_quota_open = (
                            lane_budget.discovery_remaining > 0
                        )
                        reward_quota_open = bool(
                            exploit_selected < self.reward_candidate_limit
                            and lane_budget.reward_remaining > 0
                        )
                        if lane_budget.coverage_remaining > 0:
                            candidate, examined = (
                                self._pop_fairness_candidate_unlocked(
                                    provisionally_covered=provisionally_covered,
                                    allow_reward=False,
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and prefer_unknown and discovery_quota_open:
                            candidate, examined = (
                                self._pop_leaf_confirmation_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and prefer_unknown and discovery_quota_open:
                            candidate, examined = (
                                self._pop_parent_discovery_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and prefer_unknown:
                            candidate, examined = (
                                self._pop_global_matching_candidate_unlocked(
                                    require_unknown=True,
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and reward_quota_open:
                            candidate, examined = (
                                self._pop_global_matching_candidate_unlocked(
                                    require_exact_reward=True,
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and reward_quota_open:
                            candidate, examined = (
                                self._pop_global_matching_candidate_unlocked(
                                    require_reward=True,
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and discovery_quota_open:
                            candidate, examined = (
                                self._pop_leaf_confirmation_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None and discovery_quota_open:
                            candidate, examined = (
                                self._pop_parent_discovery_candidate_unlocked(
                                    ignore_credits=_ignore_credits,
                                )
                            )
                            keys_examined += examined
                        if candidate is None:
                            root_attempts = len(self._root_order)
                            for _index in range(root_attempts):
                                source_root = self._root_order.popleft()
                                if source_root not in self._active_roots:
                                    continue
                                self._root_order.append(source_root)
                                candidate, examined, selected_arm_id = self._pop_from_root(
                                    source_root,
                                    prefer_unknown=False,
                                    allow_reward=False,
                                    ignore_credits=_ignore_credits,
                                )
                                keys_examined += examined + 1
                                if candidate is not None:
                                    break
                                if self._root_backlog_counts.get(source_root, 0) <= 0:
                                    self._active_roots.discard(source_root)
                    if candidate is None:
                        break
                    selected.append(candidate)
                    provisionally_covered.add(
                        (candidate.source_root, candidate.family)
                    )
                    if bool(
                        candidate.dispatch_ticket
                        and candidate.dispatch_ticket.unknown_exploration
                    ):
                        unknown_selected += 1
                    if candidate.selection_lane == "reward":
                        exploit_selected += 1
                self._tick_count += 1
                self._keys_examined_total += keys_examined
                if not self._legacy_structural_routing_for_test:
                    self._lookahead_index.record_reservation_examined(
                        keys_examined
                    )
                if not selected and self._backlog_count > 0:
                    self._idle_with_backlog_count += 1
                self._last_tick_selected = len(selected)
                self._last_tick_keys_examined = keys_examined
                self._last_tick_exploit_reserved = exploit_selected
                self._last_tick_unknown_reserved = unknown_selected
                self._last_tick_credit_escape_reserved = escape_selected
                self._last_tick_ms = (time.perf_counter() - started) * 1000.0
            finally:
                self._current_reservation_revision = previous_revision
                self._cold_leaf_reservation_order = previous_cold_leaf_order
            return [
                Tier3CandidateHandoffReservation(
                    ticket=item.dispatch_ticket,
                    candidate=item,
                    control_revision=revision,
                    reserved_monotonic_ns=(
                        item.dispatch_ticket.dispatched_monotonic_ns
                    ),
                )
                for item in selected
                if item.dispatch_ticket is not None
            ]

    def drain(
        self,
        limit: int,
        *,
        _ignore_credits: bool = False,
        escape_budget: int = 0,
        control_revision: int = 0,
    ) -> list[ScheduledTier3Candidate]:
        """Compatibility helper returning reserved work without committing it."""

        reservations = self.reserve_handoffs(
            limit,
            _ignore_credits=_ignore_credits,
            escape_budget=escape_budget,
            control_revision=control_revision,
        )
        return [reservation.candidate for reservation in reservations]

    def _commit_handoff_unlocked(self, state: _TicketState) -> bool:
        if state.tcp_handed_off:
            return False
        self._close_reservation_unlocked(state)
        state.tcp_handed_off = True
        if state.replay_discovery_key:
            signal = self._replay_leaf_discoveries[state.replay_discovery_key]
            signal.selected += 1
            signal.outcome = "tcp_active"
        state.phase = "tcp_active"
        state.updated_at = time.monotonic()
        self._reindex_ticket_unlocked(state)
        self._selected_total += 1
        self._tcp_handoff_total += 1
        if state.ticket.evidence_origin in {
            Tier3CandidateEvidenceOrigin.COLD_UNKNOWN.value,
            Tier3CandidateEvidenceOrigin.COLD_DISCOVERY.value,
        } and state.ticket.service_cohort_keys:
            self._cold_leaf_sampled_keys.add(state.ticket.service_cohort_keys[-1])
            if len(state.ticket.service_cohort_keys) >= 3:
                self._cold_l2_sampled_keys.add(state.ticket.service_cohort_keys[2])
        if state.ticket.selection_reason == "cold_leaf_coverage":
            cohort_key = str(state.ticket.service_cohort_key or "")
            if cohort_key:
                self._cold_leaf_started_keys.add(cohort_key)
                self._cold_leaf_coverage_started_total += 1
        self._visibility_start_total[state.ticket.visibility_class] += 1
        self._tcp_outcome_window.append((state.ticket.ticket_id, "pending"))
        if state.ticket.selection_reason == "credit_escape":
            arm = self._arms.get(state.arm_id)
            if arm is not None:
                arm.fairness_debt = 0
        return True

    def _record_tcp_window_outcome_unlocked(
        self,
        ticket_id: int,
        *,
        responsive: bool,
        hard_censored: bool = False,
    ) -> None:
        for index in range(len(self._tcp_outcome_window) - 1, -1, -1):
            sample_ticket_id, sample_outcome = self._tcp_outcome_window[index]
            if sample_ticket_id != int(ticket_id):
                continue
            if sample_outcome == "pending":
                self._tcp_outcome_window[index] = (
                    sample_ticket_id,
                    (
                        "hard_censored"
                        if hard_censored
                        else "responsive"
                        if responsive
                        else "nonresponsive"
                    ),
                )
            return

    def _open_reward_flight_unlocked(self, state: _TicketState) -> None:
        """Transfer a reward reservation into unresolved outcome ownership."""

        if state.reward_flight_open:
            return
        ticket = state.ticket
        lifecycle = str(ticket.reward_cohort_state or "")
        if ticket.reward_scope == "exact" and ticket.reward_grant_id > 0:
            grant = self._reward_grants_by_id.get(int(ticket.reward_grant_id))
            if grant is None:
                return
            grant.outstanding_handoffs += 1
            grant.outstanding_high_water = max(
                int(grant.outstanding_high_water),
                int(grant.outstanding_handoffs),
            )
            state.reward_flight_open = True
            state.reward_flight_scope = "exact"
            state.reward_flight_key = str(grant.grant_id)
            state.reward_flight_lifecycle = "exact"
            self._reindex_ticket_unlocked(state)
            total_outstanding = sum(
                max(0, int(candidate.outstanding_handoffs))
                for candidate in self._reward_grants_by_id.values()
            )
            self._reward_exact_outstanding_high_water = max(
                int(self._reward_exact_outstanding_high_water),
                total_outstanding,
            )
            return
        cohort_key = str(ticket.reward_cohort_key or "")
        cohort = self._cohort_yield_states.get(cohort_key)
        if cohort is None or cohort.scope not in {"service", "similar"}:
            return
        if ticket.reward_scope == "service":
            cohort.production_outstanding_total += 1
            self._note_cohort_counter_unlocked(
                "production_outstanding_total",
                cohort_key,
            )
            cohort.production_outstanding_high_water = max(
                int(cohort.production_outstanding_high_water),
                int(cohort.production_outstanding_total),
            )
            if lifecycle in {
                Tier3ServiceCohortEligibility.LEAF_PROBATION.value,
                Tier3ServiceCohortEligibility.PARENT_PROBATION.value,
            }:
                cohort.probation_outstanding_total += 1
                self._note_cohort_counter_unlocked(
                    "probation_outstanding_total",
                    cohort_key,
                )
                cohort.probation_outstanding_high_water = max(
                    int(cohort.probation_outstanding_high_water),
                    int(cohort.probation_outstanding_total),
                )
            elif lifecycle in {
                Tier3ServiceCohortEligibility.PARENT_PROVEN.value,
                Tier3ServiceCohortEligibility.PROVEN_50.value,
                Tier3ServiceCohortEligibility.PROVEN_75.value,
            }:
                cohort.proven_outstanding_total += 1
                cohort.proven_outstanding_high_water = max(
                    int(cohort.proven_outstanding_high_water),
                    int(cohort.proven_outstanding_total),
                )
            state.reward_flight_open = True
            state.reward_flight_scope = "service"
            state.reward_flight_key = cohort_key
            state.reward_flight_lifecycle = lifecycle
            self._reindex_ticket_unlocked(state)
            total_outstanding = self._cohort_counter_total_unlocked(
                "production_outstanding_total"
            )
            probation_outstanding = self._cohort_counter_total_unlocked(
                "probation_outstanding_total"
            )
            self._reward_service_outstanding_high_water = max(
                int(self._reward_service_outstanding_high_water),
                total_outstanding,
            )
            self._reward_probation_outstanding_high_water = max(
                int(self._reward_probation_outstanding_high_water),
                probation_outstanding,
            )
            return
        if ticket.reward_scope == "similar":
            cohort.production_outstanding_total += 1
            self._note_cohort_counter_unlocked(
                "production_outstanding_total",
                cohort_key,
            )
            cohort.production_outstanding_high_water = max(
                int(cohort.production_outstanding_high_water),
                int(cohort.production_outstanding_total),
            )
            if lifecycle == "similar_probation":
                cohort.probation_outstanding_total += 1
                self._note_cohort_counter_unlocked(
                    "probation_outstanding_total",
                    cohort_key,
                )
                cohort.probation_outstanding_high_water = max(
                    int(cohort.probation_outstanding_high_water),
                    int(cohort.probation_outstanding_total),
                )
            else:
                cohort.proven_outstanding_total += 1
                cohort.proven_outstanding_high_water = max(
                    int(cohort.proven_outstanding_high_water),
                    int(cohort.proven_outstanding_total),
                )
            state.reward_flight_open = True
            state.reward_flight_scope = "similar"
            state.reward_flight_key = cohort_key
            state.reward_flight_lifecycle = lifecycle
            self._reindex_ticket_unlocked(state)
            self._reward_similar_outstanding_high_water = max(
                int(self._reward_similar_outstanding_high_water),
                self._cohort_counter_total_unlocked(
                    "production_outstanding_total",
                    "similar",
                ),
            )
            return
        if lifecycle == Tier3ServiceCohortEligibility.LEAF_CONFIRMATION.value:
            cohort.confirmation_outstanding_total += 1
            self._note_cohort_counter_unlocked(
                "confirmation_outstanding_total",
                cohort_key,
            )
            cohort.confirmation_outstanding_high_water = max(
                int(cohort.confirmation_outstanding_high_water),
                int(cohort.confirmation_outstanding_total),
            )
            state.reward_flight_open = True
            state.reward_flight_scope = "confirmation"
            state.reward_flight_key = cohort_key
            state.reward_flight_lifecycle = lifecycle
            self._reindex_ticket_unlocked(state)
            return
        if lifecycle == Tier3ServiceCohortEligibility.PARENT_DISCOVERY.value:
            cohort.discovery_outstanding_total += 1
            self._note_cohort_counter_unlocked(
                "discovery_outstanding_total",
                cohort_key,
            )
            cohort.discovery_outstanding_high_water = max(
                int(cohort.discovery_outstanding_high_water),
                int(cohort.discovery_outstanding_total),
            )
            state.reward_flight_open = True
            state.reward_flight_scope = "discovery"
            state.reward_flight_key = cohort_key
            state.reward_flight_lifecycle = lifecycle
            self._reindex_ticket_unlocked(state)
            total_discovery = self._cohort_counter_total_unlocked(
                "discovery_outstanding_total"
            )
            self._reward_discovery_outstanding_high_water = max(
                int(self._reward_discovery_outstanding_high_water),
                total_discovery,
            )

    def _mark_reward_pass_pending_unlocked(self, state: _TicketState) -> None:
        if not state.reward_flight_open or state.reward_pass_pending_commit:
            return
        state.reward_pass_pending_commit = True
        state.reward_flight_outcome = Tier3SchedulingOutcome.PASS_PENDING_COMMIT.value
        self._scheduling_outcome_counts[
            Tier3SchedulingOutcome.PASS_PENDING_COMMIT.value
        ] += 1
        if state.reward_flight_scope == "exact":
            grant = self._reward_grants_by_id.get(
                int(state.ticket.reward_grant_id or 0)
            )
            if grant is not None:
                grant.pass_pending_count += 1

    def _resolve_reward_flight_unlocked(
        self,
        state: _TicketState,
        outcome: Tier3SchedulingOutcome,
        *,
        busy_worker_ms: int = 0,
    ) -> None:
        if not state.reward_flight_open:
            return
        state.reward_flight_open = False
        self._reindex_ticket_unlocked(state)
        state.reward_flight_outcome = outcome.value
        self._scheduling_outcome_counts[outcome.value] += 1
        if state.reward_flight_scope == "exact":
            grant = self._reward_grants_by_id.get(
                int(state.ticket.reward_grant_id or 0)
            )
            if grant is not None:
                grant.outstanding_handoffs = max(
                    0,
                    int(grant.outstanding_handoffs) - 1,
                )
                if state.reward_pass_pending_commit:
                    grant.pass_pending_count = max(
                        0,
                        int(grant.pass_pending_count) - 1,
                    )
                grant.resolved_handoffs += 1
                if outcome is Tier3SchedulingOutcome.NET_NEW:
                    grant.net_new_count += 1
                if grant.outstanding_handoffs <= 0:
                    if grant.expired:
                        self._reward_grants_by_id.pop(grant.grant_id, None)
                    elif self._reward_credit_remaining(grant) <= 0:
                        self._expire_family_reward_unlocked(
                            (grant.origin_root, grant.origin_family),
                            grant,
                        )
            self._reward_exact_resolved_count += 1
            if outcome is Tier3SchedulingOutcome.NEUTRAL_SKIP:
                self._reward_exact_neutral_count += 1
            elif outcome in {
                Tier3SchedulingOutcome.TCP_CENSORED,
                Tier3SchedulingOutcome.VALIDATION_CENSORED,
                Tier3SchedulingOutcome.CANCELLED,
                Tier3SchedulingOutcome.FACILITY,
            }:
                self._reward_exact_censored_count += 1
        elif state.reward_flight_scope == "service":
            cohort = self._cohort_yield_states.get(state.reward_flight_key)
            if cohort is not None:
                cohort.production_outstanding_total = max(
                    0,
                    int(cohort.production_outstanding_total) - 1,
                )
                if (
                    state.reward_flight_lifecycle
                    in {
                        Tier3ServiceCohortEligibility.LEAF_PROBATION.value,
                        Tier3ServiceCohortEligibility.PARENT_PROBATION.value,
                    }
                ):
                    cohort.probation_outstanding_total = max(
                        0,
                        int(cohort.probation_outstanding_total) - 1,
                    )
                elif state.reward_flight_lifecycle in {
                    Tier3ServiceCohortEligibility.PARENT_PROVEN.value,
                    Tier3ServiceCohortEligibility.PROVEN_50.value,
                    Tier3ServiceCohortEligibility.PROVEN_75.value,
                }:
                    cohort.proven_outstanding_total = max(
                        0,
                        int(cohort.proven_outstanding_total) - 1,
                    )
                if outcome is Tier3SchedulingOutcome.NET_NEW:
                    cohort.production_resolved_total += 1
                    cohort.production_net_new_total += 1
                    self._record_service_production_holdout_unlocked(
                        state.ticket,
                        passed=True,
                        censored=False,
                        busy_worker_ms=busy_worker_ms,
                    )
                elif outcome in {
                    Tier3SchedulingOutcome.TCP_DEAD,
                    Tier3SchedulingOutcome.VALIDATION_FAIL,
                }:
                    cohort.production_resolved_total += 1
                    self._record_service_production_holdout_unlocked(
                        state.ticket,
                        passed=False,
                        censored=False,
                        busy_worker_ms=busy_worker_ms,
                    )
                elif outcome is Tier3SchedulingOutcome.NEUTRAL_SKIP:
                    cohort.production_neutral_total += 1
                else:
                    cohort.production_censored_total += 1
            self._reward_service_resolved_count += 1
            if outcome is Tier3SchedulingOutcome.NEUTRAL_SKIP:
                self._reward_service_neutral_count += 1
            elif outcome not in {
                Tier3SchedulingOutcome.NET_NEW,
                Tier3SchedulingOutcome.TCP_DEAD,
                Tier3SchedulingOutcome.VALIDATION_FAIL,
            }:
                self._reward_service_censored_count += 1
        elif state.reward_flight_scope == "similar":
            cohort = self._cohort_yield_states.get(state.reward_flight_key)
            if cohort is not None:
                cohort.production_outstanding_total = max(
                    0,
                    int(cohort.production_outstanding_total) - 1,
                )
                if state.reward_flight_lifecycle == "similar_probation":
                    cohort.probation_outstanding_total = max(
                        0,
                        int(cohort.probation_outstanding_total) - 1,
                    )
                else:
                    cohort.proven_outstanding_total = max(
                        0,
                        int(cohort.proven_outstanding_total) - 1,
                    )
                if outcome is Tier3SchedulingOutcome.NET_NEW:
                    cohort.production_resolved_total += 1
                    cohort.production_net_new_total += 1
                    self._record_similar_production_holdout_unlocked(
                        cohort,
                        passed=True,
                        censored=False,
                        busy_worker_ms=busy_worker_ms,
                    )
                elif outcome in {
                    Tier3SchedulingOutcome.TCP_DEAD,
                    Tier3SchedulingOutcome.VALIDATION_FAIL,
                }:
                    cohort.production_resolved_total += 1
                    self._record_similar_production_holdout_unlocked(
                        cohort,
                        passed=False,
                        censored=False,
                        busy_worker_ms=busy_worker_ms,
                    )
                elif outcome is Tier3SchedulingOutcome.NEUTRAL_SKIP:
                    cohort.production_neutral_total += 1
                else:
                    cohort.production_censored_total += 1
            self._reward_similar_resolved_count += 1
            if outcome is Tier3SchedulingOutcome.NEUTRAL_SKIP:
                self._reward_similar_neutral_count += 1
            elif outcome not in {
                Tier3SchedulingOutcome.NET_NEW,
                Tier3SchedulingOutcome.TCP_DEAD,
                Tier3SchedulingOutcome.VALIDATION_FAIL,
            }:
                self._reward_similar_censored_count += 1
        elif state.reward_flight_scope == "discovery":
            cohort = self._cohort_yield_states.get(state.reward_flight_key)
            if cohort is not None:
                cohort.discovery_outstanding_total = max(
                    0,
                    int(cohort.discovery_outstanding_total) - 1,
                )
        elif state.reward_flight_scope == "confirmation":
            cohort = self._cohort_yield_states.get(state.reward_flight_key)
            if cohort is not None:
                reset_reason = ""
                cohort.confirmation_outstanding_total = max(
                    0,
                    int(cohort.confirmation_outstanding_total) - 1,
                )
                if state.confirmation_epoch != cohort.confirmation_epoch:
                    # Old flights still release capacity, but cannot reject
                    # evidence collected in a subsequent confirmation window.
                    cohort.confirmation_stale_transition_ignored_total += 1
                    state.reward_pass_pending_commit = False
                    return
                if outcome in {
                    Tier3SchedulingOutcome.NET_NEW,
                    Tier3SchedulingOutcome.NEUTRAL_SKIP,
                } and state.reward_pass_pending_commit:
                    cohort.confirmation_completion_total += 1
                    cohort.confirmation_pass_total += 1
                elif outcome in {
                    Tier3SchedulingOutcome.TCP_DEAD,
                    Tier3SchedulingOutcome.VALIDATION_FAIL,
                }:
                    cohort.confirmation_completion_total += 1
                    reset_reason = "leaf_confirmation_failed"
                else:
                    cohort.confirmation_censored_total += 1
                    if cohort.confirmation_censored_total >= 2:
                        reset_reason = "leaf_confirmation_twice_censored"
                if reset_reason:
                    # Release and count the old flight, but only its original
                    # confirmation state may be reset by its late result.
                    if self._service_eligibility_unlocked(cohort) is (
                        Tier3ServiceCohortEligibility.LEAF_CONFIRMATION
                    ):
                        self._reset_cohort_to_baseline_unlocked(
                            cohort,
                            reason=reset_reason,
                        )
                    else:
                        cohort.confirmation_stale_transition_ignored_total += 1
        state.reward_pass_pending_commit = False

    def _advance_committed_handoff_epoch_unlocked(
        self,
        states: list[_TicketState],
    ) -> None:
        if not states:
            return
        exploit_selected = 0
        unknown_selected = 0
        escape_selected = 0
        handoffs_by_bucket: dict[tuple[str, str], int] = {}
        for state in states:
            self._open_reward_flight_unlocked(state)
            service_lifecycle = str(state.ticket.reward_cohort_state or "")
            self._probation_handoff_history.append(
                str(state.ticket.reward_cohort_key or "")
                if state.ticket.selection_reason == "reward"
                and state.ticket.reward_probation
                else ""
            )
            self._service_handoff_history.append(
                (
                    str(state.ticket.reward_cohort_key or ""),
                    service_lifecycle,
                )
                if service_lifecycle == "parent_discovery"
                or (
                    state.ticket.selection_reason == "reward"
                    and state.ticket.reward_scope == "service"
                )
                else ("", "")
            )
            if service_lifecycle == "parent_discovery":
                discovery_state = self._cohort_yield_states.get(
                    str(state.ticket.reward_cohort_key or "")
                )
                if discovery_state is not None:
                    discovery_state.discovery_selection_total += 1
                    try:
                        parent_index = state.ticket.service_cohort_keys.index(
                            str(state.ticket.reward_cohort_key or "")
                        )
                    except ValueError:
                        parent_index = -1
                    if (
                        0 <= parent_index
                        < len(state.ticket.service_cohort_keys) - 1
                    ):
                        discovery_state.discovery_selected_child_keys.add(
                            state.ticket.service_cohort_keys[parent_index + 1]
                        )
                    if discovery_state.discovery_selection_total >= 8:
                        discovery_state.discovery_exhausted = True
                        self._dirty_cohort_keys.add(
                            str(state.ticket.reward_cohort_key or "")
                        )
                    discovery_state.discovery_last_selected_handoff_total = int(
                        self._tcp_handoff_total
                    )
            elif service_lifecycle == "leaf_confirmation":
                confirmation_state = self._cohort_yield_states.get(
                    str(state.ticket.reward_cohort_key or "")
                )
                if (
                    confirmation_state is not None
                    and state.confirmation_epoch == confirmation_state.confirmation_epoch
                ):
                    confirmation_state.confirmation_selection_total += 1
            arm = self._arms.get(state.arm_id)
            if arm is not None:
                arm.selected_count += 1
            bucket = (state.ticket.source_root, state.ticket.family)
            handoffs_by_bucket[bucket] = int(handoffs_by_bucket.get(bucket, 0)) + 1
            for peer_arm_id in state.fairness_peer_arm_ids:
                peer = self._arms.get(peer_arm_id)
                if peer is None:
                    continue
                if peer_arm_id == state.arm_id:
                    peer.fairness_debt = 0
                else:
                    peer.fairness_debt = min(2, peer.fairness_debt + 1)
            reason = state.ticket.selection_reason
            if reason == "reward" and state.ticket.reward_scope == "service":
                cohort_state = self._cohort_yield_states.get(
                    str(state.ticket.reward_cohort_key or "")
                )
                if cohort_state is not None:
                    cohort_state.production_selection_total += 1
                    if service_lifecycle == "leaf_probation":
                        cohort_state.probation_selection_total += 1
                    elif service_lifecycle in {"proven_50", "proven_75"}:
                        cohort_state.proven_selection_total += 1
                decision = self._cohort_decisions_by_ticket.get(
                    int(state.ticket.ticket_id)
                )
                if decision is not None:
                    decision["handoff_committed"] = True
            elif reason == "reward" and state.ticket.reward_scope == "similar":
                cohort_state = self._cohort_yield_states.get(
                    str(state.ticket.reward_cohort_key or "")
                )
                if cohort_state is not None:
                    cohort_state.production_selection_total += 1
                    if service_lifecycle == "similar_probation":
                        cohort_state.probation_selection_total += 1
                    else:
                        cohort_state.proven_selection_total += 1
                decision = self._cohort_decisions_by_ticket.get(
                    int(state.ticket.ticket_id)
                )
                if decision is not None:
                    decision["handoff_committed"] = True
            if state.ticket.coverage_selected:
                self._coverage_selected_total += 1
            self._handoff_lane_history.append(
                (
                    bool(state.ticket.coverage_selected),
                    bool(state.ticket.unknown_exploration),
                    bool(reason == "reward"),
                )
            )
            self._visibility_handoff_history.append(
                str(state.ticket.visibility_class or "unknown")
            )
            if service_lifecycle == "parent_discovery":
                selection_lane = "service_discovery"
            elif service_lifecycle == "leaf_confirmation":
                selection_lane = "leaf_confirmation"
            elif reason in {"cold_leaf_coverage", "replay_leaf_discovery"}:
                selection_lane = reason
            elif reason == "reward" and state.ticket.reward_scope == "exact":
                selection_lane = "exact"
            elif (
                reason == "reward"
                and state.ticket.reward_scope == "service"
                and service_lifecycle == "parent_discovery"
            ):
                selection_lane = "service_discovery"
            elif (
                reason == "reward"
                and state.ticket.reward_scope == "service"
                and state.ticket.reward_probation
            ):
                selection_lane = "service_probation"
            elif reason == "reward" and state.ticket.reward_scope == "service":
                selection_lane = "service_proven"
            elif reason == "reward" and state.ticket.reward_scope == "similar":
                selection_lane = "network_similar"
            elif reason in {"coverage", "exploration"}:
                selection_lane = reason
            else:
                selection_lane = "ordinary"
            self._handoff_selection_history.append(selection_lane)
            active_family_grant = self._family_reward_grant(bucket)
            grant = self._reward_grants_by_id.get(
                int(state.ticket.reward_grant_id)
            )
            opportunity_scope = str(state.ticket.reward_scope or "none")
            if (
                opportunity_scope == "none"
                and active_family_grant is not None
            ):
                opportunity_scope = "shadow_broad"
            if opportunity_scope == "shadow_broad":
                self._reward_shadow_broad_handoff_count += 1
            if reason == "reward":
                exploit_selected += 1
                self._exploit_selected_total += 1
            if reason == "reward":
                self._reward_hit_count += 1
                if opportunity_scope == "exact":
                    self._reward_exact_hit_count += 1
                    if grant is not None:
                        grant.used = True
                        grant.eligible_handoff_opportunities += 1
                        grant.exact_handoffs_remaining = max(
                            0,
                            grant.exact_handoffs_remaining - 1,
                        )
                elif opportunity_scope == "service":
                    self._reward_service_hit_count += 1
                    if grant is not None:
                        grant.used = True
                        grant.service_handoffs_remaining = max(
                            0,
                            grant.service_handoffs_remaining - 1,
                        )
                elif opportunity_scope == "similar":
                    self._reward_similar_hit_count += 1
                if grant is not None:
                    if not grant.first_handoff_recorded:
                        latency_ms = max(
                            0.0,
                            (time.monotonic_ns() - grant.created_monotonic_ns)
                            / 1_000_000.0,
                        )
                        grant.first_handoff_recorded = True
                        self._reward_first_handoff_latency_last_ms = latency_ms
                        self._reward_first_handoff_latency_max_ms = max(
                            self._reward_first_handoff_latency_max_ms,
                            latency_ms,
                        )
            # Exact reward credit is consumed only by an actual exact-lane
            # handoff. Unrelated work in the origin bucket cannot age or erase
            # the grant; the independent wall-clock TTL remains the stale-state
            # bound.
            if grant is not None:
                self._refresh_reward_grant_lifetime_unlocked(grant)
                if (
                    self._reward_credit_remaining(grant) <= 0
                    and grant.outstanding_handoffs <= 0
                ):
                    self._expire_family_reward_unlocked(bucket, grant)
            if state.ticket.unknown_exploration:
                unknown_selected += 1
                self._unknown_selected_total += 1
            if reason == "credit_escape":
                escape_selected += 1
                self._credit_escape_selected_total += 1
        for bucket, count in handoffs_by_bucket.items():
            credit = self._bucket_credit_for(bucket)
            credit.committed_handoff_count += max(0, int(count))
        self._last_handoff_count = len(states)
        self._last_wave_root_coverage = len(
            {state.ticket.source_root for state in states}
        )
        self._last_wave_family_coverage = len(
            {(state.ticket.source_root, state.ticket.family) for state in states}
        )
        self._last_tick_exploit_selected = exploit_selected
        self._last_tick_unknown_selected = unknown_selected
        self._last_tick_credit_escape_selected = escape_selected
        self._advance_credit_escape_debt(
            {
                (state.ticket.source_root, state.ticket.family)
                for state in states
            }
        )

    def commit_handoffs(
        self,
        reservations: list[
            Tier3CandidateHandoffReservation
            | Tier3CandidateFeedbackTicket
            | Mapping[str, Any]
        ],
    ) -> tuple[Tier3CandidateFeedbackTicket, ...]:
        """Commit one real TCP wave and advance fairness exactly once."""

        with self._lock:
            committed: list[_TicketState] = []
            for reservation in reservations:
                ticket = (
                    reservation.ticket
                    if isinstance(reservation, Tier3CandidateHandoffReservation)
                    else reservation
                )
                state = self._resolve_ticket(ticket)
                if state is None:
                    continue
                if self._commit_handoff_unlocked(state):
                    committed.append(state)
            self._advance_committed_handoff_epoch_unlocked(committed)
            return tuple(state.ticket for state in committed)

    def commit_handoff(
        self,
        reservation: (
            Tier3CandidateHandoffReservation
            | Tier3CandidateFeedbackTicket
            | Mapping[str, Any]
            | None
        ),
    ) -> bool:
        return bool(self.commit_handoffs([reservation]))

    def _requeue_candidate_unlocked(
        self,
        candidate: ScheduledTier3Candidate,
    ) -> None:
        candidate.dispatch_ticket = None
        candidate.node.extra.pop("tier3_candidate_dispatch_ticket", None)
        candidate.node.extra.pop(
            "tier3_candidate_dispatch_selection_reason",
            None,
        )
        arm_id = self._arm_id(
            candidate.source_root,
            candidate.family,
            candidate.coarse_key,
        )
        arm = self._arms.get(arm_id)
        if arm is None:
            arm = _Arm(
                arm_id=arm_id,
                source_root=candidate.source_root,
                family=candidate.family,
                coarse_key=candidate.coarse_key,
                feature_key=candidate.feature_key,
            )
            self._arms[arm_id] = arm
            self._ensure_root_family(candidate.source_root, candidate.family)
            self._activate_arm(arm)
        elif not arm.items and not arm.active:
            self._activate_arm(arm)
        arm_was_empty = not arm.items
        arm.items.appendleft(candidate)
        self._lookahead_index.add(
            candidate,
            arm_id=arm.arm_id,
            evidence=self._lookahead_evidence_for_candidate_unlocked(candidate),
            returned=True,
        )
        self._visibility_inventory_exhausted_at_ms.pop(
            str(candidate.visibility_class or "unknown"),
            None,
        )
        reevaluate_discovery = self._record_candidate_cohort_inventory_unlocked(
            candidate,
            arm_id=arm.arm_id,
            delta=1,
        )
        self._record_candidate_enqueued_unlocked(
            arm,
            arm_was_empty=arm_was_empty,
        )
        self._arm_ids_by_feedback_key.setdefault(
            candidate.arm_key,
            set(),
        ).add(arm_id)
        self._backlog_count += 1
        self._root_backlog_counts[candidate.source_root] = (
            self._root_backlog_counts.get(candidate.source_root, 0) + 1
        )
        if candidate.source_root not in self._active_roots:
            self._active_roots.add(candidate.source_root)
            self._root_order.append(candidate.source_root)
        if reevaluate_discovery:
            self._evaluate_dirty_service_cohorts_unlocked()

    def cancel_handoff(
        self,
        reservation: (
            Tier3CandidateHandoffReservation
            | Tier3CandidateFeedbackTicket
            | Mapping[str, Any]
            | None
        ),
        *,
        requeue: bool,
    ) -> bool:
        ticket = (
            reservation.ticket
            if isinstance(reservation, Tier3CandidateHandoffReservation)
            else reservation
        )
        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None or state.tcp_handed_off:
                return False
            candidate = state.candidate
            self._release_ticket_credit(
                state,
                terminal=True,
                decision_outcome="reservation_cancelled",
            )
            if requeue:
                self._requeue_candidate_unlocked(candidate)
            return True

    def on_tcp_handoff(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
    ) -> None:
        """Record a real TCP queue handoff, distinct from scheduler selection."""

        self.commit_handoff(ticket)

    def record_feedback(
        self,
        arm_key: str,
        *,
        responsive: bool = False,
        passed: bool = False,
    ) -> None:
        with self._lock:
            for arm_id in self._arm_ids_by_feedback_key.get(str(arm_key or ""), set()):
                arm = self._arms.get(arm_id)
                if arm is None:
                    continue
                arm.tcp_observed = bool(arm.tcp_observed or responsive)
                # TCP responsiveness is only a weak within-family hint. It
                # does not turn an arm into a validation-observed arm.
                if passed:
                    arm.validation_observed = True
                    arm.validation_pending = False
                    if self._legacy_structural_routing_for_test:
                        self._grant_reward_unlocked(
                            arm,
                            arm_key=str(arm_key or arm.arm_id),
                            feature_key=arm.feature_key,
                        )

    def on_tcp_outcome(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
        *,
        responsive: bool,
        cancelled: bool = False,
        hard_censored: bool = False,
        validation_required: bool = False,
    ) -> None:
        """End the TCP lease and retain only immutable validation feedback state."""

        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None or state.phase not in {"reserved", "tcp_active"}:
                return
            if cancelled and not hard_censored:
                # A deferred/cancelled probe is not an observation about the
                # candidate arm. Release the dispatch credit, but leave the
                # arm eligible for unknown/exploration selection when it is
                # retried later.
                self._release_handoff_capacity_unlocked(
                    state,
                    cancelled_before_handoff=not state.tcp_handed_off,
                )
                self._resolve_reward_flight_unlocked(
                    state,
                    Tier3SchedulingOutcome.CANCELLED,
                )
                self._release_ticket_credit(
                    state,
                    terminal=True,
                    decision_outcome="tcp_cancelled",
                )
                return
            committed = self._commit_handoff_unlocked(state)
            if committed:
                self._advance_committed_handoff_epoch_unlocked([state])
            if hard_censored:
                self._record_visibility_resolution_unlocked(
                    state,
                    Tier3SchedulingOutcome.TCP_CENSORED,
                )
                self._record_tcp_window_outcome_unlocked(
                    state.ticket.ticket_id,
                    responsive=False,
                    hard_censored=True,
                )
                self._tcp_hard_censored_count += 1
                self._release_handoff_capacity_unlocked(state)
                self._record_ordinary_e2e_outcome_unlocked(
                    state,
                    Tier3SchedulingOutcome.TCP_CENSORED,
                )
                self._resolve_reward_flight_unlocked(
                    state,
                    Tier3SchedulingOutcome.TCP_CENSORED,
                )
                self._release_ticket_credit(
                    state,
                    terminal=True,
                    decision_outcome="tcp_hard_censored",
                )
                return
            self._tcp_outcome_count += 1
            arm = self._arms.get(state.arm_id)
            if arm is not None:
                arm.tcp_observed = True
                arm.tcp_completed += 1
                if responsive:
                    arm.tcp_responsive += 1
                if responsive or validation_required:
                    if not arm.validation_observed:
                        arm.validation_pending = True
            if responsive:
                self._visibility_responsive_total[
                    state.ticket.visibility_class
                ] += 1
                signal = self._replay_leaf_discoveries.get(state.replay_discovery_key)
                if signal is not None and signal.ticket_id == state.ticket.ticket_id:
                    signal.responsive += 1
                scope = self._ticket_reward_metric_scope(state.ticket)
                if scope == "exact":
                    self._reward_exact_tcp_responsive_count += 1
                elif scope == "service":
                    self._reward_service_tcp_responsive_count += 1
                elif scope == "similar":
                    self._reward_similar_tcp_responsive_count += 1
                elif scope == "shadow_broad":
                    self._reward_shadow_broad_tcp_responsive_count += 1
            self._record_tcp_window_outcome_unlocked(
                state.ticket.ticket_id,
                responsive=responsive,
            )
            self._release_handoff_capacity_unlocked(state)
            if responsive or validation_required:
                state.phase = "feedback_only"
                state.updated_at = time.monotonic()
                self._reindex_ticket_unlocked(state)
                return
            self._resolve_reward_flight_unlocked(
                state,
                Tier3SchedulingOutcome.TCP_DEAD,
            )
            self._record_visibility_resolution_unlocked(
                state,
                Tier3SchedulingOutcome.TCP_DEAD,
            )
            self._record_ordinary_e2e_outcome_unlocked(
                state,
                Tier3SchedulingOutcome.TCP_DEAD,
            )
            self._release_ticket_credit(
                state,
                terminal=True,
                decision_outcome="tcp_dead",
            )

    def on_fast_yield(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
        *,
        continued: bool = True,
        outcome: str = "",
        busy_worker_ms: int = 0,
        right_censored: bool = False,
        facility_failure: bool = False,
    ) -> None:
        """Move one responsive candidate from FAST credit to parked-DEEP debt."""

        if not continued:
            return
        right_censored = bool(
            right_censored
            or str(outcome or "").strip().lower() in {"transient", "right_censored"}
        )
        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None or state.phase not in {
                "reserved",
                "tcp_active",
                "feedback_only",
            }:
                return
            committed = self._commit_handoff_unlocked(state)
            if committed:
                self._advance_committed_handoff_epoch_unlocked([state])
            self._release_handoff_capacity_unlocked(state)
            arm = self._arms.get(state.arm_id)
            if arm is not None:
                if not right_censored:
                    arm.validation_observed = True
                arm.validation_pending = False
                arm.parked_deep_debt += 1
                bucket_credit = self._bucket_credit(arm)
                bucket_credit.parked_deep_debt += 1
            self._parked_deep_debt += 1
            state.phase = "parked_deep"
            state.fast_continuation_accepted = True
            state.updated_at = time.monotonic()
            self._reindex_ticket_unlocked(state)
            self._fast_yield_count += 1
            # One-version compatibility for direct callers that do not yet
            # publish the actor-accepted FAST observation. Production emits
            # that observation first, so ticket-id deduplication makes this a
            # no-op and retention policy cannot overwrite the attempt class.
            if not facility_failure:
                self._record_ticket_fast_yield_samples_unlocked(
                    state.ticket,
                    passed=False,
                    censored=bool(right_censored),
                    busy_worker_ms=busy_worker_ms,
                )
                if not right_censored and state.reward_flight_open:
                    self._resolve_reward_flight_unlocked(
                        state,
                        Tier3SchedulingOutcome.VALIDATION_FAIL,
                        busy_worker_ms=busy_worker_ms,
                    )
                if not right_censored:
                    self._record_ordinary_e2e_outcome_unlocked(
                        state,
                        Tier3SchedulingOutcome.VALIDATION_FAIL,
                        busy_worker_ms=busy_worker_ms,
                    )

    def on_fast_observation(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
        *,
        outcome: str = "",
        busy_worker_ms: int = 0,
        right_censored: bool = False,
        facility_failure: bool = False,
        passed: bool = False,
    ) -> None:
        """Record the accepted FAST result without changing transaction credit."""

        if facility_failure:
            return
        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None or state.phase.startswith("terminal"):
                return
            committed = self._commit_handoff_unlocked(state)
            if committed:
                self._advance_committed_handoff_epoch_unlocked([state])
            outcome_name = str(outcome or "").strip().lower()
            censored = bool(
                right_censored
                or outcome_name in {"transient", "right_censored"}
            )
            is_pass = bool(passed or outcome_name == "pass")
            self._record_ticket_fast_yield_samples_unlocked(
                state.ticket,
                passed=is_pass,
                censored=censored,
                busy_worker_ms=max(0, int(busy_worker_ms)),
            )
            arm = self._arms.get(state.arm_id)
            if arm is not None:
                if not censored or is_pass:
                    arm.validation_observed = True
                arm.validation_pending = False
            if is_pass:
                state.reward_pending_worker_ms = max(0, int(busy_worker_ms))
                self._mark_reward_pass_pending_unlocked(state)
                return
            if censored:
                return
            if state.reward_flight_open:
                self._resolve_reward_flight_unlocked(
                    state,
                    Tier3SchedulingOutcome.VALIDATION_FAIL,
                    busy_worker_ms=max(0, int(busy_worker_ms)),
                )
            self._record_ordinary_e2e_outcome_unlocked(
                state,
                Tier3SchedulingOutcome.VALIDATION_FAIL,
                busy_worker_ms=max(0, int(busy_worker_ms)),
            )

    @staticmethod
    def _wilson_bound(successes: int, trials: int, *, lower: bool) -> float:
        trials = max(0, int(trials))
        successes = max(0, min(trials, int(successes)))
        if trials <= 0:
            return 0.0 if lower else 1.0
        z = 1.96
        z2 = z * z
        proportion = successes / float(trials)
        denominator = 1.0 + z2 / trials
        center = proportion + z2 / (2.0 * trials)
        margin = z * math.sqrt(
            (proportion * (1.0 - proportion) / trials)
            + (z2 / (4.0 * trials * trials))
        )
        value = (center - margin if lower else center + margin) / denominator
        return max(0.0, min(1.0, value))

    @staticmethod
    def _trim_cohort_samples_unlocked(
        state: _CohortYieldState,
        now: float,
    ) -> None:
        while state.samples and now - state.samples[0][0] > 60.0:
            state.samples.popleft()

    @staticmethod
    def _cohort_window_counts(
        state: _CohortYieldState,
    ) -> tuple[list[tuple[float, bool, bool, int]], int]:
        completed = [item for item in state.samples if not item[2]]
        return completed, sum(1 for item in completed if item[1])

    def _set_service_eligibility_unlocked(
        self,
        state: _CohortYieldState,
        eligibility: Tier3ServiceCohortEligibility,
        *,
        reason: str,
    ) -> None:
        previous = state.eligibility
        state.eligibility = eligibility
        self._move_cohort_eligibility_unlocked(state, previous)
        state.discovery = (
            eligibility == Tier3ServiceCohortEligibility.PARENT_DISCOVERY
        )
        state.probation = eligibility in {
            Tier3ServiceCohortEligibility.PARENT_PROBATION,
            Tier3ServiceCohortEligibility.LEAF_PROBATION,
        }
        state.promoted = eligibility in {
            Tier3ServiceCohortEligibility.PARENT_PROVEN,
            Tier3ServiceCohortEligibility.PROVEN_50,
            Tier3ServiceCohortEligibility.PROVEN_75,
        }
        state.lifecycle_state = {
            Tier3ServiceCohortEligibility.PARENT_DISCOVERY: "discovery",
            Tier3ServiceCohortEligibility.LEAF_CONFIRMATION: (
                "leaf_confirmation"
            ),
            Tier3ServiceCohortEligibility.PARENT_PROBATION: "parent_probation",
            Tier3ServiceCohortEligibility.PARENT_PROVEN: "parent_proven",
            Tier3ServiceCohortEligibility.LEAF_PROBATION: "probation",
        }.get(eligibility, eligibility.value)
        parent_exploit_share = {
            0: 0.25,
            1: 0.375,
            2: 0.50,
        }.get(int(state.hierarchy_level), 0.25)
        state.exploit_share = {
            Tier3ServiceCohortEligibility.PARENT_PROVEN: parent_exploit_share,
            Tier3ServiceCohortEligibility.PROVEN_50: 0.50,
            Tier3ServiceCohortEligibility.PROVEN_75: 0.75,
        }.get(eligibility, self.exploit_scan_share)
        state.last_decision_reason = reason
        if (
            eligibility == Tier3ServiceCohortEligibility.PARENT_DISCOVERY
            and eligibility != previous
        ):
            state.discovery_eligible_handoff_total = int(
                self._tcp_handoff_total
            )
            state.discovery_last_selected_handoff_total = 0
        if (
            eligibility == Tier3ServiceCohortEligibility.LEAF_CONFIRMATION
            and eligibility != previous
        ):
            state.confirmation_epoch += 1
            state.confirmation_selection_total = 0
            state.confirmation_completion_total = 0
            state.confirmation_pass_total = 0
            state.confirmation_censored_total = 0
            state.confirmation_evidence_pass_total = state.pass_total
            state.confirmation_eligible_handoff_total = int(
                self._tcp_handoff_total
            )
        if eligibility in {
            Tier3ServiceCohortEligibility.PARENT_PROBATION,
            Tier3ServiceCohortEligibility.PARENT_PROVEN,
            Tier3ServiceCohortEligibility.LEAF_PROBATION,
            Tier3ServiceCohortEligibility.PROVEN_50,
            Tier3ServiceCohortEligibility.PROVEN_75,
        } and eligibility != previous:
            if (
                not self._legacy_structural_routing_for_test
                and eligibility in {
                    Tier3ServiceCohortEligibility.PARENT_PROVEN,
                    Tier3ServiceCohortEligibility.PROVEN_50,
                    Tier3ServiceCohortEligibility.PROVEN_75,
                }
            ):
                state.production_state_started_completion_total = (
                    state.proven_completion_total
                )
                state.production_state_started_pass_total = state.proven_pass_total
                state.production_state_started_selection_total = (
                    state.proven_selection_total
                )
            else:
                state.production_state_started_completion_total = (
                    state.production_completion_total
                )
                state.production_state_started_pass_total = (
                    state.production_pass_total
                )
                state.production_state_started_selection_total = (
                    state.production_selection_total
                )
            state.production_consecutive_nonpass = 0
            state.production_samples.clear()

    def _reset_cohort_to_baseline_unlocked(
        self,
        state: _CohortYieldState,
        *,
        reason: str,
        zero_pass: bool = False,
    ) -> None:
        was_active = bool(
            state.discovery
            or state.probation
            or state.promoted
            or state.exploit_share > self.exploit_scan_share
        )
        if was_active:
            state.demotion_count += 1
        if state.discovery:
            self._service_discovery_exit_count += 1
        if state.probation:
            self._service_probation_exit_count += 1
        if zero_pass:
            state.zero_pass_demotion_count += 1
        self._set_service_eligibility_unlocked(
            state,
            (
                Tier3ServiceCohortEligibility.DEMOTED
                if was_active
                else Tier3ServiceCohortEligibility.OBSERVE
            ),
            reason=reason,
        )
        state.winning_windows = 0
        state.losing_windows = 0
        state.advantage_confirmed = False
        self._refresh_adaptive_exploit_share_unlocked()

    def _record_ordinary_e2e_outcome_unlocked(
        self,
        state: _TicketState,
        outcome: Tier3SchedulingOutcome,
        *,
        busy_worker_ms: int = 0,
    ) -> None:
        ticket = state.ticket
        if (
            ticket.selection_reason == "reward"
            or ticket.evidence_origin
            in {
                Tier3CandidateEvidenceOrigin.FRESH_REPLAY.value,
                Tier3CandidateEvidenceOrigin.HISTORICAL_REPROBE.value,
            }
            or ticket.reward_cohort_state
            == Tier3ServiceCohortEligibility.PARENT_DISCOVERY.value
            or int(ticket.ticket_id) in self._ordinary_e2e_ticket_ids
        ):
            return
        if outcome in {
            Tier3SchedulingOutcome.NEUTRAL_SKIP,
            Tier3SchedulingOutcome.CANCELLED,
            Tier3SchedulingOutcome.FACILITY,
        }:
            return
        passed = outcome is Tier3SchedulingOutcome.NET_NEW
        censored = outcome in {
            Tier3SchedulingOutcome.TCP_CENSORED,
            Tier3SchedulingOutcome.VALIDATION_CENSORED,
        }
        if not passed and not censored and outcome not in {
            Tier3SchedulingOutcome.TCP_DEAD,
            Tier3SchedulingOutcome.VALIDATION_FAIL,
        }:
            return
        self._ordinary_e2e_ticket_ids.add(int(ticket.ticket_id))
        now = time.monotonic()
        self._ordinary_e2e_samples.append(
            (
                now,
                passed,
                censored,
                max(1, int(busy_worker_ms)),
                frozenset(ticket.service_cohort_keys),
            )
        )
        if not censored:
            self._ordinary_e2e_completion_total += 1
        while (
            self._ordinary_e2e_samples
            and now - self._ordinary_e2e_samples[0][0] > 60.0
        ):
            self._ordinary_e2e_samples.popleft()

    def _ordinary_reference_for_service_cohort_unlocked(
        self,
        cohort_key: str,
        now: float,
    ) -> tuple[
        list[tuple[float, bool, bool, int]],
        list[tuple[float, bool, bool, int]],
        int,
        int,
    ]:
        while (
            self._ordinary_e2e_samples
            and now - self._ordinary_e2e_samples[0][0] > 60.0
        ):
            self._ordinary_e2e_samples.popleft()
        if self._ordinary_e2e_samples:
            samples = [
                (at, passed, censored, worker_ms)
                for at, passed, censored, worker_ms, service_keys
                in self._ordinary_e2e_samples
                if cohort_key not in service_keys
            ]
            completed = [item for item in samples if not item[2]]
            passes = sum(1 for item in completed if item[1])
            return (
                samples,
                completed,
                passes,
                self._ordinary_e2e_completion_total,
            )
        ordinary_state = self._cohort_yield_states.get("ordinary")
        if ordinary_state is None:
            return [], [], 0, 0
        self._trim_cohort_samples_unlocked(ordinary_state, now)
        samples = list(ordinary_state.samples)
        completed, passes = self._cohort_window_counts(ordinary_state)
        return samples, completed, passes, ordinary_state.completed_total

    def _l2_parent_discovery_ready_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> bool:
        if state.leaf_eligible or int(state.hierarchy_level) != 2:
            return False
        passed_children = set(state.current_pass_child_keys)
        if not passed_children:
            return False
        successful_leaf_exhausted = any(
            int(self._cohort_queued_inventory.get(child_key, 0)) <= 0
            for child_key in passed_children
        )
        if not successful_leaf_exhausted:
            return False
        # Spend the bounded discovery lane on an exact successful leaf first.
        # Only fan out to siblings after that immediately redeemable inventory
        # is exhausted; the parent still gains no production reward rights.
        live_children = self._cohort_queued_direct_children.get(
            str(cohort_key or ""),
            {},
        )
        unavailable = (
            passed_children
            | set(state.discovery_selected_child_keys)
            | self._reserved_parent_discovery_child_keys_unlocked(cohort_key)
        )
        return any(
            int(count) > 0 and child_key not in unavailable
            for child_key, count in live_children.items()
        )

    def _evaluate_service_cohort_unlocked(
        self,
        cohort_key: str,
        state: _CohortYieldState,
    ) -> bool:
        now = time.monotonic()
        self._trim_cohort_samples_unlocked(state, now)
        cohort_completed, cohort_passes = self._cohort_window_counts(state)
        (
            ordinary_samples,
            ordinary_completed,
            ordinary_passes,
            ordinary_reference_total,
        ) = self._ordinary_reference_for_service_cohort_unlocked(
            cohort_key,
            now,
        )

        if len(cohort_completed) >= 32 and cohort_passes <= 0:
            state.last_evaluated_completions = state.completed_total
            state.last_evaluated_reference_completions = ordinary_reference_total
            state.evaluation_count += 1
            self._reset_cohort_to_baseline_unlocked(
                state,
                reason="zero_pass_reset",
                zero_pass=True,
            )
            return True

        state.prior_support_count = int(
            self._service_prior_support_count.get(cohort_key, 0)
        )
        state.prior_child_count = len(
            self._service_prior_child_keys.get(cohort_key, set())
        )
        current_child_count = len(state.current_pass_child_keys)
        combined_child_count = len(
            set(state.current_pass_child_keys)
            | set(self._service_prior_child_keys.get(cohort_key, set()))
        )
        eligibility = self._service_eligibility_unlocked(state)

        if (
            self._legacy_structural_routing_for_test
            and not state.leaf_eligible
            and eligibility
            in {
                Tier3ServiceCohortEligibility.OBSERVE,
                Tier3ServiceCohortEligibility.DEMOTED,
                Tier3ServiceCohortEligibility.PARENT_DISCOVERY,
            }
        ):
            minimum_completed, minimum_passes = (
                (16, 4) if int(state.hierarchy_level) == 0 else (8, 3)
            )
            if (
                len(cohort_completed) >= minimum_completed
                and cohort_passes >= minimum_passes
                and len(state.current_pass_child_keys) >= 2
                and len(state.current_pass_root_families) >= 2
            ):
                if eligibility == Tier3ServiceCohortEligibility.PARENT_DISCOVERY:
                    self._service_discovery_exit_count += 1
                self._set_service_eligibility_unlocked(
                    state,
                    Tier3ServiceCohortEligibility.PARENT_PROBATION,
                    reason="parent_multistrata_holdout_probation",
                )
                state.last_evaluated_completions = state.completed_total
                state.last_evaluated_reference_completions = (
                    ordinary_reference_total
                )
                state.evaluation_count += 1
                self._service_probation_entry_count += 1
                return True

        if (
            self._legacy_structural_routing_for_test
            and not state.leaf_eligible
            and eligibility
            in {
                Tier3ServiceCohortEligibility.OBSERVE,
                Tier3ServiceCohortEligibility.DEMOTED,
            }
        ):
            live_child_count = len(
                self._cohort_queued_direct_children.get(cohort_key, {})
            )
            inventory_backed = live_child_count >= 2
            eligible_hierarchy = state.hierarchy_level in {0, 1, 2}
            if state.hierarchy_level == 0:
                eligible_hierarchy = eligible_hierarchy and (
                    combined_child_count >= 2 or inventory_backed
                )
            if (
                not state.discovery_exhausted
                and state.pass_total >= 1
                and eligible_hierarchy
                and (
                    current_child_count >= 2
                    or state.prior_support_count >= 2
                    or inventory_backed
                )
            ):
                self._set_service_eligibility_unlocked(
                    state,
                    Tier3ServiceCohortEligibility.PARENT_DISCOVERY,
                    reason="ancestor_discovery_open",
                )
                state.discovery_selection_total = 0
                state.last_evaluated_completions = state.completed_total
                state.last_evaluated_reference_completions = (
                    ordinary_reference_total
                )
                state.evaluation_count += 1
                self._service_discovery_entry_count += 1
                return True
            state.lifecycle_state = "observe"
            state.last_decision_reason = "ancestor_discovery_evidence_pending"
            return True

        if (
            self._legacy_structural_routing_for_test
            and eligibility == Tier3ServiceCohortEligibility.PARENT_DISCOVERY
        ):
            if state.discovery_selection_total >= 8:
                state.discovery_exhausted = True
                self._set_service_eligibility_unlocked(
                    state,
                    Tier3ServiceCohortEligibility.OBSERVE,
                    reason="parent_discovery_budget_exhausted",
                )
                self._service_discovery_exit_count += 1
                return True
            state.last_decision_reason = "parent_discovery_training_only"
            return True

        if (
            not self._legacy_structural_routing_for_test
            and not state.leaf_eligible
            and eligibility in {
            Tier3ServiceCohortEligibility.OBSERVE,
            Tier3ServiceCohortEligibility.DEMOTED,
            }
        ):
            if self._l2_parent_discovery_ready_unlocked(cohort_key, state):
                self._set_service_eligibility_unlocked(
                    state,
                    Tier3ServiceCohortEligibility.PARENT_DISCOVERY,
                    reason="l2_direct_child_discovery_open",
                )
                state.last_evaluated_completions = state.completed_total
                state.last_evaluated_reference_completions = (
                    ordinary_reference_total
                )
                state.evaluation_count += 1
                self._service_discovery_entry_count += 1
                return True
            state.lifecycle_state = "observe"
            state.last_decision_reason = "l2_direct_child_discovery_pending"
            return True

        if (
            not self._legacy_structural_routing_for_test
            and eligibility == Tier3ServiceCohortEligibility.PARENT_DISCOVERY
        ):
            # Parent evidence may direct child coverage, but never authorizes
            # arbitrary siblings for the production reward lane.
            if state.discovery_selection_total >= 8:
                state.discovery_exhausted = True
                self._set_service_eligibility_unlocked(
                    state,
                    Tier3ServiceCohortEligibility.OBSERVE,
                    reason="parent_discovery_budget_exhausted",
                )
                self._service_discovery_exit_count += 1
                return True
            if (
                state.discovery_outstanding_total <= 0
                and not self._l2_parent_discovery_ready_unlocked(
                    cohort_key,
                    state,
                )
            ):
                self._set_service_eligibility_unlocked(
                    state,
                    Tier3ServiceCohortEligibility.OBSERVE,
                    reason="l2_direct_child_discovery_inventory_exhausted",
                )
                self._service_discovery_exit_count += 1
                return True
            state.last_decision_reason = "parent_discovery_training_only"
            return True

        if not self._legacy_structural_routing_for_test and eligibility in {
            Tier3ServiceCohortEligibility.PARENT_PROBATION,
            Tier3ServiceCohortEligibility.PARENT_PROVEN,
        }:
            self._reset_cohort_to_baseline_unlocked(
                state,
                reason="parent_production_disabled",
            )
            return True

        if (
            state.leaf_eligible
            and eligibility
            in {
                Tier3ServiceCohortEligibility.OBSERVE,
                Tier3ServiceCohortEligibility.DEMOTED,
            }
            and state.pass_total > state.confirmation_evidence_pass_total
            and len(cohort_completed) == 1
            and cohort_passes == 1
            and int(self._cohort_queued_inventory.get(cohort_key, 0)) > 0
        ):
            self._set_service_eligibility_unlocked(
                state,
                Tier3ServiceCohortEligibility.LEAF_CONFIRMATION,
                reason="sparse_1_of_1_confirmation",
            )
            state.last_evaluated_completions = state.completed_total
            state.last_evaluated_reference_completions = ordinary_reference_total
            state.evaluation_count += 1
            return True

        if (
            state.leaf_eligible
            and eligibility
            in {
                Tier3ServiceCohortEligibility.OBSERVE,
                Tier3ServiceCohortEligibility.DEMOTED,
                Tier3ServiceCohortEligibility.LEAF_CONFIRMATION,
            }
            and state.pass_total > state.probation_started_pass_total
            and len(cohort_completed) >= 2
            and cohort_passes == len(cohort_completed)
        ):
            self._set_service_eligibility_unlocked(
                state,
                Tier3ServiceCohortEligibility.LEAF_PROBATION,
                reason="sparse_2_of_2_probation",
            )
            state.probation_started_completion_total = state.completed_total
            state.probation_started_pass_total = state.pass_total
            state.last_evaluated_completions = state.completed_total
            state.last_evaluated_reference_completions = ordinary_reference_total
            state.evaluation_count += 1
            self._service_probation_entry_count += 1
            return True

        if eligibility == Tier3ServiceCohortEligibility.LEAF_CONFIRMATION:
            if len(cohort_completed) >= 2 and cohort_passes < len(cohort_completed):
                self._reset_cohort_to_baseline_unlocked(
                    state,
                    reason="leaf_confirmation_completed_nonpass",
                )
                return True
            state.last_decision_reason = "leaf_confirmation_pending"
            return True

        if eligibility == Tier3ServiceCohortEligibility.LEAF_PROBATION:
            # Training and production verification are intentionally separate.
            state.last_decision_reason = "leaf_probation_holdout_pending"
            return True

        if eligibility == Tier3ServiceCohortEligibility.PARENT_PROBATION:
            state.last_decision_reason = "parent_probation_holdout_pending"
            return True

        if eligibility in {
            Tier3ServiceCohortEligibility.PARENT_PROVEN,
            Tier3ServiceCohortEligibility.PROVEN_50,
            Tier3ServiceCohortEligibility.PROVEN_75,
        }:
            # Ordinary/coverage samples train the leaf and authorize initial
            # probation only. Once production starts, promotion and demotion
            # are owned by the separate holdout window below.
            state.last_decision_reason = "production_holdout_active"
            return True

        state.last_decision_reason = "service_not_production_eligible"
        return True

    def _evaluate_dirty_service_cohorts_unlocked(self) -> None:
        if not self._dirty_cohort_keys:
            return
        self._cohort_evaluation_trigger_count += 1
        for cohort_key in tuple(sorted(self._dirty_cohort_keys)):
            state = self._cohort_yield_states.get(cohort_key)
            if state is None or state.scope != "service":
                self._dirty_cohort_keys.discard(cohort_key)
                continue
            if self._evaluate_service_cohort_unlocked(cohort_key, state):
                self._dirty_cohort_keys.discard(cohort_key)

    def _reset_similar_cohort_unlocked(
        self,
        state: _CohortYieldState,
        *,
        reason: str,
        zero_pass: bool = False,
    ) -> None:
        if state.promoted or state.probation:
            state.demotion_count += 1
        if zero_pass:
            state.zero_pass_demotion_count += 1
        state.promoted = False
        state.probation = False
        state.lifecycle_state = "similar_demoted"
        state.exploit_share = self.exploit_scan_share
        state.winning_windows = 0
        state.losing_windows = 0
        state.advantage_confirmed = False
        state.production_consecutive_nonpass = 0
        state.last_decision_reason = reason

    def _record_similar_production_holdout_unlocked(
        self,
        state: _CohortYieldState,
        *,
        passed: bool,
        censored: bool,
        busy_worker_ms: int,
    ) -> None:
        now = time.monotonic()
        worker_ms = max(1, int(busy_worker_ms))
        state.production_worker_ms += worker_ms
        if state.lifecycle_state == "similar_probation":
            state.probation_worker_ms += worker_ms
        else:
            state.proven_worker_ms += worker_ms
        state.production_samples.append(
            (now, bool(passed), bool(censored), worker_ms)
        )
        if censored:
            return
        state.production_completion_total += 1
        if state.lifecycle_state == "similar_probation":
            state.probation_completion_total += 1
        else:
            state.proven_completion_total += 1
        if passed:
            state.production_pass_total += 1
            if state.lifecycle_state == "similar_probation":
                state.probation_pass_total += 1
            else:
                state.proven_pass_total += 1
            state.production_consecutive_nonpass = 0
        else:
            state.production_consecutive_nonpass += 1
        if state.lifecycle_state == "similar_probation":
            if state.production_net_new_total >= 2:
                state.lifecycle_state = "similar_proven"
                state.probation = False
                state.promoted = True
                state.advantage_confirmed = True
                state.promotion_count += 1
                state.production_consecutive_nonpass = 0
                state.last_decision_reason = "similar_holdout_confirmed"
                return
            if (
                state.production_consecutive_nonpass >= 4
                or (
                    state.production_resolved_total >= 8
                    and state.production_net_new_total < 2
                )
            ):
                self._reset_similar_cohort_unlocked(
                    state,
                    reason="similar_probation_failed",
                )
            return
        if (
            state.lifecycle_state == "similar_proven"
            and state.production_consecutive_nonpass >= 8
        ):
            self._reset_similar_cohort_unlocked(
                state,
                reason="similar_proven_eight_zero_pass",
            )

    def _evaluate_nonservice_cohort_unlocked(
        self,
        state: _CohortYieldState,
    ) -> None:
        ordinary_state = self._cohort_yield_states.get("ordinary")
        if ordinary_state is None:
            return
        completed, passes = self._cohort_window_counts(state)
        ordinary_completed, ordinary_passes = self._cohort_window_counts(
            ordinary_state
        )
        if (
            state.scope == "similar"
            and not self._legacy_structural_routing_for_test
            and not state.promoted
            and len(completed) >= 2
            and passes >= 2
        ):
            # A precise anonymous network cohort is narrower than the L3
            # service profile and can expose a productive cold-start cluster.
            # Admit only a bounded holdout: dispatch quota limits this to two
            # flights per cohort and four globally until two net-new commits
            # confirm it. Source identity is deliberately absent from the key.
            state.promoted = True
            state.probation = True
            state.lifecycle_state = "similar_probation"
            state.exploit_share = self.exploit_scan_share
            state.promotion_count += 1
            state.advantage_confirmed = False
            state.production_resolved_total = 0
            state.production_net_new_total = 0
            state.production_completion_total = 0
            state.production_pass_total = 0
            state.production_consecutive_nonpass = 0
            state.production_samples.clear()
            state.last_decision_reason = "similar_sparse_2_of_2_probation"
            state.last_evaluated_completions = state.completed_total
            state.last_evaluated_reference_completions = (
                ordinary_state.completed_total
            )
            state.evaluation_count += 1
            return
        if (
            len(completed) < 8
            or len(ordinary_completed) < 2
            or state.completed_total <= state.last_evaluated_completions
        ):
            return
        state.last_evaluated_completions = state.completed_total
        state.last_evaluated_reference_completions = ordinary_state.completed_total
        state.evaluation_count += 1
        if len(completed) >= 32 and passes <= 0:
            if state.scope == "similar":
                self._reset_similar_cohort_unlocked(
                    state,
                    reason="similar_zero_pass_reset",
                    zero_pass=True,
                )
            else:
                self._reset_cohort_to_baseline_unlocked(
                    state,
                    reason="zero_pass_reset",
                    zero_pass=True,
                )
            return
        cohort_seconds = max(
            0.001,
            sum(item[3] for item in state.samples) / 1000.0 / len(state.samples),
        )
        ordinary_seconds = max(
            0.001,
            sum(item[3] for item in ordinary_state.samples)
            / 1000.0
            / len(ordinary_state.samples),
        )
        cohort_lcb = self._wilson_bound(
            passes,
            len(completed),
            lower=True,
        ) / cohort_seconds
        ordinary_ucb = self._wilson_bound(
            ordinary_passes,
            len(ordinary_completed),
            lower=False,
        ) / ordinary_seconds
        if state.scope == "similar":
            ordinary_mean = (
                float(ordinary_passes) / float(len(ordinary_completed))
                if ordinary_completed
                else 0.0
            ) / ordinary_seconds
            qualified = bool(
                len(completed) >= 8
                and passes >= 3
                and len(state.current_pass_root_families) >= 2
                and cohort_lcb > ordinary_mean
            )
            if qualified:
                state.winning_windows += 1
                state.losing_windows = 0
                if state.winning_windows >= 2 and not state.promoted:
                    state.promoted = True
                    state.probation = True
                    state.lifecycle_state = "similar_probation"
                    state.exploit_share = self.exploit_scan_share
                    state.promotion_count += 1
                    state.advantage_confirmed = True
                    state.production_resolved_total = 0
                    state.production_net_new_total = 0
                    state.production_completion_total = 0
                    state.production_pass_total = 0
                    state.production_consecutive_nonpass = 0
                    state.production_samples.clear()
                    state.last_decision_reason = (
                        "similar_multistrata_training_confirmed"
                    )
                    state.winning_windows = 0
                return
            state.winning_windows = 0
            state.losing_windows += 1
            if state.promoted and state.losing_windows >= 2:
                self._reset_similar_cohort_unlocked(
                    state,
                    reason="similar_training_advantage_lost",
                )
            return
        if passes >= 2 and cohort_lcb > ordinary_ucb:
            state.winning_windows += 1
            state.losing_windows = 0
            if state.winning_windows >= 2:
                was_promoted = state.promoted
                state.promoted = True
                state.advantage_confirmed = True
                state.exploit_share = min(0.75, state.exploit_share + 0.125)
                state.winning_windows = 0
                state.last_decision_reason = "cohort_advantage_confirmed"
                if not was_promoted:
                    state.promotion_count += 1
                self._refresh_adaptive_exploit_share_unlocked()
            return
        state.losing_windows += 1
        state.winning_windows = 0
        if state.losing_windows >= 2:
            self._reset_cohort_to_baseline_unlocked(
                state,
                reason="cohort_disadvantage_confirmed",
            )

    def _record_fast_yield_sample_unlocked(
        self,
        *,
        passed: bool,
        censored: bool,
        busy_worker_ms: int,
        scope: str,
        cohort_key: str = "",
        hierarchy_level: int = -1,
        parent_key: str = "",
        leaf_eligible: bool = False,
        pass_child_key: str = "",
        pass_root_family: str = "",
        defer_evaluation: bool = False,
    ) -> None:
        normalized_scope = (
            scope if scope in {"exact", "service", "similar"} else "ordinary"
        )
        normalized_key = (
            "ordinary"
            if normalized_scope == "ordinary"
            else str(cohort_key or normalized_scope)
        )
        state = self._cohort_yield_states.get(normalized_key)
        if state is None:
            self._next_cohort_evidence_sequence += 1
            state = _CohortYieldState(
                scope=normalized_scope,
                first_evidence_sequence=int(
                    self._next_cohort_evidence_sequence
                ),
                exploit_share=self.exploit_scan_share,
                hierarchy_level=int(hierarchy_level),
                parent_key=str(parent_key or ""),
                leaf_eligible=bool(leaf_eligible),
            )
            self._cohort_yield_states[normalized_key] = state
            self._register_cohort_state_unlocked(normalized_key, state)
        elif normalized_scope == "service":
            if hierarchy_level >= 0:
                state.hierarchy_level = int(hierarchy_level)
            if parent_key:
                state.parent_key = str(parent_key)
            state.leaf_eligible = bool(state.leaf_eligible or leaf_eligible)
        if normalized_scope == "service":
            state.prior_support_count = int(
                self._service_prior_support_count.get(normalized_key, 0)
            )
            state.prior_child_count = len(
                self._service_prior_child_keys.get(normalized_key, set())
            )
        now = time.monotonic()
        state.samples.append(
            (now, bool(passed), bool(censored), max(1, int(busy_worker_ms)))
        )
        state.sample_total += 1
        if not censored:
            state.completed_total += 1
            if passed:
                state.pass_total += 1
                if pass_child_key:
                    state.current_pass_child_keys.add(str(pass_child_key))
                if pass_root_family:
                    state.current_pass_root_families.add(
                        str(pass_root_family)
                    )
        self._trim_cohort_samples_unlocked(state, now)

        if normalized_scope == "ordinary":
            if not defer_evaluation:
                if (
                    not censored
                    and state.completed_total > 0
                    and state.completed_total % 8 == 0
                ):
                    self._dirty_cohort_keys.update(
                        key
                        for key, candidate_state in self._cohort_yield_states.items()
                        if candidate_state.scope == "service"
                    )
                self._evaluate_dirty_service_cohorts_unlocked()
            return
        if normalized_scope == "service":
            self._dirty_cohort_keys.add(normalized_key)
            if not defer_evaluation:
                self._evaluate_dirty_service_cohorts_unlocked()
            return
        if not defer_evaluation:
            self._evaluate_nonservice_cohort_unlocked(state)

    def _record_service_production_holdout_unlocked(
        self,
        ticket: Tier3CandidateFeedbackTicket,
        *,
        passed: bool,
        censored: bool,
        busy_worker_ms: int,
    ) -> None:
        cohort_key = str(ticket.reward_cohort_key or "")
        state = self._cohort_yield_states.get(cohort_key)
        if state is None or state.scope != "service":
            return
        now = time.monotonic()
        worker_ms = max(1, int(busy_worker_ms))
        state.production_worker_ms += worker_ms
        ticket_eligibility = str(ticket.reward_cohort_state or "")
        if ticket_eligibility in {"leaf_probation", "parent_probation"}:
            state.probation_worker_ms += worker_ms
        elif ticket_eligibility in {
            "parent_proven",
            "proven_50",
            "proven_75",
        }:
            state.proven_worker_ms += worker_ms
        state.production_samples.append(
            (now, bool(passed), bool(censored), worker_ms)
        )
        while (
            state.production_samples
            and now - state.production_samples[0][0] > 60.0
        ):
            state.production_samples.popleft()
        if censored:
            return
        state.production_completion_total += 1
        if passed:
            state.production_pass_total += 1
            state.production_consecutive_nonpass = 0
        else:
            state.production_consecutive_nonpass += 1
        if ticket_eligibility in {"leaf_probation", "parent_probation"}:
            state.probation_completion_total += 1
            if passed:
                state.probation_pass_total += 1
        elif ticket_eligibility in {
            "parent_proven",
            "proven_50",
            "proven_75",
        }:
            state.proven_completion_total += 1
            if passed:
                state.proven_pass_total += 1

        completion_delta = max(
            0,
            state.production_completion_total
            - state.production_state_started_completion_total,
        )
        pass_delta = max(
            0,
            state.production_pass_total - state.production_state_started_pass_total,
        )
        eligibility = self._service_eligibility_unlocked(state)
        if ticket_eligibility in {"leaf_probation", "parent_probation"}:
            expected_probation = (
                Tier3ServiceCohortEligibility.PARENT_PROBATION
                if ticket_eligibility == "parent_probation"
                else Tier3ServiceCohortEligibility.LEAF_PROBATION
            )
            if eligibility != expected_probation:
                return
            required_resolved = 2
            promotion_ready = bool(
                completion_delta >= required_resolved
                and pass_delta >= 2
            )
            if promotion_ready:
                self._set_service_eligibility_unlocked(
                    state,
                    (
                        Tier3ServiceCohortEligibility.PARENT_PROVEN
                        if self._legacy_structural_routing_for_test
                        and ticket_eligibility == "parent_probation"
                        else Tier3ServiceCohortEligibility.PROVEN_50
                    ),
                    reason=(
                        "probation_holdout_2_of_2"
                        if self._legacy_structural_routing_for_test
                        else "probation_holdout_2_net_new_guarded_proven"
                    ),
                )
                state.promotion_count += 1
                state.advantage_confirmed = True
                self._service_probation_exit_count += 1
                self._refresh_adaptive_exploit_share_unlocked()
            elif state.production_consecutive_nonpass >= 4:
                self._reset_cohort_to_baseline_unlocked(
                    state,
                    reason="probation_four_consecutive_nonpass",
                )
            elif completion_delta >= 8 and pass_delta < 2:
                self._reset_cohort_to_baseline_unlocked(
                    state,
                    reason="probation_eight_without_two_net_new",
                )
            return
        if eligibility == Tier3ServiceCohortEligibility.PARENT_PROVEN:
            if state.production_consecutive_nonpass >= 8:
                self._reset_cohort_to_baseline_unlocked(
                    state,
                    reason="parent_proven_eight_zero_pass",
                )
            else:
                state.last_decision_reason = "parent_proven_holdout_active"
            return
        if eligibility in {
            Tier3ServiceCohortEligibility.PROVEN_50,
            Tier3ServiceCohortEligibility.PROVEN_75,
        }:
            if (
                eligibility == Tier3ServiceCohortEligibility.PROVEN_50
                and not self._legacy_structural_routing_for_test
            ):
                confirmation_completions = max(
                    0,
                    state.proven_completion_total
                    - state.production_state_started_completion_total,
                )
                confirmation_passes = max(
                    0,
                    state.proven_pass_total
                    - state.production_state_started_pass_total,
                )
                if confirmation_completions < 2:
                    state.last_decision_reason = (
                        "proven_50_bounded_confirmation_pending"
                    )
                    return
                if confirmation_passes < 1:
                    self._reset_cohort_to_baseline_unlocked(
                        state,
                        reason="proven_50_confirmation_failed",
                    )
                    return
            if state.production_consecutive_nonpass >= 8:
                self._reset_cohort_to_baseline_unlocked(
                    state,
                    reason="proven_eight_zero_pass",
                )
                return
            completed = [
                sample for sample in state.production_samples if not sample[2]
            ]
            passes = sum(1 for sample in completed if sample[1])
            if len(completed) < 16 or passes < 5:
                state.last_decision_reason = "proven_75_holdout_pending"
                return
            (
                ordinary_samples,
                ordinary_completed,
                ordinary_passes,
                ordinary_reference_total,
            ) = self._ordinary_reference_for_service_cohort_unlocked(
                cohort_key,
                now,
            )
            if len(ordinary_completed) < 2:
                state.last_decision_reason = "proven_75_reference_pending"
                return
            production_lcb = self._wilson_bound(
                passes,
                len(completed),
                lower=True,
            )
            ordinary_ucb = self._wilson_bound(
                ordinary_passes,
                len(ordinary_completed),
                lower=False,
            )
            state.last_evaluated_reference_completions = (
                ordinary_reference_total
            )
            state.evaluation_count += 1
            if production_lcb > ordinary_ucb:
                state.winning_windows += 1
                state.losing_windows = 0
                state.last_decision_reason = "proven_75_holdout_advantage"
                if state.winning_windows >= 2:
                    if eligibility == Tier3ServiceCohortEligibility.PROVEN_50:
                        self._set_service_eligibility_unlocked(
                            state,
                            Tier3ServiceCohortEligibility.PROVEN_75,
                            reason="proven_75_holdout_confirmed",
                        )
                        state.promotion_count += 1
                        state.advantage_confirmed = True
                        self._refresh_adaptive_exploit_share_unlocked()
                    state.winning_windows = 0
                return
            state.winning_windows = 0
            state.losing_windows += 1
            state.last_decision_reason = "proven_75_holdout_not_advantaged"
            if state.losing_windows >= 2:
                self._set_service_eligibility_unlocked(
                    state,
                    (
                        Tier3ServiceCohortEligibility.PROVEN_50
                        if eligibility == Tier3ServiceCohortEligibility.PROVEN_75
                        else Tier3ServiceCohortEligibility.LEAF_PROBATION
                    ),
                    reason="production_holdout_disadvantage_confirmed",
                )
                state.losing_windows = 0
                state.demotion_count += 1
                self._refresh_adaptive_exploit_share_unlocked()

    def _record_fast_visibility_cost_unlocked(
        self, ticket: Tier3CandidateFeedbackTicket, *, passed: bool,
        censored: bool, busy_worker_ms: int,
    ) -> None:
        # Called only after the FAST ticket deduplication gate. These costs
        # are diagnostics, never a prior, censor penalty or lane budget.
        row = self._fast_visibility_cost[ticket.visibility_class]
        worker_ms = max(0, int(busy_worker_ms))
        row["samples"] += 1
        row["completed"] += int(not censored)
        row["censored"] += int(censored)
        row["passed"] += int(passed and not censored)
        row["worker_ms"] += worker_ms
        row["completed_worker_ms"] += worker_ms if not censored else 0
        row["censored_worker_ms"] += worker_ms if censored else 0

    def _record_ticket_fast_yield_samples_unlocked(
        self,
        ticket: Tier3CandidateFeedbackTicket,
        *,
        passed: bool,
        censored: bool,
        busy_worker_ms: int,
    ) -> None:
        """Feed every real FAST result to independent structural cohorts."""

        ticket_id = int(ticket.ticket_id)
        if ticket_id in self._fast_observed_ticket_ids:
            return
        self._fast_observed_ticket_ids.add(ticket_id)
        self._record_fast_visibility_cost_unlocked(
            ticket, passed=passed, censored=censored, busy_worker_ms=busy_worker_ms,
        )
        if ticket.visibility_class == Tier3CandidateVisibilityIndex.TCP_ONLY:
            self._tcp_hint_fast_window.append(
                (bool(passed and not censored), max(0, int(busy_worker_ms)))
            )
            if (
                not self._tcp_hint_yield_limited
                and len(self._tcp_hint_fast_window) >= 16
                and not any(sample[0] for sample in self._tcp_hint_fast_window)
                and sum(sample[1] for sample in self._tcp_hint_fast_window) >= 24_000
            ):
                # Shadow-only exposure marker; it cannot demote unrelated
                # leaves or change the production coverage budget.
                self._tcp_hint_yield_limited = True
                self._tcp_hint_limit_count += 1
        state = self._tickets.get(ticket_id)
        if state is not None:
            state.fast_pass_observed = bool(passed and not censored)
            signal = self._replay_leaf_discoveries.get(state.replay_discovery_key)
            if signal is not None:
                signal.passed += int(passed and not censored)
        self._fast_observation_sample_count += 1
        self._fast_observation_exposure_ms += max(1, int(busy_worker_ms))
        if not censored:
            self._fast_observation_completion_count += 1
            if passed:
                self._fast_observation_pass_count += 1

        replay_evidence = ticket.evidence_origin in {
            Tier3CandidateEvidenceOrigin.FRESH_REPLAY.value,
            Tier3CandidateEvidenceOrigin.HISTORICAL_REPROBE.value,
        }
        if replay_evidence:
            self._replay_fast_observation_sample_count += 1
            if not censored:
                self._replay_fast_observation_completion_count += 1
                if passed:
                    self._replay_fast_observation_pass_count += 1
        else:
            self._cold_fast_observation_sample_count += 1
            if not censored:
                self._cold_fast_observation_completion_count += 1
                if passed:
                    self._cold_fast_observation_pass_count += 1

        reward_selection = bool(
            ticket.selection_reason == "reward"
            and ticket.reward_scope in {"service", "similar"}
            and ticket.reward_cohort_key
        )
        reward_decision_pending_commit = False
        if reward_selection:
            decision = self._cohort_decisions_by_ticket.get(
                int(ticket.ticket_id)
            )
            if decision is not None:
                decision["outcome"] = (
                    "censored"
                    if censored
                    else "pass_pending_commit"
                    if passed
                    else "completed_nonpass"
                )
                decision["passed"] = bool(passed)
                if not passed:
                    self._cohort_decisions_by_ticket.pop(
                        int(ticket.ticket_id),
                        None,
                    )
            reward_decision_pending_commit = bool(passed)

        if replay_evidence:
            # A warm-cache survivor is valuable replay evidence, but it is
            # selected from a conditioned population.  Feeding it into the
            # cold L0-L3 learner promotes unrelated siblings and makes a warm
            # success degrade the next cold run.  Replay yield remains owned
            # by the typed visibility lane above.
            if reward_decision_pending_commit:
                return
            decision = self._cohort_decisions_by_ticket.pop(
                int(ticket.ticket_id),
                None,
            )
            if decision is not None:
                decision["outcome"] = (
                    "censored"
                    if censored
                    else "pass"
                    if passed
                    else "completed_nonpass"
                )
                decision["passed"] = bool(passed)
            return

        samples: list[tuple[str, str]] = []
        ordinary_sample = ticket.selection_reason not in {"reward", "replay_leaf_discovery"}
        if ordinary_sample:
            samples.append(("ordinary", "ordinary"))
        exact_key = ticket.exact_cohort_key or f"exact:{ticket.arm_key}"
        samples.append(("exact", exact_key))
        service_keys = tuple(ticket.service_cohort_keys)
        if not service_keys:
            service_key = str(ticket.service_cohort_key or "")
            if not service_key and Tier3CandidateFeatureKey._meaningful(
                ticket.feature_key.service_profile
            ):
                service_key = f"service:{ticket.feature_key.service_profile}"
            service_keys = (service_key,) if service_key else ()
        if ordinary_sample:
            now = time.monotonic()
            self._ordinary_ticket_samples.append(
                (
                    now,
                    bool(passed),
                    bool(censored),
                    max(1, int(busy_worker_ms)),
                    frozenset(service_keys),
                )
            )
            if not censored:
                self._ordinary_ticket_completion_total += 1
            while (
                self._ordinary_ticket_samples
                and now - self._ordinary_ticket_samples[0][0] > 60.0
            ):
                self._ordinary_ticket_samples.popleft()
        similar_key = str(ticket.similar_cohort_key or "")
        selected_similar_holdout = bool(
            reward_selection
            and ticket.reward_scope == "similar"
            and ticket.reward_cohort_key == similar_key
        )
        if similar_key and not selected_similar_holdout:
            samples.append(("similar", similar_key))
        seen: set[str] = set()
        for scope, cohort_key in samples:
            if cohort_key in seen:
                continue
            seen.add(cohort_key)
            self._record_fast_yield_sample_unlocked(
                passed=passed,
                censored=censored,
                busy_worker_ms=busy_worker_ms,
                scope=scope,
                cohort_key=cohort_key,
                pass_root_family=(
                    f"{ticket.source_root}\x1f{ticket.family}"
                    if scope == "similar"
                    else ""
                ),
                defer_evaluation=scope == "ordinary",
            )
        training_service_keys = (
            service_keys if ticket.selection_reason != "reward" else ()
        )
        for hierarchy_level, service_key in enumerate(training_service_keys):
            if not service_key or service_key in seen:
                continue
            seen.add(service_key)
            self._record_fast_yield_sample_unlocked(
                passed=passed,
                censored=censored,
                busy_worker_ms=busy_worker_ms,
                scope="service",
                cohort_key=service_key,
                hierarchy_level=hierarchy_level,
                parent_key=(
                    training_service_keys[hierarchy_level - 1]
                    if hierarchy_level > 0
                    else ""
                ),
                leaf_eligible=hierarchy_level == len(training_service_keys) - 1,
                pass_child_key=(
                    training_service_keys[hierarchy_level + 1]
                    if hierarchy_level + 1 < len(training_service_keys)
                    else exact_key
                ),
                pass_root_family=(
                    f"{ticket.source_root}\x1f{ticket.family}"
                ),
                defer_evaluation=True,
            )
        if passed or (
            not censored
            and self._fast_observation_completion_count % 8 == 0
        ):
            self._evaluate_dirty_service_cohorts_unlocked()
        if ticket.selection_reason == "replay_leaf_discovery":
            decision = self._cohort_decisions_by_ticket.get(ticket_id)
            if decision is not None:
                decision["passed"] = bool(passed)
                decision["outcome"] = "pass_pending_commit" if passed else "validation_pending"
            return
        if reward_decision_pending_commit:
            return
        decision = self._cohort_decisions_by_ticket.pop(
            int(ticket.ticket_id),
            None,
        )
        if decision is not None:
            decision["outcome"] = (
                "censored"
                if censored
                else "pass"
                if passed
                else "completed_nonpass"
            )
            decision["passed"] = bool(passed)

    def _refresh_adaptive_exploit_share_unlocked(self) -> None:
        self._adaptive_exploit_scan_share = max(
            [self.exploit_scan_share]
            + [
                state.exploit_share
                for state in self._cohort_yield_states.values()
                if (
                    state.scope == "exact"
                    and state.promoted
                )
                or (
                state.scope == "service"
                and self._service_eligibility_unlocked(state)
                in {
                    Tier3ServiceCohortEligibility.PARENT_PROVEN,
                    Tier3ServiceCohortEligibility.PROVEN_50,
                    Tier3ServiceCohortEligibility.PROVEN_75,
                    }
                )
            ]
        )

    def on_terminal(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
        *,
        passed: bool = False,
        outcome: str = "",
        busy_worker_ms: int = 0,
        stage: str = "",
    ) -> None:
        """Release all pre-TCP feedback credit for a terminal candidate."""

        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None or state.phase.startswith("terminal"):
                return
            if not state.tcp_handed_off and not passed:
                # A pre-start rejection/cancel is not a transport observation.
                # Release the reservation without manufacturing a handoff.
                self._release_ticket_credit(
                    state,
                    terminal=True,
                    decision_outcome="reservation_cancelled",
                )
                return
            committed = self._commit_handoff_unlocked(state)
            if committed:
                self._advance_committed_handoff_epoch_unlocked([state])
            arm = self._arms.get(state.arm_id)
            outcome_name = str(outcome or "").strip().lower()
            observed = not any(
                marker in outcome_name
                for marker in (
                    "facility",
                    "cancel",
                    "closed",
                    "unavailable",
                    "duplicate",
                    "rejected",
                )
            )
            if arm is not None:
                if observed or passed:
                    arm.validation_observed = True
                arm.validation_pending = False
            scope = self._ticket_reward_metric_scope(state.ticket)
            worker_ms = max(0, int(busy_worker_ms))
            if scope == "exact":
                self._reward_exact_worker_ms += worker_ms
                if passed:
                    self._reward_exact_fast_pass_count += 1
            elif scope == "service":
                self._reward_service_worker_ms += worker_ms
                if passed:
                    self._reward_service_fast_pass_count += 1
            elif scope == "similar":
                self._reward_similar_worker_ms += worker_ms
                if passed:
                    self._reward_similar_fast_pass_count += 1
            elif scope == "shadow_broad":
                self._reward_shadow_broad_worker_ms += worker_ms
                if passed:
                    self._reward_shadow_broad_fast_pass_count += 1
            normalized_stage = str(stage or "").strip().lower()
            if passed and normalized_stage in {"fast", "fast_probe"}:
                self._visibility_fast_pass_total[
                    state.ticket.visibility_class
                ] += 1
            if normalized_stage in {"fast", "fast_probe"} and observed:
                self._record_ticket_fast_yield_samples_unlocked(
                    state.ticket,
                    passed=passed,
                    censored=bool(
                        "censor" in outcome_name or "timeout" in outcome_name
                    ),
                    busy_worker_ms=worker_ms,
                )
            if passed:
                state.reward_pending_worker_ms = worker_ms
                self._mark_reward_pass_pending_unlocked(state)
            else:
                if any(
                    marker in outcome_name
                    for marker in ("facility", "unavailable")
                ):
                    scheduling_outcome = Tier3SchedulingOutcome.FACILITY
                elif any(
                    marker in outcome_name
                    for marker in ("cancel", "closed")
                ):
                    scheduling_outcome = Tier3SchedulingOutcome.CANCELLED
                elif any(
                    marker in outcome_name
                    for marker in ("duplicate", "rejected", "skip")
                ):
                    scheduling_outcome = Tier3SchedulingOutcome.NEUTRAL_SKIP
                elif any(
                    marker in outcome_name
                    for marker in ("censor", "timeout", "transient")
                ):
                    scheduling_outcome = (
                        Tier3SchedulingOutcome.VALIDATION_CENSORED
                    )
                else:
                    scheduling_outcome = Tier3SchedulingOutcome.VALIDATION_FAIL
                if state.reward_flight_open:
                    self._resolve_reward_flight_unlocked(
                        state,
                        scheduling_outcome,
                        busy_worker_ms=worker_ms,
                    )
                self._record_visibility_resolution_unlocked(
                    state,
                    scheduling_outcome,
                )
                self._record_ordinary_e2e_outcome_unlocked(
                    state,
                    scheduling_outcome,
                    busy_worker_ms=worker_ms,
                )
            self._release_ticket_credit(
                state,
                terminal=True,
                retain_for_commit=bool(passed),
                decision_outcome=(
                    outcome_name
                    or ("pass" if passed else "terminal")
                ),
            )
            self._terminal_feedback_count += 1

    def on_net_new_commit(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
    ) -> None:
        """Grant a short, bounded future-candidate burst for a durable pool gain."""

        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None:
                return
            # A normal PASS terminal has already released its FAST/DEEP
            # credit and retained the ticket only until the durable commit.
            # Keep commit feedback defensive: if an observer was skipped, the
            # commit is still the final ownership boundary and must not leave
            # an arm permanently saturated.
            if not state.phase.startswith("terminal"):
                committed = self._commit_handoff_unlocked(state)
                if committed:
                    self._advance_committed_handoff_epoch_unlocked([state])
                self._release_ticket_credit(
                    state,
                    terminal=True,
                    retain_for_commit=True,
                )
            arm = self._arms.get(state.arm_id)
            if arm is not None:
                arm.validation_observed = True
                arm.validation_pending = False
                arm.net_new_commits += 1
                self._grant_reward_unlocked(
                    arm,
                    arm_key=state.ticket.arm_key,
                    feature_key=state.ticket.feature_key,
                )
            self._resolve_reward_flight_unlocked(
                state,
                Tier3SchedulingOutcome.NET_NEW,
                busy_worker_ms=state.reward_pending_worker_ms,
            )
            self._record_visibility_resolution_unlocked(
                state,
                Tier3SchedulingOutcome.NET_NEW,
            )
            self._record_ordinary_e2e_outcome_unlocked(
                state,
                Tier3SchedulingOutcome.NET_NEW,
                busy_worker_ms=state.reward_pending_worker_ms,
            )
            scope = self._ticket_reward_metric_scope(state.ticket)
            if scope == "exact":
                self._reward_exact_net_new_count += 1
            elif scope == "service":
                self._reward_service_net_new_count += 1
            elif scope == "similar":
                self._reward_similar_net_new_count += 1
            elif scope == "shadow_broad":
                self._reward_shadow_broad_net_new_count += 1
            self._net_new_feedback_count += 1
            self._open_replay_leaf_discovery_unlocked(state)
            self._evaluate_dirty_service_cohorts_unlocked()
            self._close_cohort_decision_unlocked(
                state.ticket.ticket_id,
                outcome="committed",
            )
            self._forget_ticket_unlocked(state.ticket.ticket_id)

    @staticmethod
    def _ticket_reward_metric_scope(
        ticket: Tier3CandidateFeedbackTicket,
    ) -> str:
        if ticket.reward_scope in {"exact", "service", "similar"}:
            return ticket.reward_scope
        if ticket.reward_scope == "shadow_broad":
            return "shadow_broad"
        return "none"

    def on_commit_complete(
        self,
        ticket: Tier3CandidateFeedbackTicket | Mapping[str, Any] | None,
        *,
        net_new: bool,
    ) -> None:
        if net_new:
            self.on_net_new_commit(ticket)
            return
        with self._lock:
            state = self._resolve_ticket(ticket)
            if state is None:
                return
            if not state.phase.startswith("terminal"):
                self._resolve_reward_flight_unlocked(
                    state,
                    Tier3SchedulingOutcome.NEUTRAL_SKIP,
                )
                self._record_visibility_resolution_unlocked(
                    state,
                    Tier3SchedulingOutcome.NEUTRAL_SKIP,
                )
                self._release_ticket_credit(state, terminal=True)
                return
            self._resolve_reward_flight_unlocked(
                state,
                Tier3SchedulingOutcome.NEUTRAL_SKIP,
                busy_worker_ms=state.reward_pending_worker_ms,
            )
            self._record_visibility_resolution_unlocked(
                state,
                Tier3SchedulingOutcome.NEUTRAL_SKIP,
            )
            self._close_cohort_decision_unlocked(
                state.ticket.ticket_id,
                outcome="commit_rejected_or_duplicate",
            )
            self._forget_ticket_unlocked(state.ticket.ticket_id)

    def release_outstanding(self) -> int:
        """Release every non-terminal ticket during a sealed expansion."""

        with self._lock:
            states = list(self._tickets.values())
            for state in states:
                self._resolve_replay_leaf_discovery_unlocked(
                    state, Tier3SchedulingOutcome.CANCELLED,
                )
                if state.phase.startswith("terminal"):
                    self._resolve_reward_flight_unlocked(
                        state,
                        Tier3SchedulingOutcome.CANCELLED,
                    )
                    self._close_cohort_decision_unlocked(
                        state.ticket.ticket_id,
                        outcome="sealed",
                    )
                    self._forget_ticket_unlocked(state.ticket.ticket_id)
                    continue
                self._close_cohort_decision_unlocked(
                    state.ticket.ticket_id,
                    outcome="sealed",
                )
                self._release_ticket_credit(state, terminal=True)
            if not self._tickets:
                for ticket_id in tuple(self._cohort_decisions_by_ticket):
                    self._close_cohort_decision_unlocked(
                        ticket_id,
                        outcome="sealed",
                    )
                self._reserved_confirmation_handoffs.clear()
                self._reserved_discovery_handoffs.clear()
                self._reserved_cold_leaf_handoffs.clear()
            return len(states)

    def requeue_front(self, candidates: list[ScheduledTier3Candidate]) -> None:
        if not candidates:
            return
        with self._lock:
            for candidate in reversed(candidates):
                ticket_state = self._resolve_ticket(candidate.dispatch_ticket)
                if ticket_state is not None:
                    self._release_ticket_credit(ticket_state, terminal=True)
                self._requeue_candidate_unlocked(candidate)

    def size(self) -> int:
        with self._lock:
            return self._backlog_count

    def pending_handoff_count(self) -> int:
        """Return work owned between scheduler selection and TCP outcome."""

        with self._lock:
            return max(
                0,
                int(self._reserved_handoff_count),
                int(self._tcp_handoff_lease_count),
            )

    def active_handoff_ticket_ids(self) -> frozenset[int]:
        """Return tickets that still own real TCP execution capacity."""

        with self._lock:
            return frozenset(
                int(ticket_id)
                for ticket_id in self._handoff_owning_ticket_ids_unlocked()
            )

    def reconcile_tcp_execution_leases(
        self,
        authoritative_ticket_ids: Iterable[int],
        *,
        stale_after_seconds: float = 0.5,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Bound scheduler capacity to Manager's executable TCP ledger.

        The immutable context normally closes every ticket at its direct TCP
        outcome.  This reconciliation is the protocol-error backstop: a lost
        queue item may be retried once, while a transport that already started
        is closed as censored rather than trained as a candidate failure.
        """

        authoritative = {int(ticket_id) for ticket_id in authoritative_ticket_ids}
        cutoff = time.monotonic() - max(0.05, float(stale_after_seconds))
        with self._lock:
            orphan_states = sorted(
                (
                    state
                    for state in (
                        self._tickets[ticket_id]
                        for ticket_id in self._handoff_owning_ticket_ids_unlocked()
                    )
                    if int(state.ticket.ticket_id) not in authoritative
                    and float(state.updated_at) <= cutoff
                ),
                key=lambda state: int(state.ticket.ticket_id),
            )
            reclaimed_reserved: list[int] = []
            closed_started: list[int] = []
            for state in orphan_states:
                if state.phase == "reserved":
                    candidate = state.candidate
                    retry_count = int(
                        candidate.node.extra.get(
                            "tier3_scheduler_orphan_requeue_count",
                            0,
                        )
                        or 0
                    )
                    allow_requeue = retry_count < 1
                    if allow_requeue:
                        candidate.node.extra[
                            "tier3_scheduler_orphan_requeue_count"
                        ] = retry_count + 1
                    self._release_ticket_credit(
                        state,
                        terminal=True,
                        decision_outcome="tcp_ledger_orphan_reserved",
                    )
                    if allow_requeue:
                        self._requeue_candidate_unlocked(candidate)
                    reclaimed_reserved.append(int(state.ticket.ticket_id))
                    continue
                self._record_visibility_resolution_unlocked(
                    state,
                    Tier3SchedulingOutcome.TCP_CENSORED,
                )
                self._record_tcp_window_outcome_unlocked(
                    state.ticket.ticket_id,
                    responsive=False,
                    hard_censored=True,
                )
                self._record_ordinary_e2e_outcome_unlocked(
                    state,
                    Tier3SchedulingOutcome.TCP_CENSORED,
                )
                self._resolve_reward_flight_unlocked(
                    state,
                    Tier3SchedulingOutcome.TCP_CENSORED,
                )
                self._release_ticket_credit(
                    state,
                    terminal=True,
                    decision_outcome="tcp_ledger_orphan_started",
                )
                closed_started.append(int(state.ticket.ticket_id))
            self._tcp_orphan_reserved_reclaimed_total += len(
                reclaimed_reserved
            )
            self._tcp_orphan_started_closed_total += len(closed_started)
            return tuple(reclaimed_reserved), tuple(closed_started)

    def quick_metrics(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]:
        """Diagnostics snapshot; ``max_age_seconds`` lets one tick share a scan.

        The scan walks every cohort and the whole lookahead index, so its cost
        grows with the session.  Callers on the dispatch and progress loops ran
        it several times per tick while holding this lock, which is what drove
        wave overhead from 47 ms to 1203 ms and starved the UI thread on
        2026-09-20.  A caller that only reports may accept a slightly older
        snapshot; passing 0 (the default) keeps the exact behaviour.
        """

        if max_age_seconds > 0.0:
            cached = self._quick_metrics_memo
            if (
                cached is not None
                and (time.monotonic() - cached[0]) <= max_age_seconds
            ):
                return dict(cached[1])
        with self._lock:
            metrics = self.quick_metrics_unlocked()
        if max_age_seconds > 0.0:
            self._quick_metrics_memo = (time.monotonic(), dict(metrics))
        return metrics

    def supply_snapshot(self) -> Tier3CandidateSupplySnapshot:
        with self._lock:
            return self._supply_snapshot_unlocked()

    def _tcp_responsive_probability_lcb_unlocked(self) -> float:
        samples = tuple(self._tcp_outcome_window)
        total = len(samples)
        if total < 8:
            return 0.25
        # Every real start ack consumes capacity. Pending and hard-censored
        # starts therefore remain in the forecast denominator, while only a
        # completed responsive result contributes a success.
        success = sum(
            1 for _ticket_id, outcome in samples if outcome == "responsive"
        )
        z = 1.96
        z2 = z * z
        proportion = success / float(total)
        denominator = 1.0 + z2 / total
        center = proportion + z2 / (2.0 * total)
        margin = z * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z2 / (4.0 * total * total)
        )
        return max(0.0, min(1.0, (center - margin) / denominator))

    def _supply_snapshot_unlocked(self) -> Tier3CandidateSupplySnapshot:
        global_fast_available = max(
            0,
            self._global_fast_credit_limit() - self._tcp_handoff_lease_count,
        )
        normal_reservable = min(
            max(0, int(self._backlog_count)),
            global_fast_available,
        )
        lookahead_snapshot = self._lookahead_index.snapshot(
            eligible_count=normal_reservable,
        )

        reward_candidates = sum(
            max(0, int(self._cohort_queued_inventory.get(cohort_key, 0)))
            for cohort_key, _state in (
                self._production_service_cohorts_with_inventory_unlocked()
            )
        )
        if self._legacy_structural_routing_for_test:
            for grants in tuple(self._exact_rewards.values()):
                for grant in tuple(grants):
                    self._refresh_reward_grant_lifetime_unlocked(grant)
                    if grant.expired or grant.exact_handoffs_remaining <= 0:
                        continue
                    reward_candidates += min(
                        self._grant_exact_inventory_count_unlocked(grant),
                        max(0, int(grant.exact_handoffs_remaining)),
                    )
            reward_candidates += sum(
                max(
                    0,
                    int(self._cohort_queued_inventory.get(cohort_key, 0)),
                )
                for cohort_key, _state in (
                    self._production_similar_cohorts_with_inventory_unlocked()
                )
            )
        reward_lane_remaining = self._handoff_lane_budget_unlocked(
            max(1, global_fast_available)
        ).reward_remaining
        reward_reservable = min(
            reward_candidates,
            global_fast_available,
            self.reward_candidate_limit,
            reward_lane_remaining,
        )
        # Source-agnostic root/family rotation supersedes the old escape lane.
        # These fields remain zero-valued aliases for one report schema version.
        escape_eligible = 0
        responsive_lcb = self._tcp_responsive_probability_lcb_unlocked()
        tcp_window = tuple(self._tcp_outcome_window)
        tcp_completed_samples = sum(
            1
            for _ticket_id, outcome in tcp_window
            if outcome in {"responsive", "nonresponsive"}
        )
        tcp_hard_censored_samples = sum(
            1
            for _ticket_id, outcome in tcp_window
            if outcome == "hard_censored"
        )
        tcp_start_samples = len(tcp_window)
        tcp_responsive_samples = sum(
            1
            for _ticket_id, outcome in tcp_window
            if outcome == "responsive"
        )
        tcp_start_acked_responsive_rate = (
            tcp_responsive_samples / float(tcp_start_samples)
            if tcp_start_samples > 0
            else 0.0
        )
        tcp_completed_result_rate = (
            tcp_completed_samples / float(tcp_start_samples)
            if tcp_start_samples > 0
            else 0.0
        )
        # Supply is a capacity forecast, not an arm-quality posterior.  It uses
        # every real start ack in the denominator, including pending and hard
        # censored work, so incomplete transports cannot inflate expected FAST
        # arrivals. Keep the conservative 25% prior only while evidence is
        # sparse; reward learning continues to use its own confidence bounds.
        effective_probability = (
            0.25
            if tcp_start_samples < 8
            else tcp_start_acked_responsive_rate
        )
        expected_responsive_arrivals = int(
            math.ceil(self._tcp_handoff_lease_count * effective_probability)
        )
        leaf_confirmation_states = self._leaf_confirmation_open_cohorts_unlocked()
        parent_discovery_states = self._parent_discovery_open_cohorts_unlocked()
        parent_discovery_overdue_rounds = max(
            (
                max(
                    0,
                    int(self._tcp_handoff_total)
                    - max(
                        int(state.discovery_eligible_handoff_total),
                        int(state.discovery_last_selected_handoff_total),
                    ),
                )
                // 16
                for _cohort_key, state in parent_discovery_states
            ),
            default=0,
        )
        required_arrivals = max(1, int(self._free_fast_slots))
        if (
            normal_reservable > 0
            or reward_reservable > 0
            or escape_eligible > 0
        ):
            supply_state = "ready"
        elif (
            self._tcp_handoff_lease_count > 0
            and expected_responsive_arrivals >= required_arrivals
        ):
            supply_state = "imminent"
        elif self._backlog_count > 0 or self._tcp_handoff_lease_count > 0:
            supply_state = "blocked"
        else:
            supply_state = "exhausted"
        return Tier3CandidateSupplySnapshot(
            raw_backlog=max(0, int(self._backlog_count)),
            normal_reservable_count=max(0, int(normal_reservable)),
            reward_reservable_count=max(0, int(reward_reservable)),
            escape_eligible_bucket_count=max(0, int(escape_eligible)),
            reserved_handoff_count=max(0, int(self._reserved_handoff_count)),
            active_escape_lease_count=max(
                0,
                int(self._active_escape_lease_count),
            ),
            fast_supply_state=supply_state,
            unique_backlog=max(0, int(self._backlog_count)),
            duplicate_observation_count=max(
                0,
                int(self._duplicate_observation_count),
            ),
            deep_retained_count=max(0, int(self._parked_deep_debt)),
            deep_retention_limit=self._global_deep_credit_limit(),
            handoff_window_limit=max(1, int(self._handoff_window_limit)),
            tcp_execution_limit=self._global_fast_credit_limit(),
            tcp_handoff_lease_count=max(0, int(self._tcp_handoff_lease_count)),
            feedback_only_ticket_count=self._feedback_only_ticket_count_unlocked(),
            expected_responsive_arrivals=max(
                0,
                int(expected_responsive_arrivals),
            ),
            tcp_responsive_probability_lcb=round(responsive_lcb, 6),
            tcp_start_acked_sample_count=max(0, int(tcp_start_samples)),
            tcp_start_acked_responsive_rate=round(
                tcp_start_acked_responsive_rate,
                6,
            ),
            tcp_completed_sample_count=max(0, int(tcp_completed_samples)),
            tcp_hard_censored_sample_count=max(
                0,
                int(tcp_hard_censored_samples),
            ),
            tcp_completed_result_rate=round(tcp_completed_result_rate, 6),
            expected_redeemable_fast_arrivals=max(
                0,
                int(expected_responsive_arrivals),
            ),
            free_fast_slots=max(0, int(self._free_fast_slots)),
            last_real_wave_fill=max(0, int(self._last_handoff_count)),
            last_wave_root_coverage=max(0, int(self._last_wave_root_coverage)),
            last_wave_family_coverage=max(
                0,
                int(self._last_wave_family_coverage),
            ),
            leaf_confirmation_reservable_count=sum(
                max(0, int(self._cohort_cold_queued_inventory.get(cohort_key, 0)))
                for cohort_key, _state in leaf_confirmation_states
            ),
            leaf_confirmation_outstanding_count=(
                self._cohort_counter_total_unlocked(
                    "confirmation_outstanding_total"
                )
            ) + sum(
                max(0, int(value))
                for value in self._reserved_confirmation_handoffs.values()
            ),
            leaf_confirmation_fast_pending_count=(
                self._leaf_confirmation_fast_pending_unlocked()
            ),
            leaf_confirmation_deep_pending_count=(
                self._leaf_confirmation_deep_pending_unlocked()
            ),
            parent_discovery_reservable_count=sum(
                max(0, int(self._cohort_queued_inventory.get(cohort_key, 0)))
                for cohort_key, _state in parent_discovery_states
            ),
            parent_discovery_outstanding_count=(
                self._cohort_counter_total_unlocked(
                    "discovery_outstanding_total"
                )
            ) + sum(
                max(0, int(value))
                for value in self._reserved_discovery_handoffs.values()
            ),
            parent_discovery_overdue_rounds=max(
                0,
                int(parent_discovery_overdue_rounds),
            ),
            lookahead_indexed_count=lookahead_snapshot.indexed_count,
            lookahead_eligible_count=lookahead_snapshot.eligible_count,
            lookahead_examined_total=lookahead_snapshot.examined_total,
            lookahead_hit_total=lookahead_snapshot.hit_total,
            lookahead_validated_positive_count=(
                lookahead_snapshot.validated_positive_count
            ),
            lookahead_validated_reprobe_count=(
                lookahead_snapshot.validated_reprobe_count
            ),
            lookahead_tcp_only_count=lookahead_snapshot.tcp_only_count,
            lookahead_exact_positive_count=(
                lookahead_snapshot.validated_positive_count
            ),
            lookahead_unknown_count=lookahead_snapshot.unknown_count,
            lookahead_observed_nonpositive_count=(
                lookahead_snapshot.observed_nonpositive_count
            ),
            lookahead_cursor_wrap_count=(
                lookahead_snapshot.cursor_wrap_count
            ),
            lookahead_max_examined_per_reservation=(
                lookahead_snapshot.max_examined_per_reservation
            ),
            lookahead_mode=(
                "legacy_structural_test"
                if self._legacy_structural_routing_for_test
                else "validated_observation_cursor"
            ),
        )

    def diagnostic_snapshot(self) -> dict[str, Any]:
        with self._lock:
            backlog_by_arm: dict[str, int] = {}
            for arm in self._arms.values():
                for candidate in arm.items:
                    backlog_by_arm[candidate.arm_key] = (
                        backlog_by_arm.get(candidate.arm_key, 0) + 1
                    )
            return {
                **self.quick_metrics_unlocked(),
                "backlog_by_arm": backlog_by_arm,
            }

    def _top_cohort_diagnostics_unlocked(
        self,
        scope: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return a bounded opaque view of the strongest live cohorts."""

        def diagnostic_priority(item: tuple[str, _CohortYieldState]) -> tuple:
            cohort_key, state = item
            return (
                sum(1 for sample in state.samples if sample[1] and not sample[2]),
                sum(1 for sample in state.samples if not sample[2]),
                len(state.samples),
                str(cohort_key).split(":", 1)[-1],
            )

        # Inventory projection scans active tickets. Rank the bounded report
        # first so hidden diagnostic rows cannot hold the scheduler lock.
        strongest = heapq.nlargest(
            max(1, int(limit)),
            (
                item for item in self._cohort_yield_states.items()
                if item[1].scope == scope
            ),
            key=diagnostic_priority,
        )
        rows: list[dict[str, Any]] = []
        for cohort_key, state in strongest:
            completed = [sample for sample in state.samples if not sample[2]]
            passes = sum(1 for sample in completed if sample[1])
            exposure_ms = sum(sample[3] for sample in state.samples)
            production_completed = [
                sample
                for sample in state.production_samples
                if not sample[2]
            ]
            production_passes = sum(
                1 for sample in production_completed if sample[1]
            )
            production_exposure_ms = sum(
                sample[3] for sample in state.production_samples
            )
            inventory = (
                self._cohort_inventory_unlocked(cohort_key)
                if scope == "service"
                else {
                    "reservable": 0,
                    "reserved": 0,
                    "active": 0,
                    "root_family_count": 0,
                }
            )
            rows.append(
                {
                    # Production service profiles are process-scoped HMACs.
                    # Keep the prefix out of the report so this remains an
                    # opaque correlation key rather than a scheduler key.
                    "cohort_id": str(cohort_key).split(":", 1)[-1],
                    "window_sample_count": len(state.samples),
                    "window_completion_count": len(completed),
                    "window_pass_count": passes,
                    "window_exposure_worker_seconds": round(
                        exposure_ms / 1000.0,
                        3,
                    ),
                    "sample_total": int(state.sample_total),
                    "completed_total": int(state.completed_total),
                    "pass_total": int(state.pass_total),
                    "last_evaluated_completions": int(
                        state.last_evaluated_completions
                    ),
                    "last_evaluated_reference_completions": int(
                        state.last_evaluated_reference_completions
                    ),
                    # One report schema keeps the former key as an exact alias.
                    "last_evaluated_samples": int(
                        state.last_evaluated_completions
                    ),
                    "winning_windows": int(state.winning_windows),
                    "losing_windows": int(state.losing_windows),
                    "first_evidence_sequence": int(
                        state.first_evidence_sequence
                    ),
                    "hierarchy_level": int(state.hierarchy_level),
                    "leaf_eligible": bool(state.leaf_eligible),
                    "parent_cohort_id": (
                        str(state.parent_key).split(":", 1)[-1]
                        if state.parent_key
                        else ""
                    ),
                    "advantage_confirmed": bool(state.advantage_confirmed),
                    "discovery": bool(state.discovery),
                    "probation": bool(state.probation),
                    "promoted": bool(state.promoted),
                    "lifecycle_state": str(state.lifecycle_state),
                    "eligibility": state.eligibility.value,
                    "prior_support_count": int(state.prior_support_count),
                    "prior_child_count": int(state.prior_child_count),
                    "current_pass_child_count": len(
                        state.current_pass_child_keys
                    ),
                    "current_pass_root_family_count": len(
                        state.current_pass_root_families
                    ),
                    "discovery_selection_total": int(
                        state.discovery_selection_total
                    ),
                    "discovery_outstanding": int(
                        state.discovery_outstanding_total
                    ),
                    "discovery_eligible_handoff_total": int(
                        state.discovery_eligible_handoff_total
                    ),
                    "discovery_last_selected_handoff_total": int(
                        state.discovery_last_selected_handoff_total
                    ),
                    "discovery_wait_rounds": max(
                        0,
                        int(self._tcp_handoff_total)
                        - max(
                            int(state.discovery_eligible_handoff_total),
                            int(state.discovery_last_selected_handoff_total),
                        ),
                    ) // 16,
                    "discovery_selected_child_count": len(
                        state.discovery_selected_child_keys
                    ),
                    "queued_direct_child_count": len(
                        self._cohort_queued_direct_children.get(cohort_key, {})
                    ),
                    "discovery_exhausted": bool(state.discovery_exhausted),
                    "production_selection_total": int(
                        state.production_selection_total
                    ),
                    "production_completion_total": int(
                        state.production_completion_total
                    ),
                    "production_pass_total": int(state.production_pass_total),
                    "production_resolved_total": int(
                        state.production_resolved_total
                    ),
                    "production_net_new_total": int(
                        state.production_net_new_total
                    ),
                    "production_neutral_total": int(
                        state.production_neutral_total
                    ),
                    "production_censored_total": int(
                        state.production_censored_total
                    ),
                    "production_outstanding": int(
                        state.production_outstanding_total
                    ),
                    "production_outstanding_high_water": int(
                        state.production_outstanding_high_water
                    ),
                    "probation_outstanding": int(
                        state.probation_outstanding_total
                    ),
                    "probation_outstanding_high_water": int(
                        state.probation_outstanding_high_water
                    ),
                    "proven_outstanding": int(state.proven_outstanding_total),
                    "confirmation_selection_total": int(
                        state.confirmation_selection_total
                    ),
                    "confirmation_epoch": int(state.confirmation_epoch),
                    "confirmation_completion_total": int(
                        state.confirmation_completion_total
                    ),
                    "confirmation_pass_total": int(
                        state.confirmation_pass_total
                    ),
                    "confirmation_censored_total": int(
                        state.confirmation_censored_total
                    ),
                    "confirmation_evidence_pass_total": int(
                        state.confirmation_evidence_pass_total
                    ),
                    "confirmation_stale_transition_ignored_total": int(
                        state.confirmation_stale_transition_ignored_total
                    ),
                    "confirmation_outstanding": int(
                        state.confirmation_outstanding_total
                    ),
                    "confirmation_outstanding_high_water": int(
                        state.confirmation_outstanding_high_water
                    ),
                    "production_worker_seconds": round(
                        state.production_worker_ms / 1000.0,
                        3,
                    ),
                    "probation_selection_total": int(
                        state.probation_selection_total
                    ),
                    "probation_completion_total": int(
                        state.probation_completion_total
                    ),
                    "probation_pass_total": int(state.probation_pass_total),
                    "probation_worker_seconds": round(
                        state.probation_worker_ms / 1000.0,
                        3,
                    ),
                    "proven_selection_total": int(
                        state.proven_selection_total
                    ),
                    "proven_completion_total": int(
                        state.proven_completion_total
                    ),
                    "proven_pass_total": int(state.proven_pass_total),
                    "proven_worker_seconds": round(
                        state.proven_worker_ms / 1000.0,
                        3,
                    ),
                    "production_window_sample_count": len(
                        state.production_samples
                    ),
                    "production_window_completion_count": len(
                        production_completed
                    ),
                    "production_window_pass_count": production_passes,
                    "production_window_worker_seconds": round(
                        production_exposure_ms / 1000.0,
                        3,
                    ),
                    "inventory_reservable": int(inventory["reservable"]),
                    "inventory_cold_eligible": int(
                        self._cohort_cold_queued_inventory.get(cohort_key, 0)
                    ),
                    "inventory_reserved": int(inventory["reserved"]),
                    "inventory_active": int(inventory["active"]),
                    "inventory_root_family_count": int(
                        inventory["root_family_count"]
                    ),
                    "exploit_share": round(float(state.exploit_share), 3),
                    "evaluation_count": int(state.evaluation_count),
                    "decision_reason": str(state.last_decision_reason),
                }
            )
        return rows

    def quick_metrics_unlocked(self) -> dict[str, Any]:
        supply = self._supply_snapshot_unlocked()
        lane_budget = self._handoff_lane_budget_unlocked(1)
        # L0-L3 are ancestor views of the same FAST observation.  Keep the
        # per-cohort diagnostics, but never present their sum as independent
        # candidate samples.
        cohort_scope_metrics: dict[str, dict[str, int]] = {}
        for scope in ("exact", "service", "similar"):
            cohort_scope_metrics[scope] = {
                "completion_count": int(
                    self._fast_observation_completion_count
                ),
                "pass_count": int(self._fast_observation_pass_count),
                "exposure_worker_ms": int(
                    self._fast_observation_exposure_ms
                ),
            }
        service_level_metrics: dict[str, dict[str, int]] = {}
        for state in self._cohort_yield_states.values():
            if state.scope != "service":
                continue
            level = str(max(0, int(state.hierarchy_level)))
            row = service_level_metrics.setdefault(
                level,
                {
                    "cohort_count": 0,
                    "observation_completion_count": 0,
                    "observation_pass_count": 0,
                    "discovery_selection_count": 0,
                    "production_selection_count": 0,
                    "production_completion_count": 0,
                    "production_pass_count": 0,
                    "production_worker_ms": 0,
                    "production_resolved_count": 0,
                    "production_net_new_count": 0,
                    "production_neutral_count": 0,
                    "production_censored_count": 0,
                    "production_outstanding": 0,
                    "production_outstanding_high_water": 0,
                    "probation_selection_count": 0,
                    "probation_completion_count": 0,
                    "probation_pass_count": 0,
                    "probation_worker_ms": 0,
                    "proven_selection_count": 0,
                    "proven_completion_count": 0,
                    "proven_pass_count": 0,
                    "proven_worker_ms": 0,
                },
            )
            row["cohort_count"] += 1
            row["observation_completion_count"] += int(state.completed_total)
            row["observation_pass_count"] += int(state.pass_total)
            row["discovery_selection_count"] += int(
                state.discovery_selection_total
            )
            row["production_selection_count"] += int(
                state.production_selection_total
            )
            row["production_completion_count"] += int(
                state.production_completion_total
            )
            row["production_pass_count"] += int(state.production_pass_total)
            row["production_worker_ms"] += int(state.production_worker_ms)
            row["production_resolved_count"] += int(
                state.production_resolved_total
            )
            row["production_net_new_count"] += int(
                state.production_net_new_total
            )
            row["production_neutral_count"] += int(
                state.production_neutral_total
            )
            row["production_censored_count"] += int(
                state.production_censored_total
            )
            row["production_outstanding"] += int(
                state.production_outstanding_total
            )
            row["production_outstanding_high_water"] += int(
                state.production_outstanding_high_water
            )
            row["probation_selection_count"] += int(
                state.probation_selection_total
            )
            row["probation_completion_count"] += int(
                state.probation_completion_total
            )
            row["probation_pass_count"] += int(state.probation_pass_total)
            row["probation_worker_ms"] += int(state.probation_worker_ms)
            row["proven_selection_count"] += int(
                state.proven_selection_total
            )
            row["proven_completion_count"] += int(
                state.proven_completion_total
            )
            row["proven_pass_count"] += int(state.proven_pass_total)
            row["proven_worker_ms"] += int(state.proven_worker_ms)
        origin_structural_states = [
            state
            for cohort_key, state in self._cohort_yield_states.items()
            if cohort_key in self._origin_structural_cohort_keys
        ]
        active_grants: list[_FamilyRewardGrant] = []
        for grant in tuple(self._reward_grants_by_id.values()):
            self._refresh_reward_grant_lifetime_unlocked(grant)
            if self._reward_credit_remaining(grant) > 0 and not grant.expired:
                active_grants.append(grant)
            else:
                self._expire_family_reward_unlocked(
                    (grant.origin_root, grant.origin_family),
                    grant,
                )
        rewarded_arm_count = len(
            {
                grant.preferred_arm_id
                for grant in active_grants
                if grant.exact_handoffs_remaining > 0
                and self._grant_exact_inventory_count_unlocked(grant) > 0
            }
        )
        reward_handoff_credit = sum(
            self._reward_credit_remaining(grant)
            for grant in active_grants
            if self._grant_exact_inventory_count_unlocked(grant) > 0
        )
        dormant_exact_grants = [
            grant
            for grant in active_grants
            if grant.inventory_activated_monotonic_ns <= 0
        ]
        inventory_active_exact_grants = [
            grant
            for grant in active_grants
            if grant.inventory_activated_monotonic_ns > 0
        ]
        ready_arm_count = sum(
            max(0, int(credit.ready_arm_count))
            for credit in self._bucket_credits.values()
        )
        blocked_arm_count = (
            0
            if self._global_normal_dispatch_capacity_available()
            else ready_arm_count
        )
        normal_dispatchable_arm_count = max(
            0,
            ready_arm_count - blocked_arm_count,
        )
        overdue_credit_bucket_count = sum(
            1
            for debt in self._credit_escape_epoch_debt.values()
            if int(debt) >= 2
        )
        missed_epochs_by_root_family = {
            f"{root}|{family}": int(debt)
            for (root, family), debt in sorted(
                self._credit_escape_epoch_debt.items(),
                key=lambda item: (-int(item[1]), item[0][0], item[0][1]),
            )[:32]
            if int(debt) > 0
        }
        return {
            "backlog_count": self._backlog_count,
            "active_root_count": len(self._active_roots),
            "active_arm_count": self._active_arm_count,
            "overflow_arm_count": self._overflow_arm_count,
            "overflow_rotation_count": self._overflow_rotation_count,
            "unknown_overflow_rotation_count": (
                self._unknown_overflow_rotation_count
            ),
            "tick_count": self._tick_count,
            "selected_total": self._selected_total,
            "handoff_epoch_size": lane_budget.epoch_size,
            "handoff_projected_total": lane_budget.projected_handoffs,
            "handoff_coverage_limit": lane_budget.coverage_limit,
            "handoff_coverage_remaining": lane_budget.coverage_remaining,
            "handoff_unknown_floor": lane_budget.unknown_floor,
            "handoff_unknown_remaining": lane_budget.unknown_remaining,
            "handoff_exact_positive_limit": (
                lane_budget.exact_positive_limit
            ),
            "handoff_exact_positive_remaining": (
                lane_budget.exact_positive_remaining
            ),
            "handoff_validated_replay_limit": (
                lane_budget.validated_replay_limit
            ),
            "handoff_validated_replay_remaining": (
                lane_budget.validated_replay_remaining
            ),
            "handoff_validated_reprobe_limit": (
                lane_budget.validated_reprobe_limit
            ),
            "handoff_validated_reprobe_remaining": (
                lane_budget.validated_reprobe_remaining
            ),
            "handoff_fresh_replay_limit": lane_budget.fresh_replay_limit,
            "handoff_fresh_replay_remaining": (
                lane_budget.fresh_replay_remaining
            ),
            "handoff_historical_reprobe_limit": (
                lane_budget.historical_reprobe_limit
            ),
            "handoff_historical_reprobe_remaining": (
                lane_budget.historical_reprobe_remaining
            ),
            "handoff_structural_production_limit": (
                lane_budget.structural_production_limit
            ),
            "handoff_structural_production_remaining": (
                lane_budget.structural_production_remaining
            ),
            "handoff_combined_priority_limit": (
                lane_budget.combined_priority_limit
            ),
            "handoff_combined_priority_remaining": (
                lane_budget.combined_priority_remaining
            ),
            "handoff_reward_limit": lane_budget.reward_limit,
            "handoff_reward_remaining": lane_budget.reward_remaining,
            "handoff_discovery_limit": lane_budget.discovery_limit,
            "handoff_discovery_remaining": lane_budget.discovery_remaining,
            "handoff_epoch_exact_count": lane_budget.exact_committed,
            "handoff_epoch_leaf_confirmation_count": (
                lane_budget.leaf_confirmation_committed
            ),
            "handoff_epoch_service_discovery_count": (
                lane_budget.service_discovery_committed
            ),
            "handoff_epoch_service_probation_count": (
                lane_budget.service_probation_committed
            ),
            "handoff_epoch_service_proven_count": (
                lane_budget.service_proven_committed
            ),
            "handoff_epoch_ordinary_count": lane_budget.ordinary_committed,
            "handoff_epoch_committed_count": len(self._handoff_lane_history),
            "handoff_epoch_coverage_count": sum(
                1
                for coverage, _unknown, _reward in self._handoff_lane_history
                if coverage
            ),
            "handoff_epoch_unknown_count": sum(
                1
                for _coverage, unknown, _reward in self._handoff_lane_history
                if unknown
            ),
            "handoff_epoch_reward_count": sum(
                1
                for _coverage, _unknown, reward in self._handoff_lane_history
                if reward
            ),
            "coverage_selected_total": self._coverage_selected_total,
            "reserved_coverage_count": self._reserved_coverage_count,
            "reservation_total": self._reservation_total,
            "replay_leaf_discovery": self._replay_leaf_discovery_snapshot_unlocked(),
            "reserved_handoff_count": self._reserved_handoff_count,
            "reserved_exploit_count": self._reserved_exploit_count,
            "reserved_unknown_count": self._reserved_unknown_count,
            "reserved_escape_count": self._reserved_escape_count,
            "reserved_probation_handoff_count": sum(
                max(0, int(value))
                for value in self._reserved_probation_handoffs.values()
            ),
            "reserved_service_handoff_count": sum(
                max(0, int(value))
                for value in self._reserved_service_handoffs.values()
            ),
            "reserved_similar_handoff_count": sum(
                max(0, int(value))
                for value in self._reserved_similar_handoffs.values()
            ),
            "reserved_discovery_handoff_count": sum(
                max(0, int(value))
                for value in self._reserved_discovery_handoffs.values()
            ),
            "reserved_leaf_confirmation_handoff_count": sum(
                max(0, int(value))
                for value in self._reserved_confirmation_handoffs.values()
            ),
            "reserved_cold_leaf_coverage_count": sum(
                max(0, int(value))
                for value in self._reserved_cold_leaf_handoffs.values()
            ),
            "cold_leaf_coverage_selected_total": (
                self._cold_leaf_coverage_selected_total
            ),
            "cold_leaf_coverage_started_total": (
                self._cold_leaf_coverage_started_total
            ),
            "cold_leaf_sampled_distinct_started": len(self._cold_leaf_sampled_keys),
            "cold_l2_sampled_distinct_started": len(self._cold_l2_sampled_keys),
            "cold_leaf_coverage_distinct_started": len(
                self._cold_leaf_started_keys
            ),
            "cold_l2_training_outstanding_count": (
                self._cold_l2_training_outstanding_total_unlocked()
            ),
            "cold_l2_training_outstanding_high_water": int(
                self._cold_l2_training_outstanding_high_water
            ),
            "cold_l2_training_cohort_count": (
                self._cold_l2_training_cohort_count_unlocked()
            ),
            "cold_l2_training_current_limit": int(
                self._cold_l2_training_current_limit_unlocked()
            ),
            "cold_l2_training_deferred_total": int(
                self._cold_l2_training_deferred_total
            ),
            "cold_l2_training_borrowed_total": int(
                self._cold_l2_training_borrowed_total
            ),
            "probation_handoff_window_count": sum(
                1 for cohort_key in self._probation_handoff_history if cohort_key
            ),
            "normal_reservable_count": supply.normal_reservable_count,
            "reward_reservable_count": supply.reward_reservable_count,
            "escape_eligible_bucket_count": supply.escape_eligible_bucket_count,
            "active_escape_lease_count": supply.active_escape_lease_count,
            "fast_supply_state": supply.fast_supply_state,
            "leaf_confirmation_reservable_count": (
                supply.leaf_confirmation_reservable_count
            ),
            "leaf_confirmation_outstanding_count": (
                supply.leaf_confirmation_outstanding_count
            ),
            "leaf_confirmation_fast_pending_count": (
                supply.leaf_confirmation_fast_pending_count
            ),
            "leaf_confirmation_deep_pending_count": (
                supply.leaf_confirmation_deep_pending_count
            ),
            "parent_discovery_reservable_count": (
                supply.parent_discovery_reservable_count
            ),
            "parent_discovery_outstanding_count": (
                supply.parent_discovery_outstanding_count
            ),
            "parent_discovery_overdue_rounds": (
                supply.parent_discovery_overdue_rounds
            ),
            "lookahead_indexed_count": supply.lookahead_indexed_count,
            "lookahead_eligible_count": supply.lookahead_eligible_count,
            "lookahead_examined_total": supply.lookahead_examined_total,
            "lookahead_hit_total": supply.lookahead_hit_total,
            "lookahead_validated_positive_count": (
                supply.lookahead_validated_positive_count
            ),
            "lookahead_validated_reprobe_count": (
                supply.lookahead_validated_reprobe_count
            ),
            "lookahead_tcp_only_count": supply.lookahead_tcp_only_count,
            "lookahead_exact_positive_count": (
                supply.lookahead_validated_positive_count
            ),
            "lookahead_unknown_count": supply.lookahead_unknown_count,
            "lookahead_observed_nonpositive_count": (
                supply.lookahead_observed_nonpositive_count
            ),
            "lookahead_cursor_wrap_count": (
                supply.lookahead_cursor_wrap_count
            ),
            "lookahead_max_examined_per_reservation": (
                supply.lookahead_max_examined_per_reservation
            ),
            "lookahead_mode": supply.lookahead_mode,
            "visibility_validated_positive_reserved_total": int(
                self._visibility_reserved_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_reserved_total": int(
                self._visibility_reserved_total.get("validated_reprobe", 0)
            ),
            "visibility_tcp_only_reserved_total": int(
                self._visibility_reserved_total.get("tcp_only", 0)
            ),
            "visibility_exact_positive_reserved_total": int(
                self._visibility_reserved_total.get("validated_positive", 0)
            ),
            "visibility_unknown_reserved_total": int(
                self._visibility_reserved_total.get("unknown", 0)
            ),
            "visibility_observed_nonpositive_reserved_total": int(
                self._visibility_reserved_total.get(
                    "observed_nonpositive",
                    0,
                )
            ),
            "visibility_validated_positive_start_total": int(
                self._visibility_start_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_start_total": int(
                self._visibility_start_total.get("validated_reprobe", 0)
            ),
            "visibility_tcp_only_start_total": int(
                self._visibility_start_total.get("tcp_only", 0)
            ),
            "visibility_exact_positive_start_total": int(
                self._visibility_start_total.get("validated_positive", 0)
            ),
            "visibility_unknown_start_total": int(
                self._visibility_start_total.get("unknown", 0)
            ),
            "visibility_observed_nonpositive_start_total": int(
                self._visibility_start_total.get("observed_nonpositive", 0)
            ),
            "visibility_validated_positive_responsive_total": int(
                self._visibility_responsive_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_responsive_total": int(
                self._visibility_responsive_total.get("validated_reprobe", 0)
            ),
            "visibility_tcp_only_responsive_total": int(
                self._visibility_responsive_total.get("tcp_only", 0)
            ),
            "visibility_exact_positive_responsive_total": int(
                self._visibility_responsive_total.get("validated_positive", 0)
            ),
            "visibility_unknown_responsive_total": int(
                self._visibility_responsive_total.get("unknown", 0)
            ),
            "visibility_observed_nonpositive_responsive_total": int(
                self._visibility_responsive_total.get(
                    "observed_nonpositive",
                    0,
                )
            ),
            "visibility_validated_positive_fast_pass_total": int(
                self._visibility_fast_pass_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_fast_pass_total": int(
                self._visibility_fast_pass_total.get("validated_reprobe", 0)
            ),
            "visibility_tcp_only_fast_pass_total": int(
                self._visibility_fast_pass_total.get("tcp_only", 0)
            ),
            "visibility_exact_positive_fast_pass_total": int(
                self._visibility_fast_pass_total.get("validated_positive", 0)
            ),
            "visibility_unknown_fast_pass_total": int(
                self._visibility_fast_pass_total.get("unknown", 0)
            ),
            "visibility_observed_nonpositive_fast_pass_total": int(
                self._visibility_fast_pass_total.get(
                    "observed_nonpositive",
                    0,
                )
            ),
            "visibility_validated_positive_net_new_total": int(
                self._visibility_net_new_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_net_new_total": int(
                self._visibility_net_new_total.get("validated_reprobe", 0)
            ),
            "visibility_tcp_only_net_new_total": int(
                self._visibility_net_new_total.get("tcp_only", 0)
            ),
            "visibility_exact_positive_net_new_total": int(
                self._visibility_net_new_total.get("validated_positive", 0)
            ),
            "visibility_unknown_net_new_total": int(
                self._visibility_net_new_total.get("unknown", 0)
            ),
            "visibility_observed_nonpositive_net_new_total": int(
                self._visibility_net_new_total.get(
                    "observed_nonpositive",
                    0,
                )
            ),
            "visibility_validated_positive_resolved_total": int(
                self._visibility_resolved_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_resolved_total": int(
                self._visibility_resolved_total.get("validated_reprobe", 0)
            ),
            "visibility_unknown_resolved_total": int(
                self._visibility_resolved_total.get("unknown", 0)
            ),
            "visibility_tcp_only_resolved_total": int(
                self._visibility_resolved_total.get("tcp_only", 0)
            ),
            "visibility_observed_nonpositive_resolved_total": int(
                self._visibility_resolved_total.get(
                    "observed_nonpositive",
                    0,
                )
            ),
            "visibility_validated_positive_window_resolved_count": len(
                self._visibility_outcome_windows.get(
                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                    (),
                )
            ),
            "visibility_validated_positive_window_net_new_count": sum(
                1
                for passed in self._visibility_outcome_windows.get(
                    Tier3CandidateVisibilityIndex.VALIDATED_POSITIVE,
                    (),
                )
                if passed
            ),
            "visibility_validated_reprobe_window_resolved_count": len(
                self._visibility_outcome_windows.get(
                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                    (),
                )
            ),
            "visibility_validated_reprobe_window_net_new_count": sum(
                1
                for passed in self._visibility_outcome_windows.get(
                    Tier3CandidateVisibilityIndex.VALIDATED_REPROBE,
                    (),
                )
                if passed
            ),
            "visibility_unknown_window_resolved_count": len(
                self._visibility_outcome_windows.get(
                    Tier3CandidateVisibilityIndex.UNKNOWN,
                    (),
                )
            ),
            "visibility_unknown_window_net_new_count": sum(
                1
                for passed in self._visibility_outcome_windows.get(
                    Tier3CandidateVisibilityIndex.UNKNOWN,
                    (),
                )
                if passed
            ),
            "visibility_validated_positive_censored_total": int(
                self._visibility_censored_total.get("validated_positive", 0)
            ),
            "visibility_validated_reprobe_censored_total": int(
                self._visibility_censored_total.get("validated_reprobe", 0)
            ),
            "visibility_unknown_censored_total": int(
                self._visibility_censored_total.get("unknown", 0)
            ),
            "visibility_tcp_only_censored_total": int(
                self._visibility_censored_total.get("tcp_only", 0)
            ),
            "tcp_hint_policy": {
                "mode": "shadow_only",
                "limited": False,
                "coverage_limit": 4,
                "shadow_limited": self._tcp_hint_yield_limited,
                "shadow_coverage_limit": 1 if self._tcp_hint_yield_limited else 4,
                "window_fast_observations": len(self._tcp_hint_fast_window),
                "window_fast_passes": sum(int(sample[0]) for sample in self._tcp_hint_fast_window),
                "window_worker_ms": sum(sample[1] for sample in self._tcp_hint_fast_window),
                "limited_count": 0,
                "restored_by_net_new_count": 0,
                "shadow_limited_count": self._tcp_hint_limit_count,
                "shadow_restored_by_net_new_count": self._tcp_hint_restore_count,
                "writes_negative": False,
            },
            "visibility_observed_nonpositive_censored_total": int(
                self._visibility_censored_total.get(
                    "observed_nonpositive",
                    0,
                )
            ),
            "visibility_validated_positive_exhausted_at_ms": (
                self._visibility_inventory_exhausted_at_ms.get(
                    "validated_positive"
                )
                if self._lookahead_index.count("validated_positive") <= 0
                else None
            ),
            "visibility_validated_reprobe_exhausted_at_ms": (
                self._visibility_inventory_exhausted_at_ms.get(
                    "validated_reprobe"
                )
                if self._lookahead_index.count("validated_reprobe") <= 0
                else None
            ),
            "visibility_unknown_exhausted_at_ms": (
                self._visibility_inventory_exhausted_at_ms.get("unknown")
                if self._lookahead_index.count("unknown") <= 0
                else None
            ),
            "visibility_fast_cost": {
                lane: dict(cost) for lane, cost in self._fast_visibility_cost.items()
            },
            "visibility_resolved_total": int(
                sum(self._visibility_resolved_total.values())
            ),
            "visibility_censored_total": int(
                sum(self._visibility_censored_total.values())
            ),
            "fresh_replay_adaptive_quota": int(self._fresh_replay_quota),
            "fresh_replay_winning_windows": int(
                self._fresh_replay_winning_windows
            ),
            "fresh_replay_losing_windows": int(
                self._fresh_replay_losing_windows
            ),
            "historical_reprobe_adaptive_quota": int(
                self._historical_reprobe_quota
            ),
            "visibility_budget_adjustment_count": int(
                self._visibility_budget_adjustment_count
            ),
            "production_structural_routing_enabled": True,
            "production_structural_routing_mode": (
                "exact_l3_with_direct_child_discovery_and_similar_shadow"
            ),
            "cancelled_reservation_total": self._cancelled_reservation_total,
            "keys_examined_total": self._keys_examined_total,
            "idle_with_supported_backlog_count": self._idle_with_backlog_count,
            "last_tick_ms": round(self._last_tick_ms, 3),
            "last_tick_selected": self._last_tick_selected,
            "last_tick_keys_examined": self._last_tick_keys_examined,
            "last_tick_exploit_selected": self._last_tick_exploit_selected,
            "last_tick_unknown_selected": self._last_tick_unknown_selected,
            "last_tick_credit_escape_selected": (
                self._last_tick_credit_escape_selected
            ),
            "last_tick_reserved": self._last_tick_selected,
            "last_tick_exploit_reserved": self._last_tick_exploit_reserved,
            "last_tick_unknown_reserved": self._last_tick_unknown_reserved,
            "last_tick_credit_escape_reserved": (
                self._last_tick_credit_escape_reserved
            ),
            "last_handoff_count": self._last_handoff_count,
            "last_tick_exploit_share": round(
                self._last_tick_exploit_selected
                / float(max(1, self._last_tick_selected)),
                4,
            ),
            "exploit_scan_share_limit": self._adaptive_exploit_scan_share,
            "exploit_scan_share_baseline": self.exploit_scan_share,
            "exploit_selected_total": self._exploit_selected_total,
            "unknown_selected_total": self._unknown_selected_total,
            "credit_escape_selected_total": self._credit_escape_selected_total,
            "tcp_handoff_total": self._tcp_handoff_total,
            "reward_hit_count": self._reward_hit_count,
            "reward_created_count": self._reward_created_count,
            "reward_expired_unused_count": self._reward_expired_unused_count,
            "reward_expired_partial_count": self._reward_expired_partial_count,
            "reward_expired_credit_count": self._reward_expired_credit_count,
            "reward_expired_opportunity_count": (
                self._reward_expired_opportunity_count
            ),
            "reward_expired_wall_time_count": (
                self._reward_expired_wall_time_count
            ),
            "reward_grant_expiry_reason_counts": dict(
                self._reward_expiry_reason_counts
            ),
            "reward_exact_hit_count": self._reward_exact_hit_count,
            "reward_exact_dormant_grant_count": len(dormant_exact_grants),
            "reward_exact_inventory_active_grant_count": len(
                inventory_active_exact_grants
            ),
            "reward_exact_dormant_credit_count": sum(
                max(0, int(grant.exact_handoffs_remaining))
                for grant in dormant_exact_grants
            ),
            "reward_service_hit_count": self._reward_service_hit_count,
            "reward_origin_structural_cohort_count": len(
                origin_structural_states
            ),
            "reward_origin_structural_eligible_count": sum(
                1
                for state in origin_structural_states
                if self._service_eligibility_unlocked(state)
                in {
                    Tier3ServiceCohortEligibility.LEAF_PROBATION,
                    Tier3ServiceCohortEligibility.PROVEN_50,
                    Tier3ServiceCohortEligibility.PROVEN_75,
                }
            ),
            "reward_origin_structural_selected_count": sum(
                int(state.production_selection_total)
                for state in origin_structural_states
            ),
            "reward_origin_structural_resolved_count": sum(
                int(state.production_resolved_total)
                for state in origin_structural_states
            ),
            "reward_origin_structural_pass_count": sum(
                int(state.production_pass_total)
                for state in origin_structural_states
            ),
            "reward_origin_structural_net_new_count": sum(
                int(state.production_net_new_total)
                for state in origin_structural_states
            ),
            "reward_origin_structural_outstanding_count": sum(
                int(state.production_outstanding_total)
                for state in origin_structural_states
            ),
            "reward_origin_structural_inventory_count": sum(
                int(self._cohort_queued_inventory.get(cohort_key, 0))
                for cohort_key in self._origin_structural_cohort_keys
            ),
            "reward_similar_hit_count": self._reward_similar_hit_count,
            "reward_shadow_broad_handoff_count": (
                self._reward_shadow_broad_handoff_count
            ),
            "reward_exact_tcp_responsive_count": (
                self._reward_exact_tcp_responsive_count
            ),
            "reward_service_tcp_responsive_count": (
                self._reward_service_tcp_responsive_count
            ),
            "reward_similar_tcp_responsive_count": (
                self._reward_similar_tcp_responsive_count
            ),
            "reward_shadow_broad_tcp_responsive_count": (
                self._reward_shadow_broad_tcp_responsive_count
            ),
            "reward_exact_fast_pass_count": self._reward_exact_fast_pass_count,
            "reward_service_fast_pass_count": self._reward_service_fast_pass_count,
            "reward_similar_fast_pass_count": (
                self._reward_similar_fast_pass_count
            ),
            "reward_shadow_broad_fast_pass_count": (
                self._reward_shadow_broad_fast_pass_count
            ),
            "reward_exact_net_new_count": self._reward_exact_net_new_count,
            "reward_service_net_new_count": self._reward_service_net_new_count,
            "reward_similar_net_new_count": self._reward_similar_net_new_count,
            "reward_shadow_broad_net_new_count": (
                self._reward_shadow_broad_net_new_count
            ),
            "reward_exact_worker_seconds": round(
                self._reward_exact_worker_ms / 1000.0,
                3,
            ),
            "reward_service_worker_seconds": round(
                self._reward_service_worker_ms / 1000.0,
                3,
            ),
            "reward_similar_worker_seconds": round(
                self._reward_similar_worker_ms / 1000.0,
                3,
            ),
            "reward_shadow_broad_worker_seconds": round(
                self._reward_shadow_broad_worker_ms / 1000.0,
                3,
            ),
            "reward_family_fallback_hit_count": (
                0
            ),
            "reward_first_handoff_latency_last_ms": round(
                self._reward_first_handoff_latency_last_ms,
                3,
            ),
            "reward_first_handoff_latency_max_ms": round(
                self._reward_first_handoff_latency_max_ms,
                3,
            ),
            "reward_expiry_scope": "exact_selection_credit_or_15s_wall_time",
            "reward_exact_selected_count": self._reward_exact_hit_count,
            "reward_exact_outstanding_count": sum(
                max(0, int(grant.outstanding_handoffs))
                for grant in self._reward_grants_by_id.values()
            ),
            "reward_exact_resolved_count": self._reward_exact_resolved_count,
            "reward_exact_neutral_count": self._reward_exact_neutral_count,
            "reward_exact_censored_count": self._reward_exact_censored_count,
            "reward_exact_outstanding_high_water": (
                self._reward_exact_outstanding_high_water
            ),
            "reward_exact_pass_count": self._reward_exact_fast_pass_count,
            "reward_service_selected_count": self._reward_service_hit_count,
            "reward_service_outstanding_count": (
                self._cohort_counter_total_unlocked(
                    "production_outstanding_total",
                    "service",
                )
            ),
            "reward_service_resolved_count": self._reward_service_resolved_count,
            "reward_service_neutral_count": self._reward_service_neutral_count,
            "reward_service_censored_count": self._reward_service_censored_count,
            "reward_service_outstanding_high_water": (
                self._reward_service_outstanding_high_water
            ),
            "reward_service_pass_count": self._reward_service_fast_pass_count,
            "reward_similar_selected_count": self._reward_similar_hit_count,
            "reward_similar_outstanding_count": (
                self._cohort_counter_total_unlocked(
                    "production_outstanding_total",
                    "similar",
                )
            ),
            "reward_similar_resolved_count": self._reward_similar_resolved_count,
            "reward_similar_neutral_count": self._reward_similar_neutral_count,
            "reward_similar_censored_count": self._reward_similar_censored_count,
            "reward_similar_outstanding_high_water": (
                self._reward_similar_outstanding_high_water
            ),
            "reward_similar_pass_count": self._reward_similar_fast_pass_count,
            "reward_probation_outstanding_count": (
                self._cohort_counter_total_unlocked(
                    "probation_outstanding_total"
                )
            ),
            "reward_probation_outstanding_high_water": (
                self._reward_probation_outstanding_high_water
            ),
            "reward_discovery_outstanding_count": (
                self._cohort_counter_total_unlocked(
                    "discovery_outstanding_total"
                )
            ),
            "reward_discovery_outstanding_high_water": (
                self._reward_discovery_outstanding_high_water
            ),
            "scheduling_outcome_counts": dict(self._scheduling_outcome_counts),
            "ordinary_e2e_completion_count": self._ordinary_e2e_completion_total,
            "reward_exact_promoted_cohort_count": sum(
                1
                for state in self._cohort_yield_states.values()
                if state.scope == "exact" and state.promoted
            ),
            "reward_service_promoted_cohort_count": sum(
                1
                for state in self._cohort_yield_states.values()
                if state.scope == "service" and state.promoted
            ),
            "reward_service_probation_cohort_count": sum(
                1
                for state in self._cohort_yield_states.values()
                if state.scope == "service" and state.probation
            ),
            "reward_service_discovery_cohort_count": sum(
                1
                for state in self._cohort_yield_states.values()
                if state.scope == "service" and state.discovery
            ),
            "reward_service_discovery_entry_count": (
                self._service_discovery_entry_count
            ),
            "reward_service_discovery_exit_count": (
                self._service_discovery_exit_count
            ),
            "reward_service_prior_cohort_count": len(
                self._service_prior_support_count
            ),
            "reward_service_prior_support_count": sum(
                max(0, int(value))
                for value in self._service_prior_support_count.values()
            ),
            "reward_service_probation_entry_count": (
                self._service_probation_entry_count
            ),
            "reward_service_probation_exit_count": (
                self._service_probation_exit_count
            ),
            "reward_dirty_cohort_count": len(self._dirty_cohort_keys),
            "reward_cohort_evaluation_trigger_count": (
                self._cohort_evaluation_trigger_count
            ),
            "reward_similar_promoted_cohort_count": sum(
                1
                for state in self._cohort_yield_states.values()
                if state.scope == "similar" and state.promoted
            ),
            "reward_exact_promotion_count": sum(
                state.promotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "exact"
            ),
            "reward_service_promotion_count": sum(
                state.promotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "service"
            ),
            "reward_similar_promotion_count": sum(
                state.promotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "similar"
            ),
            "reward_exact_demotion_count": sum(
                state.demotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "exact"
            ),
            "reward_service_demotion_count": sum(
                state.demotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "service"
            ),
            "reward_similar_demotion_count": sum(
                state.demotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "similar"
            ),
            "reward_exact_zero_pass_demoted_count": sum(
                state.zero_pass_demotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "exact"
            ),
            "reward_service_zero_pass_demoted_count": sum(
                state.zero_pass_demotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "service"
            ),
            "reward_similar_zero_pass_demoted_count": sum(
                state.zero_pass_demotion_count
                for state in self._cohort_yield_states.values()
                if state.scope == "similar"
            ),
            "reward_exact_sample_completion_count": cohort_scope_metrics[
                "exact"
            ]["completion_count"],
            "fast_observation_sample_count": (
                self._fast_observation_sample_count
            ),
            "fast_observation_completion_count": (
                self._fast_observation_completion_count
            ),
            "fast_observation_pass_count": self._fast_observation_pass_count,
            "fast_observation_exposure_worker_seconds": round(
                self._fast_observation_exposure_ms / 1000.0,
                3,
            ),
            "replay_fast_observation_sample_count": (
                self._replay_fast_observation_sample_count
            ),
            "replay_fast_observation_completion_count": (
                self._replay_fast_observation_completion_count
            ),
            "replay_fast_observation_pass_count": (
                self._replay_fast_observation_pass_count
            ),
            "cold_fast_observation_sample_count": (
                self._cold_fast_observation_sample_count
            ),
            "cold_fast_observation_completion_count": (
                self._cold_fast_observation_completion_count
            ),
            "cold_fast_observation_pass_count": (
                self._cold_fast_observation_pass_count
            ),
            "reward_service_sample_completion_count": cohort_scope_metrics[
                "service"
            ]["completion_count"],
            "reward_similar_sample_completion_count": cohort_scope_metrics[
                "similar"
            ]["completion_count"],
            "reward_exact_sample_pass_count": cohort_scope_metrics["exact"][
                "pass_count"
            ],
            "reward_service_sample_pass_count": cohort_scope_metrics[
                "service"
            ]["pass_count"],
            "reward_similar_sample_pass_count": cohort_scope_metrics[
                "similar"
            ]["pass_count"],
            "reward_service_top_cohorts": (
                self._top_cohort_diagnostics_unlocked("service")
            ),
            "reward_similar_top_cohorts": (
                self._top_cohort_diagnostics_unlocked("similar")
            ),
            "reward_service_level_metrics": service_level_metrics,
            "reward_service_recent_decisions": [
                dict(item) for item in self._cohort_decision_ring
            ],
            "reward_service_pending_decision_count": len(
                self._cohort_decisions_by_ticket
            ),
            "reward_exact_sample_exposure_worker_seconds": round(
                cohort_scope_metrics["exact"]["exposure_worker_ms"] / 1000.0,
                3,
            ),
            "reward_service_sample_exposure_worker_seconds": round(
                cohort_scope_metrics["service"]["exposure_worker_ms"] / 1000.0,
                3,
            ),
            "reward_similar_sample_exposure_worker_seconds": round(
                cohort_scope_metrics["similar"]["exposure_worker_ms"] / 1000.0,
                3,
            ),
            "rewarded_arm_count": rewarded_arm_count,
            "reward_handoff_credit": reward_handoff_credit,
            "reward_handoff_opportunity_limit": (
                self.reward_handoff_opportunity_limit
            ),
            # One-version compatibility alias. This is the exact TCP lease
            # count; no validation-stage FAST credit exists in the scheduler.
            "fast_credit_outstanding": supply.tcp_handoff_lease_count,
            "tcp_handoff_lease_count": supply.tcp_handoff_lease_count,
            "feedback_only_ticket_count": supply.feedback_only_ticket_count,
            "handoff_window_limit": supply.handoff_window_limit,
            "tcp_execution_limit": supply.tcp_execution_limit,
            "tcp_orphan_reserved_reclaimed_total": (
                self._tcp_orphan_reserved_reclaimed_total
            ),
            "tcp_orphan_started_closed_total": (
                self._tcp_orphan_started_closed_total
            ),
            "expected_responsive_arrivals": supply.expected_responsive_arrivals,
            "expected_redeemable_fast_arrivals": (
                supply.expected_redeemable_fast_arrivals
            ),
            "tcp_responsive_probability_lcb": (
                supply.tcp_responsive_probability_lcb
            ),
            "tcp_start_acked_sample_count": (
                supply.tcp_start_acked_sample_count
            ),
            "tcp_start_acked_responsive_rate": (
                supply.tcp_start_acked_responsive_rate
            ),
            "tcp_completed_sample_count": supply.tcp_completed_sample_count,
            "tcp_hard_censored_sample_count": (
                supply.tcp_hard_censored_sample_count
            ),
            "tcp_completed_result_rate": supply.tcp_completed_result_rate,
            "free_fast_slots": supply.free_fast_slots,
            "last_real_wave_fill": supply.last_real_wave_fill,
            "last_wave_root_coverage": supply.last_wave_root_coverage,
            "last_wave_family_coverage": supply.last_wave_family_coverage,
            "parked_deep_debt": self._parked_deep_debt,
            # One-version aliases: zero means root/family has no independent
            # capacity ceiling; only the shared TCP handoff window is bounded.
            "root_family_fast_credit_limit": 0,
            "rewarded_root_family_fast_credit_limit": 0,
            "root_family_deep_credit_floor": self.unknown_parked_deep_limit,
            "root_family_deep_credit_borrow_limit": (
                self.work_conserving_parked_deep_limit
            ),
            "root_family_deep_credit_enforced": False,
            "global_fast_credit_limit": self._global_fast_credit_limit(),
            "global_deep_credit_limit": self._global_deep_credit_limit(),
            "deep_credit_blocks_fast": False,
            "duplicate_observation_count": self._duplicate_observation_count,
            "credit_blocked_arm_count": blocked_arm_count,
            "credit_escape_overdue_bucket_count": overdue_credit_bucket_count,
            "max_credit_escape_epoch_debt": max(
                self._credit_escape_epoch_debt.values(),
                default=0,
            ),
            "credit_escape_debt_by_root_family": (
                missed_epochs_by_root_family
            ),
            "ready_arm_count": ready_arm_count,
            "normal_dispatchable_arm_count": normal_dispatchable_arm_count,
            "credit_blocked_selection_count": self._credit_blocked_selection_count,
            "active_ticket_count": len(self._tickets),
            "scheduler_contract_hash": (
                TIER3_CANDIDATE_SCHEDULER_CONTRACT_HASH
            ),
            "tcp_outcome_count": self._tcp_outcome_count,
            "fast_yield_count": self._fast_yield_count,
            "terminal_feedback_count": self._terminal_feedback_count,
            "credit_release_total": self._credit_release_total,
            "net_new_feedback_count": self._net_new_feedback_count,
            "max_arm_fairness_debt": max(
                (
                    int(arm.fairness_debt)
                    for arm_ids in self._active_arms.values()
                    for arm_id in arm_ids
                    if (arm := self._arms.get(arm_id)) is not None
                    and arm.items
                ),
                default=0,
            ),
            "saturation_state": (
                "empty"
                if self._backlog_count <= 0
                and supply.tcp_handoff_lease_count <= 0
                else "dispatchable"
                if supply.normal_reservable_count > 0
                or supply.lookahead_eligible_count > 0
                else "handoff_window_saturated"
                if self._backlog_count > 0
                and supply.tcp_handoff_lease_count
                >= max(1, supply.tcp_execution_limit)
                else "visibility_blocked"
            ),
            "learning_enabled": self.score_provider is not None,
            "learning_window": self.learning_window,
            # Opt-in TCP breadth only; no new keys at the default 1.0.
            **self._cold_l2_training_breadth_metrics_unlocked(),
        }

    def _cold_l2_training_breadth_metrics_unlocked(self) -> dict[str, Any]:
        if not self._cold_l2_training_breadth > DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY:
            return {}
        return {
            "cold_l2_training_breadth": float(self._cold_l2_training_breadth),
            "cold_l2_training_capacity": int(
                self._cold_l2_training_capacity_unlocked()
            ),
        }

    def drain_all(self) -> list[ScheduledTier3Candidate]:
        """Atomically detach queued work without creating dispatch evidence."""

        with self._lock:
            ordered_arm_ids: list[str] = []
            seen_arm_ids: set[str] = set()
            for source_root in self._root_order:
                for family in self._root_families.get(source_root, ()):
                    bucket = (source_root, family)
                    for container in (
                        self._active_arms.get(bucket, ()),
                        self._overflow_arms.get(bucket, ()),
                    ):
                        for arm_id in container:
                            if arm_id not in seen_arm_ids:
                                seen_arm_ids.add(arm_id)
                                ordered_arm_ids.append(arm_id)
            for arm_id in self._arms:
                if arm_id not in seen_arm_ids:
                    ordered_arm_ids.append(arm_id)

            drained: list[ScheduledTier3Candidate] = []
            for arm_id in ordered_arm_ids:
                arm = self._arms.get(arm_id)
                if arm is None:
                    continue
                while arm.items:
                    candidate = arm.items.popleft()
                    candidate.dispatch_ticket = None
                    candidate.node.extra.pop(
                        "tier3_candidate_dispatch_ticket",
                        None,
                    )
                    candidate.node.extra.pop(
                        "tier3_candidate_dispatch_selection_reason",
                        None,
                    )
                    drained.append(candidate)
                arm.active = False

            self._lookahead_index.clear()

            for credit in self._bucket_credits.values():
                credit.queued_count = 0
                credit.queued_credit_exempt_count = 0
                credit.ready_arm_count = 0
                credit.ready_credit_exempt_arm_count = 0

            self._backlog_count = 0
            self._active_arm_count = 0
            self._overflow_arm_count = 0
            self._active_arms.clear()
            self._overflow_arms.clear()
            self._root_families.clear()
            self._root_order.clear()
            self._active_roots.clear()
            self._root_backlog_counts.clear()
            self._cohort_queued_inventory.clear()
            self._cohort_cold_queued_inventory.clear()
            self._cohort_unknown_queued_arms.clear()
            self._cohort_tcp_only_queued_arms.clear()
            self._exact_queued_inventory.clear()
            self._cohort_queued_buckets.clear()
            self._cohort_queued_arms.clear()
            self._cohort_queued_direct_children.clear()
            self._cohort_direct_children_version += 1
            self._origin_structural_cohort_keys.clear()
            self._cold_unknown_l2_queued_inventory.clear()
            self._recount_cold_l2_visible_unlocked()
            self._credit_escape_epoch_debt.clear()
            return drained
