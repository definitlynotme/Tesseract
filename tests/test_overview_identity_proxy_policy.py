from __future__ import annotations

import copy
import io
import threading
import time
import urllib.error
import urllib.request
import urllib.response
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime

import pytest

import overview_ip_check as identity_module
from overview_ip_check import _build_opener, _request_identity, collect_exit_identity
from tests.test_exit_identity import _Response, _UrlOpener, _json_payload


URL = "https://api.ipify.org?format=json"
PROVIDER = ("ipify", 4, URL)
PROXY = "http://127.0.0.1:7890"


@pytest.fixture(autouse=True)
def finish_identity_workers():
    existing = set(threading.enumerate())
    yield
    workers = [thread for thread in threading.enumerate()
               if thread not in existing and thread.name.startswith("exit-identity")]
    for worker in workers:
        worker.join(1)
    assert not [worker.name for worker in workers if worker.is_alive()]


def _collect(opener, **kwargs):
    return collect_exit_identity(
        opener=opener, providers=(PROVIDER,), rounds=1,
        retry_transient_errors=True, total_timeout_seconds=1, **kwargs,
    )["identity"]["ipv4"]


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "Provider unavailable", headers, None)


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("wrapped", [False, True])
def test_connect_transient_status_can_recover_without_an_extra_source_vote(code, wrapped):
    error = OSError(f"Tunnel connection failed: {code} Provider unavailable")
    if wrapped:
        error = urllib.error.URLError(error)
    opener = _UrlOpener({URL: [error, _json_payload("1.1.1.1")]})
    family = _collect(opener)
    assert family["ip"] == "1.1.1.1"
    assert family["state"] == "unconfirmed"
    assert family["agreement"]["successful_sources"] == 1
    assert len(opener.calls) == 2
    attempt = family["evidence"][0]["attempts"][0]
    assert len(attempt["transport_retries"]) == 1
    original = attempt["transport_retries"][0]
    assert original["http_status"] == code
    if code == 429:
        assert original["error_code"] == "rate_limited"


@pytest.mark.parametrize("error", [
    OSError("Tunnel connection failed: 403 Forbidden"),
    OSError("Tunnel connection failed: 407 Proxy Authentication Required"),
    OSError("unrelated 502 operating system failure"),
    OSError("prefix Tunnel connection failed: 503 Unavailable"),
    PermissionError("access denied"),
])
def test_permanent_connect_and_unrelated_os_errors_do_not_retry(error):
    opener = _UrlOpener({URL: [urllib.error.URLError(error), _json_payload("1.1.1.1")]})
    assert _collect(opener)["state"] == "unavailable"
    assert len(opener.calls) == 1


@pytest.mark.parametrize("wrapped", [False, True])
def test_actual_timeout_has_structured_reason_in_final_and_retry_evidence(wrapped):
    error = TimeoutError("socket request timed out")
    if wrapped:
        error = urllib.error.URLError(error)
    opener = _UrlOpener({URL: [error, error]})
    family = _collect(opener)
    attempt = family["evidence"][0]["attempts"][0]
    assert family["state"] == "unavailable"
    assert len(opener.calls) == 2
    assert attempt["status"] == "error"
    assert attempt["error_code"] == "timeout"
    original = attempt["transport_retries"][0]
    assert original["status"] == "error"
    assert original["error_code"] == "timeout"


def test_timeout_words_in_unrelated_error_do_not_create_timeout_reason():
    opener = _UrlOpener({URL: [urllib.error.URLError(OSError("operation timed out"))]})
    attempt = _collect(opener)["evidence"][0]["attempts"][0]
    assert len(opener.calls) == 1
    assert attempt["status"] == "error"
    assert "error_code" not in attempt


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("header", ["30", "http-date"])
def test_retry_after_beyond_shared_budget_preserves_reason_without_retry(monkeypatch, status, header):
    now = 1_700_000_000
    monkeypatch.setattr(identity_module.time, "time", lambda: now)
    if header == "http-date":
        header = format_datetime(datetime.fromtimestamp(now + 30, timezone.utc), usegmt=True)
    opener = _UrlOpener({URL: [_http_error(status, header), _json_payload("1.1.1.1")]})
    family = _collect(opener)
    attempt = family["evidence"][0]["attempts"][0]
    assert len(opener.calls) == 1
    assert attempt["status"] == "error"
    assert attempt["http_status"] == status
    assert attempt["retry_after_seconds"] == 30
    assert "transport_retries" not in attempt
    if status == 429:
        assert attempt["error_code"] == "rate_limited"


def test_retry_after_waits_then_reuses_remaining_shared_budget():
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((time.perf_counter(), timeout))
            if len(calls) == 1:
                raise _http_error(429, "0.1")
            return _Response(_json_payload("1.1.1.1"))

    family = _collect(Opener())
    assert family["ip"] == "1.1.1.1"
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] >= 0.1
    assert 0 < calls[1][1] < calls[0][1] <= 1
    original = family["evidence"][0]["attempts"][0]["transport_retries"][0]
    assert original["error_code"] == "rate_limited"
    assert original["retry_after_seconds"] == 0.1


@pytest.mark.parametrize("header", ["-1", "nan", "inf", "tomorrow", ""])
def test_invalid_retry_after_is_ignored_without_losing_rate_limit_evidence(header):
    opener = _UrlOpener({URL: [_http_error(429, header), _http_error(429)]})
    attempt = _collect(opener)["evidence"][0]["attempts"][0]
    assert len(opener.calls) == 2
    assert attempt["error_code"] == "rate_limited"
    assert attempt["transport_retries"][0]["error_code"] == "rate_limited"
    assert "retry_after_seconds" not in attempt["transport_retries"][0]


def test_family_completion_cancels_retry_after_wait_and_retains_limit_reason():
    limited = threading.Event()
    calls = []
    providers = (
        PROVIDER,
        ("ipify", 6, "https://api6.ipify.org?format=json"),
    )

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if request.full_url != URL:
                limited.set()
                raise _http_error(429, "0.5")
            assert limited.wait(1)
            return _Response(_json_payload("1.1.1.1"))

    started = time.perf_counter()
    result = collect_exit_identity(
        opener_factory=lambda _: Opener(), providers=providers, rounds=1,
        retry_transient_errors=True, total_timeout_seconds=1,
        completed_family_grace_seconds=0.05,
    )
    assert time.perf_counter() - started < 0.4
    assert result["collection"]["completion_reason"] == "completed_family"
    assert result["identity"]["ipv4"]["ip"] == "1.1.1.1"
    attempt = result["identity"]["ipv6"]["evidence"][0]["attempts"][0]
    original = attempt.get("transport_retries", [attempt])[0]
    assert original["error_code"] == "rate_limited"
    assert original["retry_after_seconds"] == 0.5
    snapshot = copy.deepcopy(result)
    for worker in threading.enumerate():
        if worker.name.startswith("exit-identity"):
            worker.join(0.2)
            assert not worker.is_alive()
    assert result == snapshot
    assert len(calls) == 2


def _url_response(url, *, status=200, location=None):
    headers = Message()
    if location is not None:
        headers["Location"] = location
    response = urllib.response.addinfourl(io.BytesIO(_json_payload("1.1.1.1")), headers, url, status)
    response.msg = "OK" if status == 200 else "Found"
    return response


@pytest.mark.parametrize("bypass", ["environment", "system"])
def test_explicit_proxy_ignores_bypass_for_initial_request_and_safe_redirect(monkeypatch, bypass):
    if bypass == "environment":
        monkeypatch.setenv("NO_PROXY", "*")
    else:
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _: True)
    calls = []

    def https_open(_handler, request):
        calls.append((request.host, request._tunnel_host))
        if len(calls) == 1:
            return _url_response(request.full_url, status=302, location="https://api.ipify.org/v4")
        return _url_response(request.full_url)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    assert _request_identity(_build_opener(PROXY), URL, timeout_seconds=1) == _json_payload("1.1.1.1")
    assert calls == [("127.0.0.1:7890", "api.ipify.org")] * 2


@pytest.mark.parametrize("target", ["https://other.test/ip", "http://api.ipify.org/ip"])
def test_forced_proxy_still_rejects_unsafe_redirects_before_another_request(monkeypatch, target):
    calls = []

    def https_open(_handler, request):
        calls.append((request.host, request._tunnel_host))
        return _url_response(request.full_url, status=302, location=target)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    with pytest.raises(urllib.error.HTTPError, match="cross-host or non-HTTPS") as error:
        _request_identity(_build_opener(PROXY), URL, timeout_seconds=1)
    error.value.close()
    assert calls == [("127.0.0.1:7890", "api.ipify.org")]


def test_proxy_failure_never_falls_back_to_direct_transport(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    calls = []

    def https_open(_handler, request):
        calls.append((request.host, request._tunnel_host))
        raise urllib.error.URLError(OSError("Tunnel connection failed: 502 Bad Gateway"))

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    result = collect_exit_identity(
        proxy_url=PROXY, providers=(PROVIDER,), rounds=1,
        retry_transient_errors=True, total_timeout_seconds=1,
    )
    assert result["identity"]["ipv4"]["state"] == "unavailable"
    assert calls == [("127.0.0.1:7890", "api.ipify.org")] * 2


def test_explicit_proxy_preserves_credentials_and_ipv6_proxy_authority(monkeypatch):
    calls = []

    def https_open(_handler, request):
        calls.append((request.host, request._tunnel_host, request.get_header("Proxy-authorization")))
        return _url_response(request.full_url)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    _request_identity(_build_opener("http://user:p%40ss@[::1]:7890"), URL, timeout_seconds=1)
    assert calls == [("[::1]:7890", "api.ipify.org", "Basic dXNlcjpwQHNz")]
