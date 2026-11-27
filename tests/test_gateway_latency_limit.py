"""Chain measurements have their own latency ceiling (testing.gateway_max_latency_ms).

The limit follows how a node was measured, not its tier: a chain scope
(``gateway_chain``: a Gateway through the fallback layers, or a Tier1/Tier2 node
through the selected Gateway) uses the chain limit; a direct one the primary.
"""

from __future__ import annotations

import pytest

from bridge_validation import validate_field
from config_validation import validate_testing_local
from latency_policy_view import latency_policy_form_state, validate_gateway_latency_policy_max_latency_ms
from models import Node, NodeStatus
from runtime_settings import (
    DEFAULT_GATEWAY_MAX_LATENCY_MS,
    gateway_chain_probe_timeout_ms,
    gateway_max_latency_ms_from_testing,
    strict_latency_limit_ms_for_path,
    strict_latency_limit_ms_for_scope,
    validate_gateway_max_latency_ms,
)
from tester import apply_success_latency_policy


def _node(tier: str) -> Node:
    return Node(name=f"n-{tier}", protocol="socks5", address="h.example", port=1080, tier=tier)


def test_missing_key_defaults_to_6000_and_ignores_a_customized_primary_limit() -> None:
    assert DEFAULT_GATEWAY_MAX_LATENCY_MS == 6000
    testing = {"primary_gateway_max_latency_ms": 2700}
    assert gateway_max_latency_ms_from_testing(testing) == 6000
    assert gateway_max_latency_ms_from_testing(None) == 6000
    assert strict_latency_limit_ms_for_path(testing, "chain") == 6000
    assert strict_latency_limit_ms_for_path(testing, "direct") == 2700
    assert strict_latency_limit_ms_for_path(testing, None) == 2700
    assert strict_latency_limit_ms_for_scope(testing, "gateway_chain") == 6000
    assert strict_latency_limit_ms_for_scope(testing, "manual_chain") == 6000
    for scope in ("tcp", "mihomo_direct", "xray_direct", "manual_direct", ""):
        assert strict_latency_limit_ms_for_scope(testing, scope) == 2700


@pytest.mark.parametrize("value", [499, 15001, True, "6000", 6000.0, None])
def test_gateway_limit_validation_is_strict(value) -> None:
    with pytest.raises(ValueError):
        validate_gateway_max_latency_ms(value)
    assert gateway_max_latency_ms_from_testing({"gateway_max_latency_ms": value}) == 6000


@pytest.mark.parametrize("value", [500, 6000, 15000])
def test_gateway_limit_bounds_are_inclusive(value) -> None:
    assert validate_gateway_max_latency_ms(value) == value
    validate_testing_local({"gateway_max_latency_ms": value})


def test_config_validation_rejects_an_out_of_range_gateway_limit() -> None:
    with pytest.raises(ValueError):
        validate_testing_local({"gateway_max_latency_ms": 20000})


TESTING = {"primary_gateway_max_latency_ms": 800, "gateway_max_latency_ms": 4000}


@pytest.mark.parametrize("tier", ["tier1", "tier2", "exit"])
def test_direct_checks_of_every_strict_tier_keep_the_primary_limit(tier) -> None:
    assert apply_success_latency_policy(_node(tier), 400.0, TESTING, "tcp").status == NodeStatus.ALIVE
    assert apply_success_latency_policy(_node(tier), 500.0, TESTING, "mihomo_direct").status == NodeStatus.SLOW
    dead = apply_success_latency_policy(_node(tier), 900.0, TESTING, "xray_direct")
    assert dead.status == NodeStatus.DEAD
    assert dead.extra["latency_policy_limit_ms"] == 800
    assert "latency_policy_path" not in dead.extra  # absent = direct


@pytest.mark.parametrize("tier", ["tier1", "tier2", "exit"])
def test_chain_checks_use_the_chain_limit_and_half_as_slow(tier) -> None:
    assert apply_success_latency_policy(_node(tier), 1900.0, TESTING, "gateway_chain").status == NodeStatus.ALIVE
    slow = apply_success_latency_policy(_node(tier), 2100.0, TESTING, "gateway_chain")
    assert slow.status == NodeStatus.SLOW
    assert slow.extra["latency_policy_path"] == "chain"
    dead = apply_success_latency_policy(_node(tier), 4100.0, TESTING, "gateway_chain")
    assert dead.status == NodeStatus.DEAD
    assert dead.extra["latency_policy_limit_ms"] == 4000


def test_sentinel_through_the_gateway_versus_a_direct_sentinel_check() -> None:
    # The 11-25..27 case: every Sentinel measured through the selected Gateway
    # came in over the primary limit and the tier went offline.
    sentinel = _node("tier2")
    through_gateway = apply_success_latency_policy(sentinel, 1500.0, TESTING, "gateway_chain")
    assert through_gateway.status == NodeStatus.ALIVE
    direct = apply_success_latency_policy(sentinel, 1500.0, TESTING, "mihomo_direct")
    assert direct.status == NodeStatus.DEAD
    assert direct.suspicious_reason == "latency_limit_exceeded"


def test_tester_gateway_chain_validation_reports_the_chain_scope() -> None:
    import inspect

    import tester

    source = inspect.getsource(tester._gateway_chain_validate)
    assert '"gateway_chain"' in source


def test_chain_probe_timeout_covers_the_gateway_limit() -> None:
    assert gateway_chain_probe_timeout_ms({"testing": {}}) == 8000  # default probe, 6000 + headroom < 8000
    assert gateway_chain_probe_timeout_ms({"testing": {"gateway_max_latency_ms": 15000}}) == 16000
    assert gateway_chain_probe_timeout_ms({"testing": {"gateway_max_latency_ms": 9000}}) == 10000


def test_bridge_validation_accepts_and_bounds_the_gateway_field() -> None:
    assert validate_gateway_latency_policy_max_latency_ms("7000") == 7000
    for bad in (499, 15001, True, "abc", 1.5):
        with pytest.raises(ValueError):
            validate_gateway_latency_policy_max_latency_ms(bad)


def test_form_state_reports_both_limits() -> None:
    state = latency_policy_form_state({"testing": {"primary_gateway_max_latency_ms": 2700}}, {})
    assert state.max_latency_ms == 2700
    assert state.gateway_max_latency_ms == 6000


@pytest.mark.parametrize("value", [499, 15001, True, "x"])
def test_settings_validate_refuses_a_bad_gateway_limit(value) -> None:
    result = validate_field("latency", "gateway_max_latency_ms", value)
    assert result["valid"] is False


def test_settings_validate_accepts_a_gateway_limit_above_the_primary_range() -> None:
    result = validate_field("latency", "gateway_max_latency_ms", 9000)
    assert result["valid"] is True
    assert validate_field("latency", "primary_gateway_max_latency_ms", 9000)["valid"] is False


def test_snapshot_projects_the_effective_gateway_limit() -> None:
    from bridge_runtime_access import latency_values

    def fields(block, schema, path):
        return {k: block[k] for k in schema if isinstance(block, dict) and k in block}

    assert latency_values({"primary_gateway_max_latency_ms": 2700}, fields) == {
        "primary_gateway_max_latency_ms": 2700, "gateway_max_latency_ms": 6000}
    assert latency_values({"gateway_max_latency_ms": 9000}, fields)["gateway_max_latency_ms"] == 9000
