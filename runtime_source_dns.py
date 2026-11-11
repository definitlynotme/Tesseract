"""Read-only resolver probe primitives, never a host DNS writer.

This module owns the wire-level pieces shared by resolver qualification:
a minimal DNS query builder/parser, a bounded plaintext UDP probe that fans
out to several resolvers and control names at once, and the outer fake-IP
check that recognises another fake-IP tunnel answering on the host resolver.
The decision itself lives in ``dns_resolver_policy``; the orchestration in
``dns_resolver_qualification``.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import secrets
import select
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from dns_resolver_policy import OUTER_FAKE_IP_NETWORK
from tier3_tcp_dns import Tier3TcpDnsBusy, tier3_tcp_dns_resolver

_PLAIN_RESOLVER_PROBE_SECONDS = 0.400
_PLAIN_RESOLVER_PORT = 53
_OUTER_FAKE_IP_PROBE_SECONDS = 0.250
_DNS_TYPE_A = 1
_DNS_TYPE_AAAA = 28


@dataclass(frozen=True, slots=True)
class PlainAnswer:
    """One parsed reply: response code plus every A/AAAA address it carried."""

    rcode: int
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlainProbeResult:
    address: str
    latency_ms: int | None = None
    answers: Mapping[str, PlainAnswer] = field(default_factory=dict)

    @property
    def replied(self) -> bool:
        return self.latency_ms is not None


def _dns_query_packet(hostname: str, *, record_type: int = _DNS_TYPE_A) -> bytes | None:
    labels: list[bytes] = []
    for label in hostname.strip(".").split("."):
        try:
            encoded = label.encode("idna")
        except (UnicodeError, ValueError):
            return None
        if not 1 <= len(encoded) <= 63:
            return None
        labels.append(encoded)
    if not labels:
        return None
    question = b"".join(bytes((len(label),)) + label for label in labels) + b"\x00"
    # Standard recursive query; the caller decides how much of the reply matters.
    return (
        secrets.token_bytes(2)
        + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        + question
        + struct.pack("!HH", int(record_type), 1)
    )


def _read_dns_name(payload: bytes, offset: int) -> int:
    """Return the offset just past a (possibly compressed) name."""

    hops = 0
    end = None
    while True:
        if offset >= len(payload):
            raise ValueError("truncated name")
        length = payload[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(payload):
                raise ValueError("truncated pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | payload[offset + 1]
            hops += 1
            if hops > 32:
                raise ValueError("compression loop")
            continue
        offset += 1 + length
    return offset if end is None else end


def _parse_dns_answers(payload: bytes) -> PlainAnswer | None:
    """Parse the response code and A/AAAA answers; tolerate truncation."""

    if len(payload) < 12:
        return None
    _, flags, qdcount, ancount, _, _ = struct.unpack_from("!HHHHHH", payload, 0)
    if not flags & 0x8000:
        return None
    rcode = flags & 0x000F
    addresses: list[str] = []
    offset = 12
    try:
        for _ in range(qdcount):
            offset = _read_dns_name(payload, offset) + 4
        for _ in range(ancount):
            offset = _read_dns_name(payload, offset)
            record_type, _, _, rdlength = struct.unpack_from("!HHIH", payload, offset)
            offset += 10
            rdata = payload[offset:offset + rdlength]
            offset += rdlength
            if record_type == _DNS_TYPE_A and len(rdata) == 4:
                addresses.append(socket.inet_ntop(socket.AF_INET, rdata))
            elif record_type == _DNS_TYPE_AAAA and len(rdata) == 16:
                addresses.append(socket.inet_ntop(socket.AF_INET6, rdata))
    except (ValueError, struct.error, OSError):
        pass
    return PlainAnswer(rcode=rcode, addresses=tuple(addresses))


def probe_plain_resolvers(
    resolvers: Sequence[str],
    hostnames: Sequence[str],
    *,
    budget_seconds: float = _PLAIN_RESOLVER_PROBE_SECONDS,
    port: int | None = None,
    clock=time.monotonic,
) -> dict[str, PlainProbeResult]:
    """Query every resolver for every hostname over UDP inside one budget.

    Returns one result per resolver.  A resolver that never replies has
    ``latency_ms=None``; socket/encoding faults yield an empty mapping so the
    caller treats the probe as inconclusive rather than as evidence.
    """

    targets = [str(item or "").strip() for item in resolvers if str(item or "").strip()]
    names = [str(item or "").strip() for item in hostnames if str(item or "").strip()]
    if not targets or not names:
        return {}
    queries: dict[bytes, tuple[str, str]] = {}
    packets: list[tuple[str, bytes]] = []
    for resolver in targets:
        for name in names:
            packet = _dns_query_packet(name)
            if packet is None:
                return {}
            queries[packet[:2]] = (resolver, name)
            packets.append((resolver, packet))
    destination_port = int(port if port is not None else _PLAIN_RESOLVER_PORT)
    handles: dict[int, socket.socket] = {}
    started: dict[str, float] = {}
    results: dict[str, dict[str, PlainAnswer]] = {resolver: {} for resolver in targets}
    latencies: dict[str, int] = {}
    deadline = clock() + max(0.05, float(budget_seconds))
    try:
        sent = 0
        for resolver, packet in packets:
            try:
                family = socket.AF_INET6 if ipaddress.ip_address(resolver).version == 6 else socket.AF_INET
            except ValueError:
                continue
            handle = handles.get(family)
            if handle is None:
                try:
                    handle = socket.socket(family, socket.SOCK_DGRAM)
                    handle.setblocking(False)
                except OSError:
                    return {}
                handles[family] = handle
            try:
                handle.sendto(packet, (resolver, destination_port))
            except OSError:
                continue
            started.setdefault(resolver, clock())
            sent += 1
        if not sent:
            return {}
        expected = len(queries)
        received = 0
        while received < expected:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select(list(handles.values()), [], [], remaining)
            except (OSError, ValueError):
                break
            if not readable:
                break
            for handle in readable:
                try:
                    payload, _ = handle.recvfrom(1232)
                except (BlockingIOError, InterruptedError, ConnectionError):
                    # Windows reports an ICMP rejection on the following read.
                    continue
                except OSError:
                    break
                target = queries.get(payload[:2])
                if target is None:
                    continue
                answer = _parse_dns_answers(payload)
                if answer is None:
                    continue
                resolver, name = target
                if name in results[resolver]:
                    continue
                results[resolver][name] = answer
                received += 1
                if resolver not in latencies:
                    latencies[resolver] = max(
                        0, int(round((clock() - started.get(resolver, deadline)) * 1000.0))
                    )
    finally:
        for handle in handles.values():
            handle.close()
    return {
        resolver: PlainProbeResult(
            address=resolver,
            latency_ms=latencies.get(resolver),
            answers=dict(results[resolver]),
        )
        for resolver in targets
    }


def encrypted_upstream_hostnames(dns: object) -> tuple[str, ...]:
    """Hostnames of encrypted upstreams that would need a plaintext bootstrap lookup.

    Empty when the policy is not "encrypted by hostname only" (disabled DNS,
    explicit direct resolvers, literal or plain entries).
    """

    if not isinstance(dns, dict) or not dns.get("enable"):
        return ()
    if dns.get("direct-nameserver"):
        return ()
    servers = dns.get("nameserver", [])
    if not isinstance(servers, list):
        return ()
    hosts: list[str] = []
    for server in servers:
        try:
            parsed = urlsplit(str(server))
        except ValueError:
            return ()
        host = parsed.hostname
        if parsed.scheme not in {"https", "tls", "quic", "h3"} or parsed.fragment or not host:
            return ()
        try:
            ipaddress.ip_address(host)
            return ()
        except ValueError:
            pass
        if host not in hosts:
            hosts.append(host)
    if not hosts or len(hosts) > 4:
        return ()
    return tuple(hosts)


def detect_outer_fake_ip(
    hostnames: Iterable[str],
    *,
    budget_seconds: float = _OUTER_FAKE_IP_PROBE_SECONDS,
) -> bool | None:
    """Whether the host resolver answers these names from a fake-IP range.

    ``True``/``False`` are positive evidence; ``None`` means the shared
    resolver was busy, slow, or failed, which must not change any policy.
    """

    names = [str(item or "").strip() for item in hostnames if str(item or "").strip()]
    if not names:
        return None
    deadline = time.monotonic() + max(0.05, float(budget_seconds))
    pending: list[concurrent.futures.Future] = []
    try:
        for host in names:
            try:
                pending.append(tier3_tcp_dns_resolver.submit_getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM,
                ))
            except (Tier3TcpDnsBusy, RuntimeError):
                break
        done, unfinished = concurrent.futures.wait(
            pending, timeout=max(0.0, deadline - time.monotonic()),
        )
        if unfinished or len(done) != len(names):
            return None
        for future in done:
            try:
                addresses = {ipaddress.ip_address(item[4][0]) for item in future.result()}
            except (OSError, ValueError, concurrent.futures.CancelledError):
                return None
            if not addresses or not all(address in OUTER_FAKE_IP_NETWORK for address in addresses):
                return False
        return True
    finally:
        # Running OS calls retain their shared slot until they actually exit.
        for future in pending:
            future.cancel()


__all__ = [
    "PlainAnswer",
    "PlainProbeResult",
    "detect_outer_fake_ip",
    "encrypted_upstream_hostnames",
    "probe_plain_resolvers",
]
