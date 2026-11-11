from __future__ import annotations

import pytest

from overview_ip_check import collect_exit_identity
from tests.test_exit_identity import _UrlOpener, _json_payload


PROVIDERS = (
    ("ipify", 4, "https://api.ipify.org?format=json"),
    ("ident.me", 4, "https://4.ident.me/json"),
    ("ipify", 6, "https://api6.ipify.org?format=json"),
    ("ident.me", 6, "https://6.ident.me/json"),
)


@pytest.mark.parametrize("second_ip", ["8.8.8.8", "1.1.1.1"])
def test_conflicting_profile_observations_are_not_presented_as_agreed(second_ip):
    payloads = {
        PROVIDERS[1][2]: [
            _json_payload("1.1.1.1", cc="US", city="Los Angeles", asn=13335, aso="Network A"),
            _json_payload(second_ip, cc="JP", city="Tokyo", asn=15169, aso="Network B"),
        ],
    }
    family = collect_exit_identity(
        opener=_UrlOpener(payloads), providers=(PROVIDERS[1],), rounds=2,
    )["identity"]["ipv4"]
    if second_ip != "1.1.1.1":
        assert family["state"] == "rotating"
        assert family["ip"] is None
        assert "location" not in family
        assert "network" not in family
    else:
        assert family["location"]["state"] == "conflict"
        assert family["location"]["conflicting_fields"] == ["country_code", "city"]
        assert family["network"]["source_conflict"]
        assert "asn" not in family["network"]
        assert "organisation" not in family["network"]

