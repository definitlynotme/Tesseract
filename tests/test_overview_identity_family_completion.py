"""Overview results need not wait out silent supplemental address families."""
from __future__ import annotations

import copy
import json
import threading
import time
from urllib.parse import urlsplit

import pytest

from overview_ip_check import collect_exit_identity
from tests.test_exit_identity import _Response


PROVIDERS = (
    ("ipify", 4, "https://ipify.test/v4"),
    ("ident.me", 4, "https://ident.test/v4"),
    ("ipify", 6, "https://ipify.test/v6"),
    ("ident.me", 6, "https://ident.test/v6"),
)
IPS = {4: "1.1.1.1", 6: "2606:4700:4700::1111"}


@pytest.fixture(autouse=True)
def finish_identity_workers():
    existing = set(threading.enumerate())
    yield
    workers = [t for t in threading.enumerate()
               if t not in existing and t.name.startswith("exit-identity")]
    for worker in workers:
        worker.join(1)
    assert not [worker.name for worker in workers if worker.is_alive()]


def _collect(handler, *, grace=0.05, budget=0.6):
    class Opener:
        def open(self, request, timeout):
            parsed = urlsplit(request.full_url)
            family = int(parsed.path[-1])
            result = handler(parsed.hostname, family)
            return _Response(json.dumps(result).encode())

    return collect_exit_identity(
        providers=PROVIDERS, opener_factory=lambda _: Opener(),
        rounds=1, total_timeout_seconds=budget,
        completed_family_grace_seconds=grace,
    )


@pytest.mark.parametrize("fast_family", [4, 6])
def test_complete_family_returns_before_silent_other_and_late_work_cannot_replace_it(fast_family):
    release = threading.Event()

    def handler(host, family):
        if family != fast_family:
            assert release.wait(2)
        return {"ip": IPS[family], "asn": 13335, "city": "Test City"}

    started = time.monotonic()
    try:
        result = _collect(handler)
        assert time.monotonic() - started < 0.4
        ready = result["identity"][f"ipv{fast_family}"]
        assert ready["state"] == "confirmed"
        assert ready["ip"] == IPS[fast_family]
        assert ready["location"]["city"] == "Test City"
        assert ready["network"]["asn"] == "13335"
        slow = result["identity"][f"ipv{10 - fast_family}"]
        assert slow["state"] == "unavailable"
        assert slow["ip"] is None
        assert all("silent address-family" in item["attempts"][0]["error"]
                   for item in slow["evidence"])
        assert result["collection"]["completion_reason"] == "completed_family"
        snapshot = copy.deepcopy(result)
    finally:
        release.set()
    assert result == snapshot


def test_slow_profile_in_same_family_is_collected_before_shortening_other_family():
    release = threading.Event()

    def handler(host, family):
        if family == 6:
            assert release.wait(2)
        if host == "ident.test" and family == 4:
            time.sleep(0.15)
        return {"ip": IPS[family], "asn": 13335, "city": "Test City"}

    try:
        started = time.perf_counter()
        result = _collect(handler)
        # Windows monotonic reports use a 15.625ms tick on Python 3.12.
        # Measure the wait independently without weakening the 150ms contract.
        assert time.perf_counter() - started >= 0.15
        assert result["identity"]["ipv4"]["state"] == "confirmed"
        assert result["identity"]["ipv4"]["location"]["city"] == "Test City"
        assert result["collection"]["completion_reason"] == "completed_family"
    finally:
        release.set()


def test_dual_stack_finishing_within_grace_keeps_both_families():
    def handler(host, family):
        if family == 6:
            time.sleep(0.03)
        return {"ip": IPS[family]}

    result = _collect(handler, grace=0.2)
    assert result["collection"]["completion_reason"] == "complete"
    assert all(result["identity"][key]["state"] == "confirmed" for key in ("ipv4", "ipv6"))


def test_first_secondary_ip_keeps_budget_for_later_conflicting_source():
    def handler(host, family):
        if family == 6 and host == "ident.test":
            time.sleep(0.15)
            return {"ip": "2606:4700:4700::1001"}
        return {"ip": IPS[family]}

    started = time.perf_counter()
    result = _collect(handler)
    assert time.perf_counter() - started >= 0.15
    assert result["collection"]["completion_reason"] == "complete"
    assert result["identity"]["ipv4"]["state"] == "confirmed"
    assert result["identity"]["ipv6"]["state"] == "conflict"
    assert result["identity"]["ipv6"]["ip"] is None


def test_conflicting_primary_ips_do_not_cut_off_silent_other_family():
    release = threading.Event()

    def handler(host, family):
        if family == 6:
            assert release.wait(2)
        if host == "ident.test" and family == 4:
            return {"ip": "8.8.8.8"}
        return {"ip": IPS[family]}

    try:
        result = _collect(handler, budget=0.2)
        assert result["collection"]["completion_reason"] == "deadline"
        assert result["collection"]["elapsed_ms"] >= 180
        assert result["identity"]["ipv4"]["state"] == "conflict"
    finally:
        release.set()


def test_default_collection_still_waits_for_all_requested_evidence():
    release = threading.Event()

    def handler(host, family):
        if family == 6:
            assert release.wait(2)
        return {"ip": IPS[family]}

    try:
        result = _collect(handler, grace=None, budget=0.2)
        assert result["collection"]["completion_reason"] == "deadline"
        assert result["collection"]["elapsed_ms"] >= 180
        assert result["identity"]["ipv4"]["state"] == "confirmed"
    finally:
        release.set()


@pytest.mark.parametrize("grace", [-0.1, float("nan"), float("inf")])
def test_invalid_family_grace_is_rejected(grace):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        collect_exit_identity(rounds=1, total_timeout_seconds=1, completed_family_grace_seconds=grace)


@pytest.mark.parametrize("kwargs", [{"rounds": 1}, {"rounds": 2, "total_timeout_seconds": 1}])
def test_family_grace_requires_bounded_single_round(kwargs):
    with pytest.raises(ValueError, match="total timeout and rounds=1"):
        collect_exit_identity(completed_family_grace_seconds=0.1, **kwargs)
