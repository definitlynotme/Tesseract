from __future__ import annotations

import math

DEFAULT_TIER3_MIN_ALIVE_NODES = 3
DEFAULT_TIER3_MAX_POOL_SIZE = 100
DEFAULT_TIER3_TARGET_ALIVE = 25
DEFAULT_TIER3_TARGET_RESERVE_NODES = (
    DEFAULT_TIER3_TARGET_ALIVE - DEFAULT_TIER3_MIN_ALIVE_NODES
)
DEFAULT_TIER3_TARGET_FLOOR_MULTIPLIER = 2
DEFAULT_TIER3_MIN_COLLECT_INTERVAL = 120
DEFAULT_TIER3_MAX_COLLECT_INTERVAL = 3600
DEFAULT_TIER3_CACHE_REVIVAL_DAYS = 7
DEFAULT_TIER3_VALIDATED_REPLAY_DAYS = 30
DEFAULT_TIER3_OBSERVATION_HISTORY_LIMIT = 5000
DEFAULT_TIER3_OBSERVATION_REVIVE_COOLDOWN_HOURS = 24.0
DEFAULT_TIER3_VALIDATION_CANDIDATE_LIMIT = 24
DEFAULT_TIER3_VALIDATION_EMPTY_RETRY_LIMIT = 96
DEFAULT_TIER3_VALIDATION_BATCH_SIZE = 12
DEFAULT_TIER3_INCREMENTAL_VALIDATION_BATCH_SIZE = 4
DEFAULT_TIER3_VALIDATION_CONCURRENCY_MIN = 3
DEFAULT_TIER3_VALIDATION_CONCURRENCY_MAX = 16
DEFAULT_TIER3_VALIDATION_WORKER_MIN = 1
DEFAULT_TIER3_VALIDATION_WORKER_MAX = 24
DEFAULT_TIER3_FULL_VALIDATION_PROCESS_MAX = 24
DEFAULT_TIER3_FULL_VALIDATION_CONCURRENCY_MAX = 96
DEFAULT_TIER3_FULL_VALIDATION_TIMEOUT_FLOOR = 5.0
DEFAULT_TIER3_FULL_VALIDATION_PROCESS_BATCH_SIZE = 16
DEFAULT_TIER3_SOURCE_STREAM_WINDOW_MIN = 8
DEFAULT_TIER3_SOURCE_STREAM_NO_GROWTH_TAIL_SECONDS = 120.0
DEFAULT_TIER3_VALIDATION_BUDGET_MULTIPLIER = 4.0
DEFAULT_TIER3_VALIDATION_SESSION_WAVE_MAX = 5
DEFAULT_TIER3_CATCHUP_COLLECT_INTERVAL_MIN = 1
DEFAULT_TIER3_STARTUP_EXPORT_CANDIDATE_LIMIT = 36
DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY = 8
DEFAULT_TIER3_UNDERFILLED_VALIDATION_EMPTY_RETRY_LIMIT = 96
DEFAULT_TIER3_UNDERFILLED_VALIDATION_BATCH_SIZE = 12
DEFAULT_TIER3_EMPTY_REFRESH_REPLACE_AFTER_FAILURES = 3
DEFAULT_TIER3_NEGATIVE_CACHE_COOLDOWN_HOURS = 6.0
DEFAULT_TIER3_POOL_EVICTION_FAILURE_THRESHOLD = 3

# Capacity is a validation contract, not a function of the host CPU count.
TIER3_AUDITED_PROCESS_WORKER_CAP = 16
TIER3_PROCESS_WORKER_HARD_CAP = 16


def tier3_process_worker_ceiling(cpu_count: int | None = None) -> int:
    """Compatibility entry point for the audited, host-independent ceiling."""
    del cpu_count
    return TIER3_PROCESS_WORKER_HARD_CAP


# Batch validation backend (tiers.tier3.validation_slots_per_process > 1): one
# helper process hosts that many worker identities, each with its own Mihomo
# listener.  The process ceiling above is unchanged; identities scale with it.
# The shipped default (8 slots in each of 8 processes) lives in
# config.example.yaml.  This 1 is only the fallback for a missing or unusable
# value and the clamp floor, and 1 is also what code treats as "single-slot
# path, unchanged" -- so keep it at 1.
DEFAULT_TIER3_VALIDATION_SLOTS_PER_PROCESS = 1
TIER3_VALIDATION_SLOTS_PER_PROCESS_MAX = 16


def tier3_validation_slots_per_process(value: int | None) -> int:
    try:
        slots = int(value if value is not None else DEFAULT_TIER3_VALIDATION_SLOTS_PER_PROCESS)
    except (TypeError, ValueError):
        slots = DEFAULT_TIER3_VALIDATION_SLOTS_PER_PROCESS
    return max(1, min(TIER3_VALIDATION_SLOTS_PER_PROCESS_MAX, slots))


def tier3_validation_slot_ceiling(slots_per_process: int | None = None) -> int:
    """Identity ceiling: processes ceiling times identities per process."""
    return tier3_process_worker_ceiling() * tier3_validation_slots_per_process(
        slots_per_process
    )


# TCP prefilter breadth (tiers.tier3.tcp_breadth_per_identity): TCP in-flight
# capacity per validator identity.  1.0 keeps TCP breadth equal to the
# identity count.  Above 1.0 the TCP executor capacity, its layout ceiling and
# the PI in-flight bounds are multiplied by the factor (rounded up), never
# beyond the transport ceiling.  The shipped default (2.0) lives in
# config.example.yaml; this 1.0 is the fallback for a missing or unusable
# value, the clamp floor and the "unchanged" marker of the scaled limits.
DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY = 1.0
TIER3_TCP_BREADTH_PER_IDENTITY_MAX = 4.0
TIER3_TCP_TRANSPORT_CEILING = 256


def tier3_tcp_breadth_per_identity(value: object) -> float:
    """Clamp the configured breadth to [1.0, 4.0]; anything unusable is 1.0."""

    if value is None or isinstance(value, bool):
        return DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY
    try:
        breadth = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY
    if not math.isfinite(breadth):
        return DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY
    return max(
        DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY,
        min(TIER3_TCP_BREADTH_PER_IDENTITY_MAX, breadth),
    )


def tier3_tcp_breadth_scaled_limit(limit: int, breadth: float) -> int:
    """Scale one TCP bound by the breadth factor.

    At breadth 1.0 (the default) the bound is returned unchanged.  Above it the
    bound is ``ceil(limit * breadth)`` capped by the transport ceiling, and
    never lower than the unscaled bound.
    """

    base = int(limit)
    if not float(breadth) > DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY:
        return base
    # round() first so float noise (10 * 1.1 = 11.000000000000002) does not
    # add a slot.
    scaled = int(math.ceil(round(base * float(breadth), 6)))
    return max(base, min(TIER3_TCP_TRANSPORT_CEILING, scaled))
