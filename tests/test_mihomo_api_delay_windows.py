"""App delay windows recorded by MihomoAPIClient (route lease, lease-v1)."""

from __future__ import annotations

import json

import pytest

import mihomo_api
from mihomo_api import MihomoAPIClient, recent_delay_windows


class FakeResponse:
    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body

    def close(self):
        pass


@pytest.fixture(autouse=True)
def clean_windows():
    mihomo_api.clear_delay_windows()
    yield
    mihomo_api.clear_delay_windows()


def client(base="http://127.0.0.1:9090", fail=False, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request.full_url, timeout))
        if fail:
            raise OSError("controller down")
        return FakeResponse(json.dumps({"delay": 42}).encode())

    return MihomoAPIClient(base_url=base, opener=opener)


def test_proxy_group_and_provider_delays_record_windows():
    api = client()
    api.proxy_delay("leaf-a", url="https://example.test/204", timeout_ms=1000)
    api.group_delay("TIER3-AUTO", url="https://example.test/204", timeout_ms=1000)
    api.health_check_provider("TIER3B-RUNTIME", timeout=1.0)
    windows = recent_delay_windows("http://127.0.0.1:9090")
    assert set(windows) == {"leaf-a", "group:TIER3-AUTO", "provider:TIER3B-RUNTIME"}
    for values in windows.values():
        (issue, finish), = values
        assert finish is not None and finish >= issue


def test_windows_are_recorded_on_exception_and_kept_per_base_url():
    api = client(fail=True)
    with pytest.raises(Exception):
        api.proxy_delay("leaf-a", url="https://example.test/204", timeout_ms=1000)
    other = client(base="http://127.0.0.1:9191")
    other.proxy_delay("leaf-b", url="https://example.test/204", timeout_ms=1000)
    first = recent_delay_windows("http://127.0.0.1:9090")
    assert list(first) == ["leaf-a"] and first["leaf-a"][0][1] is not None
    assert list(recent_delay_windows("http://127.0.0.1:9191/")) == ["leaf-b"]


def test_windows_are_bounded_per_name_and_in_names():
    api = client()
    for _ in range(mihomo_api.DELAY_WINDOW_MAXLEN + 4):
        api.proxy_delay("leaf-a", url="https://example.test/204", timeout_ms=1000)
    assert len(recent_delay_windows(api.base_url)["leaf-a"]) == mihomo_api.DELAY_WINDOW_MAXLEN
    for index in range(mihomo_api.DELAY_WINDOW_MAX_NAMES + 10):
        api.proxy_delay(f"n{index}", url="https://example.test/204", timeout_ms=1000)
    assert len(mihomo_api._DELAY_WINDOWS) == mihomo_api.DELAY_WINDOW_MAX_NAMES


def test_requests_timeouts_and_results_are_unchanged():
    seen: list = []
    api = client(seen=seen)
    result = api.proxy_delay("leaf a", url="https://example.test/204", timeout_ms=1500, request_timeout=0.7)
    assert result == {"delay": 42}
    url, timeout = seen[0]
    assert url.startswith("http://127.0.0.1:9090/proxies/leaf%20a/delay?")
    assert "timeout=1500" in url and timeout == pytest.approx(0.7)
