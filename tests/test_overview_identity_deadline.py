from __future__ import annotations

import copy
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from overview_ip_check import _IdentityDeadline, collect_exit_identity


PROVIDERS = (
    ("fast", 4, "https://fast.test/ip"),
    ("slow", 4, "https://slow.test/ip"),
    ("queued", 4, "https://queued.test/ip"),
)


@pytest.fixture(autouse=True)
def identity_workers_finish():
    existing = set(threading.enumerate())
    yield
    workers = [
        thread for thread in threading.enumerate()
        if thread not in existing and thread.name.startswith("exit-identity")
    ]
    for thread in workers:
        thread.join(timeout=1)
    assert not [thread.name for thread in workers if thread.is_alive()]


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return b'{"ip":"1.1.1.1"}'


@pytest.mark.parametrize("budget", [0.12, 0.35])
def test_remaining_never_exceeds_original_budget_after_deadline_rounding(monkeypatch, budget):
    now = [1_065_694.0]
    monkeypatch.setattr("overview_ip_check.time.monotonic", lambda: now[0])
    deadline = _IdentityDeadline(budget)
    initial = deadline.remaining(4.0)
    assert 0 < initial <= budget
    now[0] += budget / 2
    assert 0 < deadline.remaining(4.0) <= initial


def test_total_deadline_returns_without_waiting_for_running_provider_or_queued_work():
    release = threading.Event()
    finished = threading.Event()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            try:
                assert release.wait(2), "test provider was not released"
                return _Response()
            finally:
                finished.set()

    started = time.monotonic()
    try:
        result = collect_exit_identity(
            opener=Opener(), providers=PROVIDERS, rounds=2,
            timeout_seconds=4, total_timeout_seconds=0.12,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert len(calls) == 1
        assert 0 < calls[0][1] <= 0.12
        assert result["identity"]["ipv4"]["ip"] is None
        assert all(
            attempt["status"] == "timeout"
            for source in result["identity"]["ipv4"]["evidence"]
            for attempt in source["attempts"]
        )
        snapshot = copy.deepcopy(result)
    finally:
        release.set()
    assert finished.wait(1)
    assert result == snapshot
    assert len(calls) == 1


def test_total_deadline_keeps_partial_success_without_claiming_missing_rounds_were_stable():
    release = threading.Event()
    finished = threading.Event()

    class Opener:
        def open(self, request, timeout):
            if "slow.test" in request.full_url:
                try:
                    assert release.wait(2)
                finally:
                    finished.set()
            return _Response()

    try:
        result = collect_exit_identity(
            opener_factory=lambda _: Opener(), providers=PROVIDERS[:2], rounds=2,
            total_timeout_seconds=0.12,
        )
        ipv4 = result["identity"]["ipv4"]
        assert ipv4["ip"] == "1.1.1.1"
        assert ipv4["state"] == "unconfirmed"
        assert ipv4["agreement"]["stable_sources"] == 0
        assert ipv4["evidence"][0]["stability"] == "unconfirmed"
        assert [attempt["status"] for attempt in ipv4["evidence"][0]["attempts"]] == ["ok", "timeout"]
        assert [attempt["status"] for attempt in ipv4["evidence"][1]["attempts"]] == ["timeout", "timeout"]
    finally:
        release.set()
    assert finished.wait(1)


def test_slow_opener_setup_cannot_start_request_after_collection_deadline():
    release = threading.Event()
    finished = threading.Event()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return _Response()

    def factory(_proxy):
        try:
            assert release.wait(2)
            return Opener()
        finally:
            finished.set()

    try:
        result = collect_exit_identity(
            opener_factory=factory, providers=PROVIDERS[:1], rounds=1,
            total_timeout_seconds=0.12,
        )
        assert result["identity"]["ipv4"]["ip"] is None
    finally:
        release.set()
    assert finished.wait(1)
    assert calls == []


def test_deadline_interrupts_active_response_socket_and_worker_closes_it():
    reader, writer = socket.socketpair()
    closed = threading.Event()
    reading = threading.Event()

    class Response(_Response):
        def __init__(self):
            self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=reader))

        def read(self, limit):
            reading.set()
            return reader.recv(limit)

        def __exit__(self, *_args):
            reader.close()
            closed.set()

    class Opener:
        def open(self, request, timeout):
            return Response()

    try:
        started = time.monotonic()
        result = collect_exit_identity(
            opener=Opener(), providers=PROVIDERS[:1], rounds=1,
            timeout_seconds=4, total_timeout_seconds=0.12,
        )
        assert time.monotonic() - started < 0.5
        assert reading.is_set()
        assert closed.wait(1)
        assert reader.fileno() == -1
        assert result["identity"]["ipv4"]["ip"] is None
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_total_deadline_requires_finite_positive_budget(budget):
    with pytest.raises(ValueError, match="total_timeout_seconds"):
        collect_exit_identity(total_timeout_seconds=budget)
