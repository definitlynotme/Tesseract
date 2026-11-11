"""The new UI bridge keeps a hand-written route_lease block (lease-v1).

No bridge code changes: saving the egress_recovery section must retain an
existing ``client.egress_recovery.route_lease`` block untouched.
"""

from __future__ import annotations

from copy import deepcopy

from bridge_validation import validate_change


def test_saving_egress_recovery_retains_route_lease():
    lease = {"enabled": True, "ttl_seconds": 600, "stable_seconds": 110}
    current = {"network": {"egress_recovery": {"enabled": True, "route_lease": deepcopy(lease)}}}
    result = validate_change(
        {"section": "network", "values": {"egress_recovery": {"enabled": False}}},
        current,
    )
    assert result.valid, result.errors
    assert result.values["egress_recovery"]["route_lease"] == lease
    assert current["network"]["egress_recovery"]["route_lease"] == lease
