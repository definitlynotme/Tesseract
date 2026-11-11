"""The overview answer policy returns shortly after the first usable identity."""
from __future__ import annotations

import copy
import json
import ssl
import threading
import time
from urllib.parse import urlsplit

import pytest

from overview_ip_check import (
    IdentityAnswerPolicy,
    OVERVIEW_ANSWER_POLICY,
    collect_exit_identity,
)
from tests.test_exit_identity import _Response


PROVIDERS = (
    ("ipify", 4, "https://ipify.test/v4"),
    ("ident.me", 4, "https://ident.test/v4"),
    ("ipify", 6, "https://ipify.test/v6"),
    ("ident.me", 6, "https://ident.test/v6"),
)
PROFILE = {"cc": "JP", "country": "Japan", "city": "Tokyo", "asn": 13335, "aso": "Example"}


@pytest.fixture(autouse=True)
def finish_identity_workers():
    existing = set(threading.enumerate())
    yield
    workers = [t for t in threading.enumerate()
               if t not in existing and t.name.startswith("exit-identity")]
    for worker in workers:
        worker.join(2)
    assert not [worker.name for worker in workers if worker.is_alive()]


@pytest.fixture
def release():
    event = threading.Event()
    yield event
    event.set()


def _collect(handler, *, budget=3.0, policy=OVERVIEW_ANSWER_POLICY, providers=PROVIDERS, **kwargs):
    calls: list[tuple[str, int]] = []
    lock = threading.Lock()

    class Opener:
        def open(self, request, timeout):
            parsed = urlsplit(request.full_url)
            key = (parsed.hostname.split(".")[0], int(parsed.path[-1]))
            with lock:
                calls.append(key)
            return _Response(json.dumps(handler(*key, timeout)).encode())

    started = time.perf_counter()
    result = collect_exit_identity(
        providers=providers, opener_factory=lambda _: Opener(),
        rounds=1, max_workers=len(providers), total_timeout_seconds=budget,
        retry_transient_errors=True, answer_policy=policy, **kwargs,
    )
    return result, time.perf_counter() - started, calls


def _statuses(result, family):
    return [
        source["attempts"][0]["status"] for source in result["identity"][family]["evidence"]
    ]


def test_hung_same_family_source_returns_after_confirm_grace(release):
    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ipify":
            assert release.wait(5)
            raise TimeoutError("timed out")
        return {"ip": "1.1.1.1", **PROFILE}

    result, elapsed, _calls = _collect(handler)
    assert 0.6 <= elapsed < 1.2
    assert result["collection"]["completion_reason"] == "answered"
    assert result["identity"]["ipv4"]["state"] == "unconfirmed"
    assert result["identity"]["ipv4"]["location"]["city"] == "Tokyo"
    assert "abandoned" in _statuses(result, "ipv4")


def test_pending_profile_source_extends_wait_to_profile_grace(release):
    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ident":
            assert release.wait(5)
            raise TimeoutError("timed out")
        return {"ip": "1.1.1.1"}

    result, elapsed, _calls = _collect(handler)
    assert 1.35 <= elapsed < 2.0
    assert result["collection"]["completion_reason"] == "answered"
    assert "location" not in result["identity"]["ipv4"]


def test_profile_arrival_confirming_the_ip_ends_the_wait():
    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ident":
            time.sleep(0.3)
            return {"ip": "1.1.1.1", **PROFILE}
        return {"ip": "1.1.1.1"}

    result, elapsed, _calls = _collect(handler)
    assert elapsed < 0.6
    assert result["identity"]["ipv4"]["state"] == "confirmed"
    assert result["identity"]["ipv4"]["network"]["asn"] == "13335"


def test_conflict_is_never_hidden_by_an_early_answer():
    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ident":
            time.sleep(0.3)
            return {"ip": "1.0.0.1", **PROFILE}
        return {"ip": "1.1.1.1"}

    result, _elapsed, _calls = _collect(handler)
    ipv4 = result["identity"]["ipv4"]
    assert ipv4["state"] == "conflict"
    assert ipv4["ip"] is None


def test_settled_ipv4_gives_a_silent_ipv6_only_a_short_grace(release):
    def handler(_source, family, _timeout):
        if family == 6:
            assert release.wait(5)
            raise TimeoutError("timed out")
        return {"ip": "1.1.1.1", **PROFILE}

    result, elapsed, _calls = _collect(handler)
    assert 0.25 <= elapsed < 0.8
    assert result["identity"]["ipv4"]["state"] == "confirmed"
    assert result["identity"]["ipv6"]["state"] == "unavailable"


def test_single_ipv6_answer_gets_a_bounded_confirmation(release):
    def handler(source, family, _timeout):
        if family == 6 and source == "ident":
            assert release.wait(5)
            raise TimeoutError("timed out")
        if family == 6:
            return {"ip": "2606:4700:4700::1111"}
        return {"ip": "1.1.1.1", **PROFILE}

    result, elapsed, _calls = _collect(handler)
    assert 0.6 <= elapsed < 1.2
    assert result["identity"]["ipv6"]["state"] == "unconfirmed"
    assert result["identity"]["ipv6"]["ip"] == "2606:4700:4700::1111"


def test_ipv6_conflict_inside_its_grace_is_reported():
    def handler(source, family, _timeout):
        if family == 6 and source == "ident":
            time.sleep(0.15)
            return {"ip": "2606:4700:4700::1001"}
        if family == 6:
            return {"ip": "2606:4700:4700::1111"}
        return {"ip": "1.1.1.1", **PROFILE}

    result, _elapsed, _calls = _collect(handler)
    assert result["identity"]["ipv6"]["state"] == "conflict"
    assert result["identity"]["ipv4"]["state"] == "confirmed"


def test_ipv6_is_never_transport_retried_under_the_policy():
    def handler(_source, _family, _timeout):
        raise OSError("Tunnel connection failed: 502 Bad Gateway")

    result, _elapsed, calls = _collect(handler, budget=1.5)
    assert sorted(calls) == sorted([("ipify", 4), ("ipify", 4), ("ident", 4), ("ident", 4),
                                    ("ipify", 6), ("ident", 6)])
    assert result["identity"]["ipv4"]["state"] == "unavailable"


def test_failed_ipv4_makes_ipv6_the_answer_family(release):
    def handler(source, family, _timeout):
        if family == 4:
            raise ValueError("bad payload")
        if source == "ipify":
            assert release.wait(5)
            raise TimeoutError("timed out")
        return {"ip": "2606:4700:4700::1111", **PROFILE}

    result, elapsed, _calls = _collect(handler)
    assert elapsed < 1.2
    assert result["collection"]["answer_family"] == "ipv6"
    assert result["identity"]["ipv6"]["location"]["city"] == "Tokyo"


def test_silent_ipv4_waits_only_a_bounded_grace_after_ipv6_answers(release):
    def handler(source, family, _timeout):
        if family == 4:
            assert release.wait(8)
            raise TimeoutError("timed out")
        time.sleep(0.2)
        return {"ip": "2606:4700:4700::1111", **PROFILE}

    result, elapsed, _calls = _collect(handler, budget=6.0)
    grace = OVERVIEW_ANSWER_POLICY.primary_silent_grace_seconds
    assert grace <= elapsed < grace + 1.0, "a silent IPv4 must not hold the answer for the whole budget"
    assert result["collection"]["completion_reason"] == "answered"
    assert result["collection"]["answer_family"] == "ipv6"
    assert result["identity"]["ipv6"]["state"] == "confirmed"
    assert set(_statuses(result, "ipv4")) == {"abandoned"}


def test_late_ipv4_inside_the_silent_grace_becomes_the_answer():
    def handler(source, family, _timeout):
        if family == 6:
            return {"ip": "2606:4700:4700::1111", **PROFILE}
        time.sleep(0.9)
        if source == "ident":
            return {"ip": "1.1.1.1", **PROFILE}
        raise ssl.SSLEOFError("EOF")

    result, elapsed, _calls = _collect(handler, budget=6.0)
    assert elapsed < 2.5
    assert result["collection"]["answer_family"] == "ipv4"
    assert result["identity"]["ipv4"]["ip"] == "1.1.1.1"


def test_a_batch_is_accepted_in_arrival_order(monkeypatch, release):
    """A conflict that lands inside a window extended by an earlier arrival
    in the same batch is kept even when the collector wakes up late."""
    import concurrent.futures as futures_module
    import overview_ip_check as module

    real_wait = futures_module.wait
    gate = {"armed": False}

    def late_wait(fs, timeout=None, return_when=futures_module.ALL_COMPLETED):
        done, not_done = real_wait(fs, timeout=timeout, return_when=return_when)
        if gate["armed"] and done:
            gate["armed"] = False
            time.sleep(0.4)  # the collector thread wakes up late
            done, not_done = real_wait(fs, timeout=0, return_when=return_when)
        return done, not_done

    monkeypatch.setattr(module, "wait", late_wait)
    providers = (
        ("ipify", 4, "https://ipify.test/v4"),
        ("aws", 4, "https://aws.test/v4"),
        ("ipify", 6, "https://ipify.test/v6"),
        ("ident.me", 6, "https://ident.test/v6"),
    )
    # IPv4 stop = first IPv4 (~0 s) + 0.5 s. The first IPv6 at 0.4 s moves it
    # to 1.2 s; the conflicting IPv4 at 0.6 s is inside the moved window.
    policy = IdentityAnswerPolicy(confirm_grace_seconds=0.5, profile_grace_seconds=0.5,
                                  secondary_confirm_grace_seconds=0.8,
                                  secondary_silent_grace_seconds=0.0)

    def handler(source, family, _timeout):
        if source == "ipify" and family == 4:
            return {"ip": "1.1.1.1"}
        if source == "ipify" and family == 6:
            time.sleep(0.4)
            gate["armed"] = True
            return {"ip": "2606:4700:4700::1111"}
        if source == "aws":
            time.sleep(0.6)
            return {"ip": "8.8.8.8"}
        assert release.wait(5)
        raise TimeoutError("timed out")

    result, _elapsed, _calls = _collect(handler, budget=4.0, policy=policy, providers=providers)
    assert result["identity"]["ipv4"]["state"] == "conflict"
    assert "abandoned" not in _statuses(result, "ipv4")


def test_answer_family_reflects_the_final_state():
    def handler(source, family, _timeout):
        if family == 6:
            time.sleep(0.5)
            raise ssl.SSLEOFError("EOF")
        if source == "ipify":
            time.sleep(0.05)
            return {"ip": "1.1.1.1"}
        time.sleep(0.3)
        return {"ip": "8.8.8.8"}

    result, _elapsed, _calls = _collect(handler)
    assert result["identity"]["ipv4"]["state"] == "conflict"
    assert result["collection"]["answer_family"] is None


def test_answer_family_is_reported_when_the_answer_arrives_last():
    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ident":
            raise ValueError("bad payload")
        time.sleep(0.4)
        return {"ip": "1.1.1.1"}

    result, _elapsed, _calls = _collect(handler)
    assert result["identity"]["ipv4"]["ip"] == "1.1.1.1"
    assert result["collection"]["answer_family"] == "ipv4"


def test_results_that_arrived_during_a_hold_count_after_its_release(monkeypatch):
    """The hold boundary is when the release was observed, not when the
    collector happened to process an earlier arrival."""
    import concurrent.futures as futures_module
    import overview_ip_check as module

    real_wait = futures_module.wait
    hold = threading.Event()
    started = time.monotonic()

    def releasing_wait(fs, timeout=None, return_when=futures_module.ALL_COMPLETED):
        done, not_done = real_wait(fs, timeout=timeout, return_when=return_when)
        if done and time.monotonic() - started > 0.9:
            hold.set()  # released between the arrival and its processing
        return done, not_done

    monkeypatch.setattr(module, "wait", releasing_wait)
    providers = (
        ("ipify", 4, "https://ipify.test/v4"),
        ("aws", 4, "https://aws.test/v4"),
        ("ipify", 6, "https://ipify.test/v6"),
    )

    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "aws":
            time.sleep(1.0)  # after the 0.75 s answer stop, inside the hold
            return {"ip": "8.8.8.8"}
        return {"ip": "1.1.1.1"}

    result, _elapsed, _calls = _collect(
        handler, budget=4.0, providers=providers, not_before=hold,
        policy=IdentityAnswerPolicy(profile_sources=()),
    )
    assert result["identity"]["ipv4"]["state"] == "conflict"


def test_policy_never_cuts_before_the_first_usable_ip():
    def handler(_source, family, _timeout):
        time.sleep(0.9 if family == 4 else 0.0)
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        return {"ip": "1.1.1.1", **PROFILE}

    result, elapsed, _calls = _collect(handler, budget=1.5)
    assert elapsed >= 0.85
    assert result["identity"]["ipv4"]["state"] == "confirmed"


def test_no_usable_ip_keeps_deadline_semantics(release):
    def handler(_source, _family, _timeout):
        assert release.wait(5)
        raise TimeoutError("timed out")

    result, _elapsed, _calls = _collect(handler, budget=0.4)
    assert result["collection"]["completion_reason"] == "deadline"
    statuses = _statuses(result, "ipv4") + _statuses(result, "ipv6")
    assert "abandoned" not in statuses
    assert set(statuses) == {"timeout"}


def test_late_results_cannot_mutate_the_returned_snapshot(release):
    def handler(source, family, _timeout):
        if source == "ipify" and family == 4:
            assert release.wait(5)
            return {"ip": "1.0.0.1"}
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        return {"ip": "1.1.1.1", **PROFILE}

    result, _elapsed, _calls = _collect(handler)
    snapshot = copy.deepcopy(result)
    release.set()
    time.sleep(0.2)
    assert result == snapshot
    assert result["identity"]["ipv4"]["ip"] == "1.1.1.1"


def test_first_ok_ms_answer_family_and_abandoned_count_are_reported(release):
    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ipify":
            assert release.wait(5)
            raise TimeoutError("timed out")
        return {"ip": "1.1.1.1", **PROFILE}

    result, _elapsed, _calls = _collect(handler)
    collection = result["collection"]
    assert collection["answer_family"] == "ipv4"
    assert collection["abandoned_sources"] == 1
    assert collection["first_ok_ms"]["ipv4"] is not None
    assert collection["first_ok_ms"]["ipv6"] is None


def test_not_before_holds_an_early_answer_and_cancel_ends_it(release):
    held = threading.Event()

    def handler(source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        if source == "ipify":
            assert release.wait(5)
            raise TimeoutError("timed out")
        return {"ip": "1.1.1.1", **PROFILE}

    threading.Timer(1.2, held.set).start()
    result, elapsed, _calls = _collect(handler, not_before=held)
    assert elapsed >= 1.15
    assert result["collection"]["completion_reason"] == "answered"

    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    result, elapsed, _calls = _collect(handler, not_before=threading.Event(), cancel=cancel)
    assert elapsed < 0.8
    assert result["collection"]["completion_reason"] == "cancelled"


@pytest.mark.parametrize("kwargs,match", [
    ({"policy": IdentityAnswerPolicy(confirm_grace_seconds=-1)}, "finite and nonnegative"),
    ({"policy": IdentityAnswerPolicy(confirm_grace_seconds=float("nan"))}, "finite and nonnegative"),
    ({"policy": IdentityAnswerPolicy(confirm_grace_seconds=2.0, profile_grace_seconds=1.0)}, "profile grace"),
])
def test_invalid_policy_is_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        _collect(lambda *_args: {"ip": "1.1.1.1"}, **kwargs)


def test_policy_requires_a_bounded_single_round_and_excludes_legacy_grace():
    with pytest.raises(ValueError, match="total timeout and rounds=1"):
        collect_exit_identity(rounds=2, total_timeout_seconds=1, answer_policy=OVERVIEW_ANSWER_POLICY)
    with pytest.raises(ValueError, match="replaces completed_family_grace_seconds"):
        collect_exit_identity(
            rounds=1, total_timeout_seconds=1, answer_policy=OVERVIEW_ANSWER_POLICY,
            completed_family_grace_seconds=0.1,
        )
    with pytest.raises(ValueError, match="requires an answer_policy"):
        collect_exit_identity(rounds=1, total_timeout_seconds=1, not_before=threading.Event())


def test_timezone_and_network_type_are_validated_before_display():
    def handler(_source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        return {"ip": "1.1.1.1", **PROFILE, "tz": "Asia/Tokyo", "type": "hosting"}

    result, _elapsed, _calls = _collect(handler)
    ipv4 = result["identity"]["ipv4"]
    assert ipv4["location"]["timezone"] == "Asia/Tokyo"
    assert ipv4["network"]["type"] == "hosting"

    def hostile(_source, family, _timeout):
        if family == 6:
            raise ssl.SSLEOFError("EOF")
        return {"ip": "1.1.1.1", **PROFILE, "tz": "<b>x</b>", "type": "vpn-provider"}

    result, _elapsed, _calls = _collect(hostile)
    ipv4 = result["identity"]["ipv4"]
    assert "timezone" not in ipv4["location"]
    assert "type" not in ipv4["network"]


def test_network_types_match_the_display_labels():
    from overview_ip_check import IDENT_ME_NETWORK_TYPES
    from ui_i18n import EXIT_NETWORK_TYPE_TEXT

    assert set(IDENT_ME_NETWORK_TYPES) == set(EXIT_NETWORK_TYPE_TEXT)
