"""Identity-ceiling arithmetic for the batch backend, and N == 1 equivalence.

The manager multiplies every per-process bound by ``validation_slots_per_process``;
a mistake there already produced an empty A/B arm once.  These pin the pure
functions and the domain's backend selection so a regression is caught by a
unit test instead of a harness run.
"""
from __future__ import annotations

from manager import tier3_tcp_executor_layout
from tier3_defaults import (
    TIER3_PROCESS_WORKER_HARD_CAP,
    TIER3_VALIDATION_SLOTS_PER_PROCESS_MAX,
    tier3_process_worker_ceiling,
    tier3_validation_slot_ceiling,
    tier3_validation_slots_per_process,
)


def test_slots_per_process_is_clamped_and_defaults_to_one() -> None:
    assert tier3_validation_slots_per_process(None) == 1
    assert tier3_validation_slots_per_process("") == 1
    assert tier3_validation_slots_per_process("garbage") == 1
    assert tier3_validation_slots_per_process(0) == 1
    assert tier3_validation_slots_per_process(4) == 4
    assert tier3_validation_slots_per_process(99) == TIER3_VALIDATION_SLOTS_PER_PROCESS_MAX


def test_identity_ceiling_is_process_ceiling_times_slots_and_process_ceiling_is_untouched() -> None:
    assert tier3_process_worker_ceiling() == TIER3_PROCESS_WORKER_HARD_CAP == 16
    assert tier3_validation_slot_ceiling(None) == 16
    assert tier3_validation_slot_ceiling(1) == 16
    assert tier3_validation_slot_ceiling(4) == 64
    assert tier3_validation_slot_ceiling(16) == 256


def test_tcp_executor_layout_default_ceiling_is_byte_equivalent_to_the_audited_cap() -> None:
    default = tier3_tcp_executor_layout(safe_capacity=64, active_limit=64, wave_size=4)
    explicit = tier3_tcp_executor_layout(safe_capacity=64, active_limit=64, wave_size=4, ceiling=16)
    assert default == explicit
    assert default.global_inflight_limit == 16
    assert default.worker_count == 4


def test_tcp_executor_layout_scales_with_the_identity_ceiling() -> None:
    wide = tier3_tcp_executor_layout(safe_capacity=64, active_limit=64, wave_size=4, ceiling=64)
    assert wide.global_inflight_limit == 64
    assert wide.worker_count == 16
    assert max(wide.worker_slot_vector) == 4
    # The PI-owned active limit still bounds in-flight below the ceiling.
    partial = tier3_tcp_executor_layout(safe_capacity=64, active_limit=20, wave_size=4, ceiling=64)
    assert partial.global_inflight_limit == 20
