from __future__ import annotations

import pytest

from scripts.tier3_audit_socket_witness import classify_owned_tcp_bindings, physical_tcp_witness_valid


def test_socket_witness_ignores_external_processes_and_owned_loopback():
    sample = classify_owned_tcp_bindings([
        dict(pid=1, local="192.0.2.3", remote="203.0.113.3", state=5),
        dict(pid=2, local="198.18.0.1", remote="203.0.113.3", state=5),
        dict(pid=1, local="127.0.0.1", remote="127.0.0.1", state=5),
        dict(pid=1, local="0.0.0.0", remote="0.0.0.0", state=2),
    ], {1}, ["192.0.2.3"])
    assert sample == dict(owned_nonloopback=1, physical_bound=1, unexpected_bound=0,
                         owned_loopback=1, states={"5": 1})
    assert physical_tcp_witness_valid([sample])


def test_socket_witness_checks_ipv6_and_mapped_ipv4_on_current_physical_interface():
    sample = classify_owned_tcp_bindings([
        dict(pid=1, local="2001:db8:1::1%15", remote="2001:db8:2::1", state=3),
        dict(pid=1, local="::ffff:192.0.2.3", remote="::ffff:203.0.113.3", state=5),
    ], {1}, ["2001:db8:1::1", "192.0.2.3"])
    assert sample["physical_bound"] == 2
    assert physical_tcp_witness_valid([sample])


@pytest.mark.parametrize("local", ["198.18.0.1", "0.0.0.0", "192.0.2.9"])
def test_unexpected_or_unbound_owned_connection_invalidates_isolation(local):
    sample = classify_owned_tcp_bindings([
        dict(pid=1, local=local, remote="203.0.113.3", state=3),
    ], {1}, ["192.0.2.3"])
    assert sample["unexpected_bound"] == 1
    assert not physical_tcp_witness_valid([sample])


@pytest.mark.parametrize("samples", [[], [dict(owned_nonloopback=0, physical_bound=0)],
    [dict(error="access_denied")], [dict(owned_nonloopback=1, physical_bound=0)]])
def test_missing_empty_or_failed_socket_sample_is_not_bypass_proof(samples):
    assert not physical_tcp_witness_valid(samples)


@pytest.mark.parametrize("addresses", [[], ["0.0.0.0"], ["127.0.0.1"]])
def test_stale_or_empty_physical_interface_inventory_is_rejected(addresses):
    with pytest.raises(ValueError, match="current"):
        classify_owned_tcp_bindings([], {1}, addresses)
