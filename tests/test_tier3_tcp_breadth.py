"""Opt-in TCP breadth per validator identity (tiers.tier3.tcp_breadth_per_identity).

Default 1.0 must be byte-identical to the pre-breadth code: executor layout,
PI in-flight bounds, supply-gate watermarks and the 50% refill rule.  The
reference functions below are verbatim copies of the pre-change code.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

import manager as manager_module
import tester as tester_module
from config_validation import validate_local_config
from models import Node, NodeStatus
from tier3_defaults import (
    DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY,
    TIER3_TCP_BREADTH_PER_IDENTITY_MAX,
    TIER3_TCP_TRANSPORT_CEILING,
    tier3_tcp_breadth_per_identity,
    tier3_tcp_breadth_scaled_limit,
    tier3_validation_slot_ceiling,
)
from tier3_refresh_controller import (
    TIER3_TCP_PREFETCH_RATIO_MAX,
    tier3_operational_validation_supply_watermarks,
    tier3_tcp_breadth_prefetch_ratio,
    tier3_validation_fresh_pipeline_headroom,
    tier3_validation_supply_pause_state,
)
from tier3_sources import CollectionReport


# --- verbatim pre-change reference implementations -------------------------


def _reference_refill_headroom(*, open_leases: int, inflight_limit: int) -> int:
    high_water = max(1, int(inflight_limit))
    low_water = max(0, high_water // 2)
    leased = max(0, int(open_leases))
    if leased > low_water:
        return 0
    return max(0, high_water - leased)


def _reference_supply_watermarks(
    *,
    configured_low_water: int,
    configured_high_water: int,
    operational_helpers: int,
    wave_capacity: int,
    desired_fast_slots: int | None = None,
    active_fast: int = 0,
    active_deep: int = 0,
) -> tuple[int, int]:
    configured_high = max(1, int(configured_high_water))
    wave = max(1, int(wave_capacity))
    operational = max(0, int(operational_helpers))
    if desired_fast_slots is not None:
        operational = min(
            operational,
            max(
                max(0, int(active_fast)),
                min(
                    max(0, int(desired_fast_slots)),
                    max(0, operational - max(0, int(active_deep))),
                ),
            ),
        )
    high = min(configured_high, max(wave, operational + wave))
    low = min(high, max(0, operational))
    return low, high


def _reference_session_layout(
    *,
    slot_ceiling: int,
    validator_worker_capacity: int,
    active_limit: int,
    wave_size: int,
) -> manager_module.Tier3TcpExecutorLayout:
    capacity = max(1, min(slot_ceiling, int(validator_worker_capacity or 1)))
    return manager_module.tier3_tcp_executor_layout(
        safe_capacity=capacity,
        active_limit=active_limit,
        wave_size=max(1, int(wave_size or 1)),
        ceiling=slot_ceiling,
    )


def _session_layout(
    *,
    slot_ceiling: int,
    validator_worker_capacity: int,
    active_limit: int,
    wave_size: int,
    breadth: float,
) -> manager_module.Tier3TcpExecutorLayout:
    # Mirrors refresh_tier3: identity capacity, then the breadth scaling of
    # the executor safe capacity and the layout ceiling.
    identity_capacity = max(
        1, min(slot_ceiling, int(validator_worker_capacity or 1))
    )
    return manager_module.tier3_tcp_executor_layout(
        safe_capacity=tier3_tcp_breadth_scaled_limit(identity_capacity, breadth),
        active_limit=active_limit,
        wave_size=max(1, int(wave_size or 1)),
        ceiling=tier3_tcp_breadth_scaled_limit(slot_ceiling, breadth),
    )


# --- configuration ----------------------------------------------------------


def test_breadth_setting_is_clamped_and_defaults_to_one() -> None:
    assert DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY == 1.0
    assert TIER3_TCP_BREADTH_PER_IDENTITY_MAX == 4.0
    for value, expected in (
        (None, 1.0),
        ("", 1.0),
        ("wide", 1.0),
        (True, 1.0),
        (False, 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        ([2.0], 1.0),
        (0, 1.0),
        (0.5, 1.0),
        (1, 1.0),
        (1.0, 1.0),
        (1.5, 1.5),
        (2, 2.0),
        ("2.0", 2.0),
        (" 2.5 ", 2.5),
        (4.0, 4.0),
        (9, 4.0),
    ):
        assert tier3_tcp_breadth_per_identity(value) == expected, value


def test_manager_reads_breadth_from_tier3_config(tmp_path: Path) -> None:
    from tests.test_manager import ManagerTests

    fixture = ManagerTests()
    for index, (overrides, expected) in enumerate(
        (
            ({}, 1.0),
            ({"tcp_breadth_per_identity": None}, 1.0),
            ({"tcp_breadth_per_identity": 2.0}, 2.0),
            ({"tcp_breadth_per_identity": "3"}, 3.0),
            ({"tcp_breadth_per_identity": 16}, 4.0),
            ({"tcp_breadth_per_identity": "wide"}, 1.0),
        )
    ):
        case_path = tmp_path / f"case-{index}"
        case_path.mkdir()
        owner = fixture._minimal_manager(
            case_path, tier3_enabled=True, tier3_overrides=overrides,
        )
        try:
            assert owner._tier3_tcp_breadth_per_identity() == expected, overrides
        finally:
            owner.close(timeout_seconds=3.0)


def test_config_validation_accepts_the_key() -> None:
    for value in (1.0, 2.0, 4.0, "2.0"):
        validate_local_config({"tiers": {"tier3": {"tcp_breadth_per_identity": value}}})


def test_config_example_documents_the_default() -> None:
    from tier3_defaults import tier3_validation_slots_per_process
    from tier3_tcp_dns import tier3_tcp_dns_configured_capacity

    example_path = Path(manager_module.__file__).with_name("config.example.yaml")
    text = example_path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    tier3 = payload["tiers"]["tier3"]
    # The batch profile ships as the base default: 8 identities in each of 8
    # helper processes, doubled TCP breadth, DNS pool following the TCP cap.
    assert tier3["tcp_breadth_per_identity"] == 2.0
    assert tier3_tcp_breadth_per_identity(tier3["tcp_breadth_per_identity"]) == 2.0
    assert tier3_validation_slots_per_process(tier3["validation_slots_per_process"]) == 8
    assert tier3["validation_worker_max"] == 8
    assert tier3["full_validation_process_max"] == 8
    assert tier3["validation_concurrency_max"] == 8
    assert tier3_tcp_dns_configured_capacity(tier3["tcp_dns_capacity"], tcp_cap=128) == 64
    # The code fallbacks stay single-slot; they are clamp floors too.
    assert tier3_tcp_breadth_per_identity(None) == DEFAULT_TIER3_TCP_BREADTH_PER_IDENTITY == 1.0
    assert tier3_validation_slots_per_process(None) == 1
    # The documented opt-out names the single-slot profile.
    for fragment in (
        "validation_slots_per_process: 1",
        "tcp_breadth_per_identity: 1.0",
        "validation_worker_max: 24",
        "full_validation_process_max: 24",
        "validation_concurrency_max: 16",
    ):
        assert fragment in text
    validate_local_config(payload)


# --- default byte-identity ---------------------------------------------------


def test_scaled_limit_is_identity_at_default_breadth() -> None:
    for value in (*range(0, 300), 384, 512, 1024, 4096):
        assert tier3_tcp_breadth_scaled_limit(value, 1.0) == value
        assert tier3_tcp_breadth_scaled_limit(value, 0.5) == value
        assert (
            tier3_tcp_breadth_scaled_limit(value, tier3_tcp_breadth_per_identity(None))
            == value
        )


def test_default_session_layout_is_byte_identical() -> None:
    slot_ceilings = [tier3_validation_slot_ceiling(k) for k in (1, 2, 4, 8, 16)]
    for slot_ceiling, capacity, active, wave in itertools.product(
        slot_ceilings,
        (0, 1, 3, 4, 8, 16, 24, 32, 64, 96, 128, 192, 256, 384),
        (1, 3, 4, 5, 8, 16, 17, 64, 128, 300),
        (0, 1, 3, 4),
    ):
        expected = _reference_session_layout(
            slot_ceiling=slot_ceiling,
            validator_worker_capacity=capacity,
            active_limit=active,
            wave_size=wave,
        )
        actual = _session_layout(
            slot_ceiling=slot_ceiling,
            validator_worker_capacity=capacity,
            active_limit=active,
            wave_size=wave,
            breadth=1.0,
        )
        assert actual == expected, (slot_ceiling, capacity, active, wave)


def test_default_pi_inflight_bounds_are_unchanged() -> None:
    # tcp_inflight_min/max = validation_concurrency_min/max x K, passed
    # through unchanged (even above the 256 transport ceiling) at 1.0.
    for per_process, k in itertools.product((1, 3, 4, 16, 64, 96), (1, 4, 8, 16)):
        bound = per_process * k
        assert tier3_tcp_breadth_scaled_limit(bound, 1.0) == bound


def test_default_refill_headroom_is_byte_identical() -> None:
    for limit, leased in itertools.product(
        (0, 1, 2, 3, 4, 5, 7, 8, 15, 16, 17, 32, 63, 64, 128, 256),
        range(-1, 260),
    ):
        expected = _reference_refill_headroom(open_leases=leased, inflight_limit=limit)
        assert (
            manager_module.tier3_tcp_handoff_refill_headroom(
                open_leases=leased, inflight_limit=limit,
            )
            == expected
        )
        assert (
            manager_module.tier3_tcp_handoff_refill_headroom(
                open_leases=leased, inflight_limit=limit, refill_wave=None,
            )
            == expected
        )


def test_default_supply_watermarks_are_byte_identical() -> None:
    for (
        configured_low, configured_high, operational, wave, desired, fast, deep,
    ) in itertools.product(
        (0, 1, 8, 32),
        (1, 4, 16, 64, 256),
        (0, 1, 4, 7, 16, 62, 64),
        (1, 4),
        (None, 0, 3, 12, 48),
        (0, 4, 14, 62),
        (0, 2, 8),
    ):
        kwargs = dict(
            configured_low_water=configured_low,
            configured_high_water=configured_high,
            operational_helpers=operational,
            wave_capacity=wave,
            desired_fast_slots=desired,
            active_fast=fast,
            active_deep=deep,
        )
        expected = _reference_supply_watermarks(**kwargs)
        assert tier3_operational_validation_supply_watermarks(**kwargs) == expected
        assert (
            tier3_operational_validation_supply_watermarks(
                **kwargs, tcp_prefetch_ratio=0.0,
            )
            == expected
        )


# --- b > 1 --------------------------------------------------------------------


def test_breadth_two_doubles_capacity_ceiling_and_pi_bounds() -> None:
    # 8 x 8 batch profile: 64 identities, identity ceiling 128.
    slot_ceiling = tier3_validation_slot_ceiling(8)
    assert slot_ceiling == 128
    identity_capacity = min(slot_ceiling, 8 * 8, 8 * 8)
    assert tier3_tcp_breadth_scaled_limit(identity_capacity, 2.0) == 128
    assert tier3_tcp_breadth_scaled_limit(slot_ceiling, 2.0) == 256
    assert tier3_tcp_breadth_scaled_limit(3 * 8, 2.0) == 48
    assert tier3_tcp_breadth_scaled_limit(16 * 8, 2.0) == 256
    layout = _session_layout(
        slot_ceiling=slot_ceiling,
        validator_worker_capacity=64,
        active_limit=128,
        wave_size=4,
        breadth=2.0,
    )
    assert layout.worker_count == 32
    assert layout.global_inflight_limit == 128
    assert layout.max_wave_size == 4
    baseline = _session_layout(
        slot_ceiling=slot_ceiling,
        validator_worker_capacity=64,
        active_limit=128,
        wave_size=4,
        breadth=1.0,
    )
    assert baseline.worker_count == 16
    assert baseline.global_inflight_limit == 64
    # Default single-slot profile: 16 identities -> 32 TCP at 2.0.
    single = _session_layout(
        slot_ceiling=16,
        validator_worker_capacity=24,
        active_limit=32,
        wave_size=4,
        breadth=2.0,
    )
    assert (single.worker_count, single.global_inflight_limit) == (8, 32)


def test_breadth_rounds_up_and_respects_the_transport_ceiling() -> None:
    assert TIER3_TCP_TRANSPORT_CEILING == 256
    assert tier3_tcp_breadth_scaled_limit(10, 1.1) == 11  # no float-noise slot
    assert tier3_tcp_breadth_scaled_limit(16, 1.1) == 18
    assert tier3_tcp_breadth_scaled_limit(3, 1.5) == 5
    assert tier3_tcp_breadth_scaled_limit(64, 4.0) == 256
    assert tier3_tcp_breadth_scaled_limit(256, 2.0) == 256
    # Never below the unscaled bound, even when that is above the ceiling.
    assert tier3_tcp_breadth_scaled_limit(300, 2.0) == 300
    layout = _session_layout(
        slot_ceiling=tier3_validation_slot_ceiling(16),
        validator_worker_capacity=256,
        active_limit=512,
        wave_size=4,
        breadth=2.0,
    )
    assert layout.global_inflight_limit == 256


def test_refill_uses_one_wave_headroom_only_when_requested() -> None:
    # Default: 50% hysteresis.
    assert manager_module.tier3_tcp_handoff_refill_headroom(
        open_leases=65, inflight_limit=128,
    ) == 0
    assert manager_module.tier3_tcp_handoff_refill_headroom(
        open_leases=64, inflight_limit=128,
    ) == 64
    # Breadth > 1: refill whenever one wave fits.
    for leased, expected in ((0, 128), (64, 64), (100, 28), (124, 4), (125, 0), (128, 0)):
        assert manager_module.tier3_tcp_handoff_refill_headroom(
            open_leases=leased, inflight_limit=128, refill_wave=4,
        ) == expected, leased
    # Below two waves the 50% rule still applies: never stricter than default.
    for leased, expected in ((0, 4), (1, 3), (2, 2), (3, 0), (4, 0)):
        assert manager_module.tier3_tcp_handoff_refill_headroom(
            open_leases=leased, inflight_limit=4, refill_wave=4,
        ) == expected, leased


def test_one_wave_refill_is_never_stricter_than_the_default() -> None:
    for limit, leased, wave in itertools.product(
        (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 15, 16, 32, 64, 128, 256),
        range(-1, 260),
        (1, 3, 4, 8),
    ):
        default = manager_module.tier3_tcp_handoff_refill_headroom(
            open_leases=leased, inflight_limit=limit,
        )
        widened = manager_module.tier3_tcp_handoff_refill_headroom(
            open_leases=leased, inflight_limit=limit, refill_wave=wave,
        )
        assert widened >= default, (limit, leased, wave)
        high = max(1, limit)
        low = max(high // 2, high - wave)
        assert widened == (0 if max(0, leased) > low else high - max(0, leased))


# --- supply gate --------------------------------------------------------------


def _gate_paused(
    *, currently_paused: bool, fresh_depth: int, tcp_inflight: int, ratio: float,
    low: int, high: int,
) -> bool:
    headroom = tier3_validation_fresh_pipeline_headroom(
        actor_fresh_depth=fresh_depth,
        tcp_pending_batches=0,
        tcp_active_batches=0,
        wave_capacity=4,
        high_water=high,
        responsive_reservation_ratio=ratio,
        tcp_inflight_nodes=tcp_inflight,
    )
    projected = max(0, high - max(0, int(headroom)))
    return tier3_validation_supply_pause_state(
        currently_paused=currently_paused,
        supply_depth=projected,
        low_water=low,
        high_water=high,
    )


def test_prefetch_gate_keeps_sixty_two_fast_slots_busy() -> None:
    op, r, p, latency_ratio, wave = 62, 0.27, 0.27, 0.42, 4
    ratio, p_available = tier3_tcp_breadth_prefetch_ratio(
        responsive_reservation_ratio=r,
        tcp_scanned=1000,
        tcp_responsive=270,
        tcp_timeout_seconds=latency_ratio * 3.0,
        fast_budget_seconds=3.0,
    )
    assert p_available
    assert math.isclose(ratio, (r / p) * latency_ratio)
    low, high = tier3_operational_validation_supply_watermarks(
        configured_low_water=128,
        configured_high_water=256,
        operational_helpers=op,
        wave_capacity=wave,
        tcp_prefetch_ratio=ratio,
    )
    # Corrected inequality: high >= op * (1 + (r/p) * T_s/T_L) + wave.
    assert high >= op * (1.0 + (r / p) * latency_ratio) + wave
    assert (low, high) == (89, 93)
    # Steady state that keeps all op FAST leases busy: I = op*T_s/(p*T_L).
    tcp_inflight = int(math.ceil(op * latency_ratio / p))
    for currently_paused in (False, True):
        assert not _gate_paused(
            currently_paused=currently_paused,
            fresh_depth=op,
            tcp_inflight=tcp_inflight,
            ratio=r,
            low=low,
            high=high,
        )
    # Today's gate (ratio 0) pauses that TCP and caps busy FAST near 70%.
    today_low, today_high = tier3_operational_validation_supply_watermarks(
        configured_low_water=128,
        configured_high_water=256,
        operational_helpers=op,
        wave_capacity=wave,
    )
    assert (today_low, today_high) == (62, 66)
    assert _gate_paused(
        currently_paused=False,
        fresh_depth=op,
        tcp_inflight=tcp_inflight,
        ratio=r,
        low=today_low,
        high=today_high,
    )
    sustainable = max(
        busy
        for busy in range(op + 1)
        if not _gate_paused(
            currently_paused=False,
            fresh_depth=busy,
            tcp_inflight=int(math.ceil(busy * latency_ratio / p)),
            ratio=r,
            low=today_low,
            high=today_high,
        )
    )
    assert sustainable < 0.75 * op


def test_prefetch_keeps_a_one_wave_hysteresis_band() -> None:
    for op, ratio in ((16, 0.42), (62, 0.42), (64, 2.0), (7, 0.1)):
        low, high = tier3_operational_validation_supply_watermarks(
            configured_low_water=1,
            configured_high_water=1024,
            operational_helpers=op,
            wave_capacity=4,
            tcp_prefetch_ratio=ratio,
        )
        prefetch = math.ceil(op * ratio)
        assert (low, high) == (op + prefetch, op + prefetch + 4)
    # The configured high water still bounds the prefetch; the low water then
    # sits one wave below it instead of collapsing onto it.
    assert tier3_operational_validation_supply_watermarks(
        configured_low_water=1,
        configured_high_water=70,
        operational_helpers=62,
        wave_capacity=4,
        tcp_prefetch_ratio=0.42,
    ) == (66, 70)
    # A configured high inside one wave of the FAST slots keeps the default
    # band (here the default (62, 64) at pressure 0, desired 64).
    assert tier3_operational_validation_supply_watermarks(
        configured_low_water=1,
        configured_high_water=64,
        operational_helpers=62,
        wave_capacity=4,
        tcp_prefetch_ratio=2.0,
    ) == (62, 64)
    # DEEP-borrowed slots are excluded before the prefetch is sized.
    assert tier3_operational_validation_supply_watermarks(
        configured_low_water=1,
        configured_high_water=256,
        operational_helpers=16,
        wave_capacity=4,
        desired_fast_slots=12,
        active_fast=12,
        active_deep=4,
        tcp_prefetch_ratio=0.5,
    ) == (18, 22)
    # Non-finite or non-positive ratios are today's formula.
    for bad in (float("nan"), -1.0, 0.0):
        assert tier3_operational_validation_supply_watermarks(
            configured_low_water=32,
            configured_high_water=64,
            operational_helpers=16,
            wave_capacity=4,
            tcp_prefetch_ratio=bad,
        ) == (16, 20)


def test_prefetch_watermarks_never_tighten_or_collapse_the_default_band() -> None:
    for (
        configured_high, operational, wave, desired, fast, deep, ratio,
    ) in itertools.product(
        (1, 4, 16, 64, 66, 70, 83, 91, 102, 160, 190, 256),
        (0, 1, 4, 7, 16, 62, 64),
        (1, 4),
        (None, 0, 3, 12, 48),
        (0, 4, 14, 62),
        (0, 2, 8),
        (0.1, 0.42, 1.0, 2.0),
    ):
        kwargs = dict(
            configured_low_water=1,
            configured_high_water=configured_high,
            operational_helpers=operational,
            wave_capacity=wave,
            desired_fast_slots=desired,
            active_fast=fast,
            active_deep=deep,
        )
        default_low, default_high = _reference_supply_watermarks(**kwargs)
        low, high = tier3_operational_validation_supply_watermarks(
            **kwargs, tcp_prefetch_ratio=ratio,
        )
        case = (configured_high, operational, wave, desired, fast, deep, ratio)
        assert low >= default_low and high >= default_high, case
        assert high - low >= default_high - default_low, case
        if high > low:
            # A paused gate at the high water stays paused: no flip-flop.
            assert tier3_validation_supply_pause_state(
                currently_paused=True,
                supply_depth=high,
                low_water=low,
                high_water=high,
            ), case
        if desired is None and operational > 0:
            if configured_high >= operational + wave:
                assert high - low == wave, case
            else:
                assert (low, high) == (default_low, default_high), case
    # The reviewer's sweep: op 62, desired 64, configured high
    # max(64, round(64 * (1 + 3 * pressure))), clamped ratio 2.0.
    for pressure in (0.0, 0.1, 0.14, 0.2, 0.5, 0.63, 1.0):
        configured_high = max(64, round(64 * (1 + 3 * pressure)))
        low, high = tier3_operational_validation_supply_watermarks(
            configured_low_water=1,
            configured_high_water=configured_high,
            operational_helpers=62,
            wave_capacity=4,
            tcp_prefetch_ratio=2.0,
        )
        if configured_high >= 66:
            assert high - low == 4, (pressure, low, high)
        else:
            assert (low, high) == (62, 64), (pressure, low, high)


def test_prefetch_ratio_inputs_and_clamps() -> None:
    # Before 8 outcomes p is unavailable and 1.0 is used.
    ratio, p_available = tier3_tcp_breadth_prefetch_ratio(
        responsive_reservation_ratio=1.0,
        tcp_scanned=7,
        tcp_responsive=1,
        tcp_timeout_seconds=1.25,
        fast_budget_seconds=3.0,
    )
    assert not p_available
    assert math.isclose(ratio, 1.25 / 3.0)
    # Observed zero responsiveness takes the upper clamp.
    assert tier3_tcp_breadth_prefetch_ratio(
        responsive_reservation_ratio=0.25,
        tcp_scanned=100,
        tcp_responsive=0,
        tcp_timeout_seconds=1.25,
        fast_budget_seconds=3.0,
    ) == (TIER3_TCP_PREFETCH_RATIO_MAX, True)
    # Low p with the 1/wave reservation floor clamps at 2.
    assert tier3_tcp_breadth_prefetch_ratio(
        responsive_reservation_ratio=0.25,
        tcp_scanned=1000,
        tcp_responsive=50,
        tcp_timeout_seconds=1.25,
        fast_budget_seconds=3.0,
    ) == (2.0, True)
    # Typical cold start: r slightly above p.
    ratio, _ = tier3_tcp_breadth_prefetch_ratio(
        responsive_reservation_ratio=0.2666,
        tcp_scanned=3484,
        tcp_responsive=867,
        tcp_timeout_seconds=1.25,
        fast_budget_seconds=3.0,
    )
    assert 0.44 < ratio < 0.46
    for bad in (
        dict(tcp_timeout_seconds=float("nan"), fast_budget_seconds=3.0),
        dict(tcp_timeout_seconds=1.25, fast_budget_seconds=0.0),
        dict(tcp_timeout_seconds=1.25, fast_budget_seconds=float("inf")),
    ):
        assert tier3_tcp_breadth_prefetch_ratio(
            responsive_reservation_ratio=0.3,
            tcp_scanned=100,
            tcp_responsive=30,
            **bad,
        )[0] == 0.0


# --- real refresh_tier3 session ----------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


_CIPHERS = ("aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305", "aes-192-gcm")


def _run_dead_tcp_refresh(
    tmp_path: Path,
    *,
    extra_overrides: dict[str, Any] | None = None,
    candidate_count: int = 48,
    cipher_count: int = 1,
    probe_seconds: float = 0.15,
    wait_for_helpers_seconds: float = 0.0,
) -> dict[str, Any]:
    """One real refresh_tier3 whose candidates all fail TCP after a delay.

    ``cipher_count`` spreads candidates over that many cold L2 service
    cohorts (the scheduler's cold-L2 training limit is per cohort).
    ``wait_for_helpers_seconds`` holds the source delivery until a validator
    helper is operational, so the supply gate sizes against real FAST slots.
    """

    from tests.test_manager import ManagerTests

    fixture = ManagerTests()
    fixture.setUp()
    overrides: dict[str, Any] = {
        "bootstrap_enabled": False,
        "subscription_urls": ["https://opaque.example/sub"],
        "validation_concurrency_min": 4,
        "validation_concurrency_max": 4,
    }
    overrides.update(extra_overrides or {})
    owner = fixture._minimal_manager(
        tmp_path, tier3_enabled=True, tier3_overrides=overrides,
    )
    owner.mark_tier3_core_running(effective_egress_ok=True)
    owner.set_effective_egress_state(True)
    owner.runtime_source_fetch_proxy_provider = (
        lambda _phase: "http://127.0.0.1:19101"
    )
    Path(owner.sources_file).write_text(
        "subscription_urls:\n  - https://opaque.example/sub\n"
        "github_repos: []\ntelegram_channels: []\nlocal_files: []\n",
        encoding="utf-8",
    )
    ciphers = _CIPHERS[: max(1, min(len(_CIPHERS), cipher_count))]
    candidates = [
        Node(name=f"breadth-{index}", protocol="ss",
             address=f"opaque-{index}.invalid", port=443,
             method=ciphers[index % len(ciphers)],
             password="fixture", tier="tier3",
             source="https://opaque.example/sub")
        for index in range(candidate_count)
    ]
    lock = threading.Lock()
    state: dict[str, Any] = {
        "inflight": 0, "peak": 0, "probed": 0, "helpers_ready": None,
    }
    threads: set[str] = set()
    all_probed = threading.Event()
    errors: list[BaseException] = []
    capture = _Capture()
    owner.logger.addHandler(capture)
    previous_level = owner.logger.level
    owner.logger.setLevel(logging.INFO)

    async def collect(*_args, **kwargs):
        if wait_for_helpers_seconds > 0:
            deadline = time.monotonic() + wait_for_helpers_seconds
            state["helpers_ready"] = False
            while time.monotonic() < deadline:
                progress = owner.tier3_last_collection_report or {}
                if int(
                    progress.get("source_stream_process_active_helper_count", 0)
                    or 0
                ) > 0:
                    state["helpers_ready"] = True
                    break
                await asyncio.sleep(0.1)
        report = CollectionReport(
            source_count=1, fetched_sources=1, node_count=len(candidates),
            sources=[{"type": "subscription_url",
                      "value": "https://opaque.example/sub", "ok": True}],
        )
        kwargs["source_observer"](candidates, report)
        return candidates, report

    async def probe(node, **_kwargs):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            threads.add(threading.current_thread().name)
        try:
            await asyncio.sleep(probe_seconds)
        finally:
            with lock:
                state["inflight"] -= 1
                state["probed"] += 1
                if state["probed"] >= len(candidates):
                    all_probed.set()
        updated = node.clone()
        updated.status = NodeStatus.DEAD
        updated.suspicious_reason = "tcp connection refused"
        return updated

    # Record how refresh_tier3 calls the supply gate and the refill rule, so
    # the tests pin the wiring and not only the pure functions.
    watermark_calls: list[tuple[dict[str, Any], tuple[int, int]]] = []
    refill_calls: list[dict[str, Any]] = []
    real_watermarks = manager_module.tier3_operational_validation_supply_watermarks
    real_refill = manager_module.tier3_tcp_handoff_refill_headroom

    def recording_watermarks(**kwargs):
        result = real_watermarks(**kwargs)
        watermark_calls.append((dict(kwargs), result))
        return result

    def recording_refill(**kwargs):
        refill_calls.append(dict(kwargs))
        return real_refill(**kwargs)

    def refresh() -> None:
        try:
            owner.refresh_tier3(
                suppress_intervention_alert=True, startup_mode=False,
            )
        except BaseException as exc:
            errors.append(exc)

    try:
        with patch.object(
            manager_module, "collect_nodes_from_sources_with_report", collect,
        ), patch.object(tester_module, "_tcp_probe", probe), patch.object(
            manager_module,
            "tier3_operational_validation_supply_watermarks",
            recording_watermarks,
        ), patch.object(
            manager_module, "tier3_tcp_handoff_refill_headroom", recording_refill,
        ):
            worker = threading.Thread(target=refresh)
            worker.start()
            try:
                assert all_probed.wait(30 + wait_for_helpers_seconds), (
                    f"probed {state['probed']}"
                )
                # Let a progress report carry the final counters.
                time.sleep(0.5)
            finally:
                owner.request_stop()
                worker.join(15)
        assert not worker.is_alive()
        assert not errors
        report = dict(owner.tier3_last_collection_report or {})
        return {
            "peak": state["peak"],
            "helpers_ready": state["helpers_ready"],
            "threads": set(threads),
            "report": report,
            "messages": list(capture.messages),
            "watermark_calls": list(watermark_calls),
            "refill_calls": list(refill_calls),
        }
    finally:
        owner.logger.removeHandler(capture)
        owner.logger.setLevel(previous_level)
        owner.close(timeout_seconds=3.0)
        fixture.doCleanups()


def _breadth_lines(messages: list[str]) -> list[str]:
    return [
        message for message in messages
        if message.startswith("Bastion Layer TCP breadth session policy")
    ]


def _assert_default_gate_wiring(observed: dict[str, Any]) -> None:
    """At 1.0 the session passes no prefetch and no one-wave refill."""

    assert observed["watermark_calls"]
    for kwargs, result in observed["watermark_calls"]:
        assert kwargs["tcp_prefetch_ratio"] == 0.0, kwargs
        reference_kwargs = {
            key: value for key, value in kwargs.items()
            if key != "tcp_prefetch_ratio"
        }
        assert result == _reference_supply_watermarks(**reference_kwargs)
    assert observed["refill_calls"]
    assert all(call["refill_wave"] is None for call in observed["refill_calls"])


def _assert_breadth_gate_wiring(observed: dict[str, Any]) -> None:
    """Above 1.0 the session passes the prefetch and the one-wave refill."""

    assert observed["watermark_calls"]
    assert any(
        kwargs["tcp_prefetch_ratio"] > 0.0
        for kwargs, _result in observed["watermark_calls"]
    )
    waves = {kwargs["wave_capacity"] for kwargs, _ in observed["watermark_calls"]}
    assert observed["refill_calls"]
    assert {call["refill_wave"] for call in observed["refill_calls"]} == waves


def test_default_session_keeps_identity_tcp_cap(tmp_path: Path) -> None:
    observed = _run_dead_tcp_refresh(tmp_path)

    # 16 identities, concurrency 4: 4 collector threads, 4 in flight.
    assert observed["peak"] <= 4
    assert len(observed["threads"]) <= 4
    assert _breadth_lines(observed["messages"]) == []
    report = observed["report"]
    assert not [
        key for key in report if "tcp_breadth" in key or "tcp_prefetch" in key
    ]
    assert report["source_stream_tcp_executor_worker_count"] == 4
    assert report["source_stream_tcp_executor_global_inflight_limit"] == 4
    _assert_default_gate_wiring(observed)


def test_breadth_two_session_scales_tcp_executor_and_logs_once(
    tmp_path: Path,
) -> None:
    observed = _run_dead_tcp_refresh(
        tmp_path, extra_overrides={"tcp_breadth_per_identity": 2.0},
    )

    assert observed["peak"] <= 8
    assert len(observed["threads"]) <= 8
    lines = _breadth_lines(observed["messages"])
    assert len(lines) == 1, lines
    line = lines[0]
    for fragment in (
        "tcp_breadth_per_identity=2.00",
        "effective_breadth=2.00",
        "tcp_cap=32",
        "identity_tcp_cap=16",
        "tcp_layout_ceiling=32",
        "tcp_inflight_min=8",
        "tcp_inflight_max=8",
        "transport_ceiling=256",
        "collector_threads=8",
        "prefetch_ratio_start=0.333",
        "prefetch_ratio_max=2.00",
        "refill=one_wave",
    ):
        assert fragment in line, (fragment, line)
    report = observed["report"]
    assert report["source_stream_tcp_executor_worker_count"] == 8
    # PI in-flight bounds 4 -> 8: one slot on each of the 8 workers.
    assert report["source_stream_tcp_executor_global_inflight_limit"] == 8
    assert report["source_stream_tcp_executor_slot_vector"] == [1] * 8
    assert report["source_stream_tcp_breadth_per_identity"] == 2.0
    assert report["source_stream_tcp_breadth_tcp_cap"] == 32
    assert report["source_stream_tcp_breadth_identity_tcp_cap"] == 16
    assert report["source_stream_tcp_breadth_layout_ceiling"] == 32
    assert report["source_stream_tcp_prefetch_evaluation_count"] > 0
    assert 0.0 < report["source_stream_tcp_prefetch_ratio_max"] <= 2.0
    _assert_breadth_gate_wiring(observed)


def test_breadth_two_runs_more_tcp_than_identities(tmp_path: Path) -> None:
    # Four cold L2 cohorts, so the scheduler's per-cohort cold-L2 training
    # limit (4 per cohort at 4 helpers) does not cap TCP at the helper count.
    runs = {}
    for breadth in (1.0, 2.0):
        case_path = tmp_path / f"breadth-{breadth}"
        case_path.mkdir()
        runs[breadth] = _run_dead_tcp_refresh(
            case_path,
            extra_overrides={"tcp_breadth_per_identity": breadth},
            candidate_count=96,
            cipher_count=4,
            probe_seconds=0.3,
            wait_for_helpers_seconds=45.0,
        )
        if not runs[breadth]["helpers_ready"]:
            pytest.skip("validator helpers did not become operational in 45 s")
    assert runs[1.0]["peak"] <= 4
    assert 4 < runs[2.0]["peak"] <= 8, runs[2.0]["peak"]
    _assert_default_gate_wiring(runs[1.0])
    _assert_breadth_gate_wiring(runs[2.0])
    # With helpers operational the prefetch actually raises the gate above
    # the default formula for the same inputs.
    raised = [
        (kwargs, result)
        for kwargs, result in runs[2.0]["watermark_calls"]
        if kwargs["operational_helpers"] > 0
        and result[1]
        > _reference_supply_watermarks(
            **{
                key: value for key, value in kwargs.items()
                if key != "tcp_prefetch_ratio"
            }
        )[1]
    ]
    assert raised, runs[2.0]["watermark_calls"][-5:]


def test_breadth_two_widens_the_cold_l2_training_limit_for_one_cohort(
    tmp_path: Path,
) -> None:
    # One cold L2 cohort, so the scheduler's cold-L2 training limit is the
    # whole cold budget. Unscaled it equals the identity count (4 here) and
    # held a b=2 session at the b=1 in-flight level; scaled, the one cohort
    # may fill the widened pipeline of 8 TCP leases.
    from tier3_candidate_scheduler import Tier3CandidateScheduler

    def run(case: str, breadth: float) -> dict[str, Any]:
        case_path = tmp_path / case
        case_path.mkdir()
        return _run_dead_tcp_refresh(
            case_path,
            extra_overrides={"tcp_breadth_per_identity": breadth},
            candidate_count=96,
            cipher_count=1,
            probe_seconds=0.3,
        )

    default = run("default", 1.0)
    # The report builders read these fields from the quick_metrics(0.25 s)
    # memo they already fetched, which is lock-free while fresh, so a
    # progress publish never waits on the scheduler lock for them.  Record
    # any call to the locked snapshot during the session.
    snapshot_calls: list[str] = []
    real_snapshot = Tier3CandidateScheduler.cold_l2_training_snapshot

    def recording_snapshot(scheduler: Tier3CandidateScheduler) -> dict[str, Any]:
        snapshot_calls.append(threading.current_thread().name)
        return real_snapshot(scheduler)

    with patch.object(
        Tier3CandidateScheduler,
        "cold_l2_training_snapshot",
        recording_snapshot,
    ):
        widened = run("breadth-2", 2.0)
    assert snapshot_calls == [], sorted(set(snapshot_calls))
    # Control: the same b=2 session with the scheduler left unscaled, as it
    # was before; only the cold-L2 training limit differs from ``widened``.
    with patch.object(
        Tier3CandidateScheduler,
        "set_cold_l2_training_breadth",
        lambda _scheduler, _breadth: None,
    ):
        unscaled = run("breadth-2-unscaled-scheduler", 2.0)

    assert default["peak"] <= 4
    assert not [key for key in default["report"] if "cold_l2" in key and "breadth" in key]

    tcp_cap = widened["report"]["source_stream_tcp_executor_global_inflight_limit"]
    assert tcp_cap == 8
    cold_l2 = {
        key.removeprefix("source_stream_tcp_breadth_cold_l2_"): value
        for key, value in widened["report"].items()
        if key.startswith("source_stream_tcp_breadth_cold_l2_")
    }
    assert set(cold_l2) == {
        "capacity",
        "current_limit",
        "cohort_count",
        "outstanding_count",
        "outstanding_high_water",
        "deferred_total",
        "borrowed_total",
    }, cold_l2
    # The end-of-session limit follows the envelope as it winds down, so the
    # high-water mark is the evidence: the scheduler handed the one cohort
    # the whole widened pipeline ...
    assert cold_l2["outstanding_high_water"] == tcp_cap, cold_l2
    # ... and the executor ran more TCP than there are identities.
    assert 4 < widened["peak"] <= tcp_cap, (widened["peak"], cold_l2)
    line = _breadth_lines(widened["messages"])[0]
    assert "cold_l2_training_capacity=ceil(helpers*breadth)" in line

    unscaled_report = unscaled["report"]
    assert unscaled_report["source_stream_tcp_executor_global_inflight_limit"] == 8
    assert unscaled_report["source_stream_tcp_breadth_cold_l2_deferred_total"] > 0
    assert (
        unscaled_report["source_stream_tcp_breadth_cold_l2_outstanding_high_water"]
        == 4
    )
    assert unscaled["peak"] <= 4
