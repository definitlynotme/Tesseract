"""Bound OS resolver work independently of short-lived TCP wave event loops."""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import threading


TIER3_TCP_DNS_DEFAULT_CAPACITY = 16
TIER3_TCP_DNS_MAX_CAPACITY = 64


def tier3_tcp_dns_capacity_for_tcp_cap(tcp_cap: int) -> int:
    """Resolver slots for a session TCP cap: ceil(cap / 2) within [16, 64]."""

    try:
        cap = max(0, int(tcp_cap or 0))
    except (TypeError, ValueError, OverflowError):
        cap = 0
    return min(
        TIER3_TCP_DNS_MAX_CAPACITY,
        max(TIER3_TCP_DNS_DEFAULT_CAPACITY, (cap + 1) // 2),
    )


def tier3_tcp_dns_configured_capacity(setting: object, *, tcp_cap: int) -> int:
    """Resolver slots for the opt-in ``tiers.tier3.tcp_dns_capacity`` setting.

    Unset or unrecognised keeps the default 16. ``"auto"`` follows the session
    TCP cap via :func:`tier3_tcp_dns_capacity_for_tcp_cap`. An integer is
    clamped to [16, 64], so the setting can only add slots.
    """

    if setting is None or isinstance(setting, bool):
        return TIER3_TCP_DNS_DEFAULT_CAPACITY
    if isinstance(setting, str):
        text = setting.strip().lower()
        if text == "auto":
            return tier3_tcp_dns_capacity_for_tcp_cap(tcp_cap)
        setting = text
    try:
        requested = int(setting)
    except (TypeError, ValueError, OverflowError):
        # YAML ``.inf`` must not stop a session; it is not a size.
        return TIER3_TCP_DNS_DEFAULT_CAPACITY
    return min(
        TIER3_TCP_DNS_MAX_CAPACITY,
        max(TIER3_TCP_DNS_DEFAULT_CAPACITY, requested),
    )


class Tier3TcpDnsBusy(RuntimeError):
    """All resolver slots are occupied by actual, not merely awaited, calls."""


class Tier3TcpDnsResolver:
    def __init__(self, capacity: int = TIER3_TCP_DNS_DEFAULT_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._executor = self._new_executor(self.capacity)
        # ``_lock`` guards the slot counters; ``_executor_lock`` guards which
        # executor receives a submission. Order: _executor_lock, then _lock.
        self._lock = threading.Lock()
        self._executor_lock = threading.Lock()
        self._shutdown_requested = False
        self._active = 0
        self._high_water = 0
        self._saturated = 0

    @staticmethod
    def _new_executor(capacity: int) -> concurrent.futures.ThreadPoolExecutor:
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=capacity, thread_name_prefix="tier3-tcp-dns",
        )

    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        future = self.submit_getaddrinfo(host, port, family=family, type=type, proto=proto, flags=flags)
        return await asyncio.wrap_future(future)

    def submit_getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        """Share the same bounded OS calls with synchronous startup probes."""
        # Cancelling asyncio's await cannot interrupt a Windows getaddrinfo.
        # Keep its slot until the OS call exits; never queue an unbounded tail.
        with self._lock:
            if self._active >= self.capacity:
                self._saturated += 1
                raise Tier3TcpDnsBusy("tcp_dns_facility_saturated")
            self._active += 1
            self._high_water = max(self._high_water, self._active)
        try:
            with self._executor_lock:
                future = self._executor.submit(
                    socket.getaddrinfo, host, port, family, type, proto, flags,
                )
        except BaseException:
            self._release_slot(None)
            raise
        future.add_done_callback(self._release_slot)
        return future

    def _release_slot(self, _future) -> None:
        with self._lock:
            if self._active <= 0:
                raise ValueError("Tier3TcpDnsResolver slot released too many times")
            self._active -= 1

    def configure_capacity(self, capacity: int) -> int:
        """Set the slot bound for new calls; calls already in the OS keep theirs.

        A changed bound gets an executor sized to it. The previous executor is
        shut down without cancelling, so its running calls finish, their slots
        are released normally and its idle threads exit.
        """

        capacity = max(1, int(capacity))
        retired = None
        with self._executor_lock:
            with self._lock:
                if capacity == self.capacity:
                    return capacity
                self.capacity = capacity
            if not self._shutdown_requested:
                retired = self._executor
                self._executor = self._new_executor(capacity)
        if retired is not None:
            retired.shutdown(wait=False)
        return capacity

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"capacity": self.capacity, "active_calls": self._active,
                    "high_water": self._high_water, "saturated_total": self._saturated}

    def shutdown(self) -> None:
        """Stop accepting work without pretending in-progress OS calls exited."""
        with self._executor_lock:
            self._shutdown_requested = True
            executor = self._executor
        executor.shutdown(wait=False, cancel_futures=True)


# Process-owned, so repeated expansions cannot multiply unresolved DNS threads.
# Work items contain only resolver arguments, never a Node, ticket or callback.
tier3_tcp_dns_resolver = Tier3TcpDnsResolver()
