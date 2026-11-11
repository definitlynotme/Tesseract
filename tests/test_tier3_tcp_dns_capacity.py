from __future__ import annotations

import concurrent.futures
import socket
import threading
import time
from unittest.mock import patch

import pytest

import tier3_tcp_dns
from tier3_tcp_dns import (
    Tier3TcpDnsBusy,
    Tier3TcpDnsResolver,
    tier3_tcp_dns_capacity_for_tcp_cap,
    tier3_tcp_dns_configured_capacity,
)


@pytest.mark.parametrize(
    ("tcp_cap", "capacity"),
    [
        (None, 16), (0, 16), (1, 16), (4, 16), (16, 16), (31, 16), (32, 16),
        (33, 17), (48, 24), (64, 32), (65, 33), (100, 50), (127, 64),
        (128, 64), (256, 64), (float("inf"), 16), ("garbage", 16),
    ],
)
def test_capacity_is_half_the_tcp_cap_within_bounds(tcp_cap, capacity):
    assert tier3_tcp_dns_capacity_for_tcp_cap(tcp_cap) == capacity


@pytest.mark.parametrize(
    ("setting", "tcp_cap", "capacity"),
    [
        # Unset or unrecognised: the default pool, whatever the TCP cap.
        (None, 16, 16), (None, 256, 16), (True, 64, 16), (False, 64, 16),
        ("", 64, 16), ("fast", 64, 16), ("12.5", 64, 16), ([32], 64, 16),
        (float("inf"), 64, 16), (float("nan"), 64, 16),
        # "auto" follows the session TCP cap.
        ("auto", 16, 16), (" Auto ", 64, 32), ("AUTO", 256, 64),
        # Explicit sizes are clamped to [16, 64].
        (24, 16, 24), ("40", 16, 40), (8, 64, 16), (500, 16, 64), (64.9, 16, 64),
    ],
)
def test_configured_capacity_is_opt_in(setting, tcp_cap, capacity):
    assert tier3_tcp_dns_configured_capacity(setting, tcp_cap=tcp_cap) == capacity


def test_process_resolver_default_is_unchanged():
    resolver = Tier3TcpDnsResolver()
    try:
        assert resolver.snapshot() == {
            "capacity": 16, "active_calls": 0, "high_water": 0,
            "saturated_total": 0,
        }
        assert tier3_tcp_dns.tier3_tcp_dns_resolver.capacity >= 16
    finally:
        resolver.shutdown()


def test_same_capacity_keeps_executor():
    resolver = Tier3TcpDnsResolver()
    executor = resolver._executor
    try:
        assert resolver.configure_capacity(16) == 16
        assert resolver._executor is executor
    finally:
        resolver.shutdown()


class _BlockingResolve:
    """Patched getaddrinfo that holds each OS call until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.entered = threading.Semaphore(0)

    def __call__(self, host, *_args):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        self.entered.release()
        try:
            self.release.wait(5)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]
        finally:
            with self.lock:
                self.inflight -= 1

    def wait_entered(self, count: int) -> None:
        for _ in range(count):
            assert self.entered.acquire(timeout=3)


def _wait_idle(resolver: Tier3TcpDnsResolver) -> None:
    deadline = time.monotonic() + 3
    while resolver.snapshot()["active_calls"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert resolver.snapshot()["active_calls"] == 0


def test_growing_capacity_admits_more_calls_and_stays_bounded():
    resolver = Tier3TcpDnsResolver(capacity=2)
    resolve = _BlockingResolve()
    try:
        with patch.object(socket, "getaddrinfo", resolve):
            first = [resolver.submit_getaddrinfo(f"h{i}.invalid", 443) for i in range(2)]
            resolve.wait_entered(2)
            with pytest.raises(Tier3TcpDnsBusy, match="tcp_dns_facility_saturated"):
                resolver.submit_getaddrinfo("busy.invalid", 443)
            assert resolver.configure_capacity(4) == 4
            second = [resolver.submit_getaddrinfo(f"g{i}.invalid", 443) for i in range(2)]
            resolve.wait_entered(2)
            with pytest.raises(Tier3TcpDnsBusy):
                resolver.submit_getaddrinfo("busy.invalid", 443)
            assert resolver.snapshot() == {
                "capacity": 4, "active_calls": 4, "high_water": 4,
                "saturated_total": 2,
            }
            resolve.release.set()
            # Calls on the retired executor finish normally, not cancelled.
            assert all(future.result(3) for future in first + second)
        _wait_idle(resolver)
        assert resolve.peak == 4
    finally:
        resolve.release.set()
        resolver.shutdown()


def test_shrinking_capacity_keeps_running_calls_and_defers_new_ones():
    resolver = Tier3TcpDnsResolver(capacity=4)
    resolve = _BlockingResolve()
    try:
        with patch.object(socket, "getaddrinfo", resolve):
            running = [resolver.submit_getaddrinfo(f"h{i}.invalid", 443) for i in range(4)]
            resolve.wait_entered(4)
            assert resolver.configure_capacity(2) == 2
            with pytest.raises(Tier3TcpDnsBusy):
                resolver.submit_getaddrinfo("busy.invalid", 443)
            assert resolver.snapshot()["active_calls"] == 4
            resolve.release.set()
            assert all(future.result(3) for future in running)
            _wait_idle(resolver)
            resolve.release.clear()
            after = [resolver.submit_getaddrinfo(f"n{i}.invalid", 443) for i in range(2)]
            resolve.wait_entered(2)
            with pytest.raises(Tier3TcpDnsBusy):
                resolver.submit_getaddrinfo("busy.invalid", 443)
            resolve.release.set()
            assert all(future.result(3) for future in after)
        _wait_idle(resolver)
        assert resolve.peak == 4
    finally:
        resolve.release.set()
        resolver.shutdown()


def test_resize_during_concurrent_submissions_never_fails_a_call():
    resolver = Tier3TcpDnsResolver(capacity=16)
    stop = threading.Event()
    unexpected: list[BaseException] = []
    completed = [0]
    completed_lock = threading.Lock()

    def fast_resolve(*_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]

    def submitter() -> None:
        while not stop.is_set():
            try:
                future = resolver.submit_getaddrinfo("opaque.invalid", 443)
            except Tier3TcpDnsBusy:
                continue
            except BaseException as exc:
                unexpected.append(exc)
                return
            try:
                future.result(3)
            except BaseException as exc:
                unexpected.append(exc)
                return
            with completed_lock:
                completed[0] += 1

    try:
        with patch.object(socket, "getaddrinfo", fast_resolve):
            threads = [threading.Thread(target=submitter) for _ in range(8)]
            for thread in threads:
                thread.start()
            for index in range(200):
                resolver.configure_capacity(16 if index % 2 else 32)
            stop.set()
            for thread in threads:
                thread.join(5)
        assert not unexpected
        assert completed[0] > 0
        _wait_idle(resolver)
    finally:
        stop.set()
        resolver.shutdown()


def test_resize_waits_for_a_submission_already_inside_the_executor():
    entered = threading.Event()
    release = threading.Event()
    gate = {"armed": True}

    class _GatedExecutor(concurrent.futures.ThreadPoolExecutor):
        """Hold the first submit inside the executor until released."""

        def submit(self, fn, /, *args, **kwargs):
            if gate["armed"]:
                gate["armed"] = False
                entered.set()
                release.wait(5)
            return super().submit(fn, *args, **kwargs)

    def gated_executor(capacity: int) -> concurrent.futures.ThreadPoolExecutor:
        return _GatedExecutor(
            max_workers=capacity, thread_name_prefix="tier3-tcp-dns",
        )

    def fast_resolve(*_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]

    submitted: dict[str, object] = {}
    errors: list[BaseException] = []

    with patch.object(
        Tier3TcpDnsResolver, "_new_executor", staticmethod(gated_executor),
    ), patch.object(socket, "getaddrinfo", fast_resolve):
        resolver = Tier3TcpDnsResolver(capacity=2)
        retired = resolver._executor

        def submit() -> None:
            try:
                submitted["future"] = resolver.submit_getaddrinfo(
                    "opaque.invalid", 443,
                )
            except BaseException as exc:
                errors.append(exc)

        submitter = threading.Thread(target=submit)
        resizer = threading.Thread(target=resolver.configure_capacity, args=(4,))
        try:
            submitter.start()
            assert entered.wait(3)
            resizer.start()
            resizer.join(0.3)
            # The swap must not retire the executor under a submission that
            # already chose it.
            assert resizer.is_alive()
            assert resolver._executor is retired
            release.set()
            submitter.join(3)
            resizer.join(3)
            assert not submitter.is_alive() and not resizer.is_alive()
            assert not errors
            assert submitted["future"].result(3) == fast_resolve()
            assert resolver._executor is not retired
            assert resolver.snapshot()["capacity"] == 4
            _wait_idle(resolver)
        finally:
            release.set()
            resolver.shutdown()


def test_shutdown_is_sticky_across_resize():
    resolver = Tier3TcpDnsResolver(capacity=2)
    resolver.shutdown()
    resolver.configure_capacity(8)
    with pytest.raises(RuntimeError):
        resolver.submit_getaddrinfo("opaque.invalid", 443)
    assert resolver.snapshot()["active_calls"] == 0
    assert resolver.snapshot()["capacity"] == 8
