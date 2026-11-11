"""Bounded, positive-evidence-only resolver qualification.

One run discovers the link's own resolvers, probes them and the neutral
encrypted catalog in parallel inside a hard wall-clock budget, and returns a
frozen :class:`ResolverQualification`.  Nothing here waits on an unknown:
whatever has not answered when the budget ends is reported as such, and the
policy layer treats "no verdict" as "no evidence", never as a downgrade.

No request body is sent anywhere; every probe is a single DNS control query
whose answer is validated against a known set.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from dns_resolver_policy import (
    CANDIDATE_ANSWER_MISMATCH,
    CANDIDATE_CONNECT_FAILED,
    CANDIDATE_NOT_PROBED,
    CANDIDATE_QUALIFIED,
    CANDIDATE_TIMEOUT,
    CANDIDATE_TLS_FAILED,
    LINK_ANSWERS_INTERNAL_ONLY,
    LINK_ANSWERS_PUBLIC,
    LINK_CAPTIVE_SUSPECTED,
    LINK_NOT_PROBED,
    LINK_OUTER_FAKE_IP,
    LINK_RESOLVER_CONTROL_ANSWERS,
    LINK_RESOLVER_CONTROL_NAMES,
    LINK_SILENT,
    OUTER_FAKE_IP_NETWORK,
    TRANSPORT_DOH,
    EncryptedCandidate,
    EncryptedCandidateResult,
    LinkResolverResult,
    ResolverQualification,
    encrypted_candidates,
)
from runtime_source_dns import (
    PlainAnswer,
    PlainProbeResult,
    _dns_query_packet,
    _parse_dns_answers,
    detect_outer_fake_ip,
    probe_plain_resolvers,
)

QUALIFICATION_BUDGET_SECONDS = 2.0
DISCOVERY_BUDGET_SECONDS = 0.15
# Probes run concurrently and the deadline loop bounds wall time, so one
# probe may use (nearly) the whole budget: a TLS handshake to a distant
# resolver is three round trips and must not be mistaken for silence.
ENCRYPTED_PROBE_TIMEOUT_SECONDS = 1.8
LINK_PROBE_BUDGET_SECONDS = 0.4
EARLY_RETURN_QUALIFIED_COUNT = 2
MAX_ENCRYPTED_PROBES = 10
# Every probe must start immediately: a slow failure must never delay the
# one endpoint that works (ten encrypted probes + link + outer fake-IP).
MAX_PROBE_WORKERS = 12

_WINDOWS_AF_INET = 2
_WINDOWS_AF_INET6 = 23
_IF_OPER_STATUS_UP = 1
_IF_TYPE_SOFTWARE_LOOPBACK = 24
_ERROR_BUFFER_OVERFLOW = 111
_ERROR_NO_DATA = 232
_GAA_FLAG_SKIP_ANYCAST = 0x0002
_GAA_FLAG_SKIP_MULTICAST = 0x0004
_GAA_FLAG_INCLUDE_PREFIX = 0x0010
_GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
_DEFAULT_EXCLUDED_ADAPTER_NAMES: frozenset[str] = frozenset({"tesseract", "mihomo", "meta", "clash"})
_SYSTEMD_STUB_ADDRESSES: frozenset[str] = frozenset({"127.0.0.53", "127.0.0.54"})


@dataclass(frozen=True, slots=True)
class DiscoveredAdapter:
    index: int
    name: str
    friendly_name: str
    oper_up: bool
    has_gateway: bool
    dns_servers: tuple[str, ...]
    suffix: str
    unicast: tuple[str, ...]
    if_type: int = 0


@dataclass(frozen=True, slots=True)
class LinkDiscovery:
    adapters: tuple[DiscoveredAdapter, ...] = ()
    resolvers: tuple[str, ...] = ()
    resolver_adapters: Mapping[str, tuple[int, bool]] = None  # type: ignore[assignment]
    suffixes: tuple[str, ...] = ()
    ipv4_available: bool = True
    ipv6_available: bool = False
    fingerprint: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.resolver_adapters is None:
            object.__setattr__(self, "resolver_adapters", {})


def _is_usable_resolver_address(
    address: str,
    *,
    excluded: Iterable[str] = (),
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
        return False
    if parsed in OUTER_FAKE_IP_NETWORK:
        return False
    if parsed.version == 6 and parsed.is_link_local:
        # Zone-scoped resolvers are not usable as config literals (Phase 1).
        return False
    if parsed.version == 6 and parsed.is_site_local:
        # Windows advertises fec0:0:0:ffff::1-3 on adapters with no resolver.
        return False
    if str(parsed) in set(excluded):
        return False
    return True


def _fingerprint(adapters: Iterable[DiscoveredAdapter]) -> str:
    rows = sorted(
        (
            adapter.index,
            adapter.name,
            adapter.has_gateway,
            tuple(sorted(adapter.dns_servers)),
            adapter.suffix,
            tuple(sorted(adapter.unicast)),
        )
        for adapter in adapters
        if adapter.oper_up
    )
    digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
    return digest[:32]


def _assemble_discovery(
    adapters: Sequence[DiscoveredAdapter],
    *,
    excluded_addresses: Iterable[str],
    excluded_adapter_names: Iterable[str],
) -> LinkDiscovery:
    excluded_names = {str(name or "").strip().lower() for name in excluded_adapter_names}
    ordered = sorted(
        (adapter for adapter in adapters if adapter.oper_up),
        key=lambda adapter: (0 if adapter.has_gateway else 1, adapter.index),
    )
    resolvers: list[str] = []
    resolver_adapters: dict[str, tuple[int, bool]] = {}
    suffixes: list[str] = []
    ipv4_available = False
    ipv6_available = False
    for adapter in ordered:
        if adapter.if_type == _IF_TYPE_SOFTWARE_LOOPBACK:
            continue
        for address in adapter.unicast:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
                continue
            if parsed in OUTER_FAKE_IP_NETWORK:
                continue
            if parsed.version == 4:
                ipv4_available = True
            else:
                ipv6_available = True
        lowered_names = {adapter.name.strip().lower(), adapter.friendly_name.strip().lower()}
        if lowered_names & excluded_names:
            continue
        for server in adapter.dns_servers:
            if not _is_usable_resolver_address(server, excluded=excluded_addresses):
                continue
            if server not in resolvers:
                resolvers.append(server)
                resolver_adapters[server] = (adapter.index, adapter.has_gateway)
        suffix = adapter.suffix.strip().strip(".").lower()
        if suffix and suffix not in suffixes:
            suffixes.append(suffix)
    return LinkDiscovery(
        adapters=tuple(adapters),
        resolvers=tuple(resolvers),
        resolver_adapters=resolver_adapters,
        suffixes=tuple(suffixes),
        ipv4_available=ipv4_available or not (ipv4_available or ipv6_available),
        ipv6_available=ipv6_available,
        fingerprint=_fingerprint(adapters),
    )


# ----------------------------------------------------------------- Windows

def _windows_adapters() -> tuple[DiscoveredAdapter, ...]:
    import ctypes
    from ctypes import wintypes

    class _LengthPair(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("Second", wintypes.ULONG)]

    class _Alignment(ctypes.Union):
        _fields_ = [("Alignment", ctypes.c_ulonglong), ("s", _LengthPair)]

    class _SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class _AddressNode(ctypes.Structure):
        pass

    _AddressNode._fields_ = [
        ("u", _Alignment),
        ("Next", ctypes.POINTER(_AddressNode)),
        ("Address", _SocketAddress),
    ]

    class _UnicastAddress(ctypes.Structure):
        pass

    _UnicastAddress._fields_ = [
        ("u", _Alignment),
        ("Next", ctypes.POINTER(_UnicastAddress)),
        ("Address", _SocketAddress),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", wintypes.ULONG),
        ("PreferredLifetime", wintypes.ULONG),
        ("LeaseLifetime", wintypes.ULONG),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
    ]

    class _AdapterAddresses(ctypes.Structure):
        pass

    _AdapterAddresses._fields_ = [
        ("u", _Alignment),
        ("Next", ctypes.POINTER(_AdapterAddresses)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.POINTER(_AddressNode)),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
        ("Mtu", wintypes.ULONG),
        ("IfType", wintypes.ULONG),
        ("OperStatus", ctypes.c_int),
        ("Ipv6IfIndex", wintypes.ULONG),
        ("ZoneIndices", wintypes.ULONG * 16),
        ("FirstPrefix", ctypes.c_void_p),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("FirstWinsServerAddress", ctypes.c_void_p),
        ("FirstGatewayAddress", ctypes.POINTER(_AddressNode)),
        ("Ipv4Metric", wintypes.ULONG),
        ("Ipv6Metric", wintypes.ULONG),
    ]

    def sockaddr_text(entry: _SocketAddress) -> str:
        if not entry.lpSockaddr or entry.iSockaddrLength < 8:
            return ""
        raw = ctypes.string_at(entry.lpSockaddr, entry.iSockaddrLength)
        family = int.from_bytes(raw[0:2], "little")
        try:
            if family == _WINDOWS_AF_INET and len(raw) >= 8:
                return socket.inet_ntop(socket.AF_INET, raw[4:8])
            if family == _WINDOWS_AF_INET6 and len(raw) >= 24:
                return socket.inet_ntop(socket.AF_INET6, raw[8:24])
        except (OSError, ValueError):
            return ""
        return ""

    ip_helper = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_adapters = ip_helper.GetAdaptersAddresses
    get_adapters.argtypes = [
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
    ]
    get_adapters.restype = wintypes.ULONG
    flags = (
        _GAA_FLAG_SKIP_ANYCAST
        | _GAA_FLAG_SKIP_MULTICAST
        | _GAA_FLAG_INCLUDE_PREFIX
        | _GAA_FLAG_INCLUDE_GATEWAYS
    )
    size = wintypes.ULONG(16384)
    buffer = None
    for _ in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        status = get_adapters(0, flags, None, buffer, ctypes.byref(size))
        if status == 0:
            break
        if status == _ERROR_BUFFER_OVERFLOW:
            continue
        if status == _ERROR_NO_DATA:
            return ()
        raise OSError(status, "GetAdaptersAddresses failed")
    else:
        raise OSError(_ERROR_BUFFER_OVERFLOW, "GetAdaptersAddresses buffer never sufficed")

    adapters: list[DiscoveredAdapter] = []
    node = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
    while node:
        entry = node.contents
        dns_servers: list[str] = []
        server = entry.FirstDnsServerAddress
        while server:
            text = sockaddr_text(server.contents.Address)
            if text and text not in dns_servers:
                dns_servers.append(text)
            server = server.contents.Next
        unicast: list[str] = []
        address = entry.FirstUnicastAddress
        while address:
            text = sockaddr_text(address.contents.Address)
            if text and text not in unicast:
                unicast.append(text)
            address = address.contents.Next
        has_gateway = False
        gateway = entry.FirstGatewayAddress
        while gateway:
            if sockaddr_text(gateway.contents.Address):
                has_gateway = True
                break
            gateway = gateway.contents.Next
        adapters.append(
            DiscoveredAdapter(
                index=int(entry.u.s.Second),
                name=str((entry.AdapterName or b"").decode("ascii", "replace")),
                friendly_name=str(entry.FriendlyName or entry.Description or ""),
                oper_up=int(entry.OperStatus) == _IF_OPER_STATUS_UP,
                has_gateway=has_gateway,
                dns_servers=tuple(dns_servers),
                suffix=str(entry.DnsSuffix or ""),
                unicast=tuple(unicast),
                if_type=int(entry.IfType),
            )
        )
        node = entry.Next
    return tuple(adapters)


# ------------------------------------------------------------------- POSIX

def _resolv_conf_adapters(path: str = "/etc/resolv.conf") -> tuple[DiscoveredAdapter, ...]:
    servers: list[str] = []
    suffixes: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text or text.startswith(("#", ";")):
                    continue
                parts = text.split()
                if parts[0] == "nameserver" and len(parts) >= 2:
                    server = parts[1].split("%")[0]
                    if server not in servers:
                        servers.append(server)
                elif parts[0] in {"search", "domain"}:
                    for suffix in parts[1:]:
                        if suffix not in suffixes:
                            suffixes.append(suffix)
    except OSError:
        return ()
    unicast: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            candidate = str(info[4][0]).split("%")[0]
            if candidate not in unicast:
                unicast.append(candidate)
    except (OSError, ValueError):
        pass
    if not servers and not suffixes:
        return ()
    return (
        DiscoveredAdapter(
            index=1,
            name="resolv.conf",
            friendly_name="resolv.conf",
            oper_up=True,
            has_gateway=True,
            dns_servers=tuple(servers),
            suffix=suffixes[0] if suffixes else "",
            unicast=tuple(unicast),
        ),
    )


def _macos_scutil_adapters(timeout_seconds: float = 0.4) -> tuple[DiscoveredAdapter, ...]:
    import subprocess

    try:
        completed = subprocess.run(
            ["scutil", "--dns"],
            capture_output=True,
            text=True,
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    servers: list[str] = []
    suffixes: list[str] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if text.startswith("nameserver["):
            _, _, value = text.partition(":")
            server = value.strip().split("%")[0]
            if server and server not in servers:
                servers.append(server)
        elif text.startswith(("search domain[", "domain ")):
            _, _, value = text.partition(":")
            suffix = value.strip()
            if suffix and suffix not in suffixes:
                suffixes.append(suffix)
    if not servers:
        return ()
    return (
        DiscoveredAdapter(
            index=1,
            name="scutil",
            friendly_name="scutil --dns",
            oper_up=True,
            has_gateway=True,
            dns_servers=tuple(servers),
            suffix=suffixes[0] if suffixes else "",
            unicast=(),
        ),
    )


def discover_link_resolvers(
    *,
    excluded_addresses: Iterable[str] = (),
    excluded_adapter_names: Iterable[str] = _DEFAULT_EXCLUDED_ADAPTER_NAMES,
    adapter_source: Callable[[], Sequence[DiscoveredAdapter]] | None = None,
) -> LinkDiscovery:
    """Enumerate the link's resolvers without any subprocess on Windows."""

    try:
        if adapter_source is not None:
            adapters = tuple(adapter_source())
        elif os.name == "nt":
            adapters = _windows_adapters()
        elif sys.platform == "darwin":
            adapters = _macos_scutil_adapters() or _resolv_conf_adapters()
        else:
            adapters = _resolv_conf_adapters()
    except Exception as exc:  # discovery is advisory; never fail qualification
        return LinkDiscovery(error=f"{type(exc).__name__}: {exc}", fingerprint="unknown")
    return _assemble_discovery(
        adapters,
        excluded_addresses=excluded_addresses,
        excluded_adapter_names=excluded_adapter_names,
    )


# ------------------------------------------------------------------ probes

def _classify_link_result(result: PlainProbeResult) -> str:
    if not result.replied:
        return LINK_SILENT
    answers = [answer for answer in result.answers.values() if isinstance(answer, PlainAnswer)]
    if not answers:
        return LINK_SILENT
    address_sets = [set(answer.addresses) for answer in answers]
    if any(addresses & LINK_RESOLVER_CONTROL_ANSWERS for addresses in address_sets):
        return LINK_ANSWERS_PUBLIC
    non_empty = [addresses for addresses in address_sets if addresses]
    if non_empty:
        if len(non_empty) >= 2 and all(addresses == non_empty[0] for addresses in non_empty):
            return LINK_CAPTIVE_SUSPECTED
        for addresses in non_empty:
            for text in addresses:
                try:
                    parsed = ipaddress.ip_address(text)
                except ValueError:
                    continue
                if (
                    parsed.is_private
                    or parsed.is_link_local
                    or parsed.is_loopback
                    or (parsed.version == 4 and parsed in ipaddress.ip_network("100.64.0.0/10"))
                ):
                    return LINK_CAPTIVE_SUSPECTED
        # Wrong public answers for both control names: treat as lying resolver.
        return LINK_CAPTIVE_SUSPECTED
    return LINK_ANSWERS_INTERNAL_ONLY


def _wire_query(candidate: EncryptedCandidate) -> bytes:
    packet = _dns_query_packet(candidate.control_name)
    if packet is None:
        raise ValueError("control name cannot be encoded")
    return packet


def _doh_exchange(
    candidate: EncryptedCandidate,
    query: bytes,
    *,
    timeout_seconds: float,
    context: ssl.SSLContext,
) -> bytes:
    host = f"[{candidate.address}]" if candidate.family == 6 else candidate.address
    connection = http.client.HTTPSConnection(
        host, int(candidate.port or 443), timeout=max(0.1, float(timeout_seconds)), context=context,
    )
    try:
        encoded = base64.urlsafe_b64encode(query).rstrip(b"=").decode("ascii")
        path = "/dns-query"
        if "://" in candidate.url:
            from urllib.parse import urlsplit

            path = urlsplit(candidate.url).path or "/dns-query"
        connection.request(
            "GET",
            f"{path}?dns={encoded}",
            headers={"Accept": "application/dns-message", "Connection": "close"},
        )
        response = connection.getresponse()
        body = response.read(4096)
        if response.status != 200:
            raise _AnswerMismatch(f"status {response.status}")
        return body
    finally:
        connection.close()


def _dot_exchange(
    candidate: EncryptedCandidate,
    query: bytes,
    *,
    timeout_seconds: float,
    context: ssl.SSLContext,
) -> bytes:
    timeout = max(0.1, float(timeout_seconds))
    raw = socket.create_connection((candidate.address, int(candidate.port or 853)), timeout=timeout)
    try:
        raw.settimeout(timeout)
        secure = context.wrap_socket(raw, server_hostname=candidate.address)
        try:
            secure.sendall(len(query).to_bytes(2, "big") + query)
            header = _recv_exact(secure, 2)
            length = int.from_bytes(header, "big")
            return _recv_exact(secure, min(length, 4096))
        finally:
            secure.close()
    finally:
        raw.close()


def _recv_exact(handle: ssl.SSLSocket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = handle.recv(remaining)
        if not chunk:
            raise _AnswerMismatch("connection closed before the answer")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _AnswerMismatch(Exception):
    pass


def probe_encrypted_candidate(
    candidate: EncryptedCandidate,
    *,
    timeout_seconds: float = ENCRYPTED_PROBE_TIMEOUT_SECONDS,
    ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    clock=time.monotonic,
) -> EncryptedCandidateResult:
    """Validate one IP-literal encrypted endpoint with a known-answer query."""

    started = clock()
    try:
        query = _wire_query(candidate)
        context = ssl_context_factory()
        if candidate.transport == TRANSPORT_DOH:
            payload = _doh_exchange(candidate, query, timeout_seconds=timeout_seconds, context=context)
        else:
            payload = _dot_exchange(candidate, query, timeout_seconds=timeout_seconds, context=context)
    except ssl.SSLError:
        return EncryptedCandidateResult(candidate, CANDIDATE_TLS_FAILED)
    except (socket.timeout, TimeoutError):
        return EncryptedCandidateResult(candidate, CANDIDATE_TIMEOUT)
    except _AnswerMismatch:
        return EncryptedCandidateResult(candidate, CANDIDATE_ANSWER_MISMATCH)
    except (OSError, http.client.HTTPException, ValueError):
        return EncryptedCandidateResult(candidate, CANDIDATE_CONNECT_FAILED)
    latency_ms = max(0, int(round((clock() - started) * 1000.0)))
    answer = _parse_dns_answers(payload)
    if answer is None or answer.rcode != 0:
        return EncryptedCandidateResult(candidate, CANDIDATE_ANSWER_MISMATCH, latency_ms)
    if set(answer.addresses) & set(candidate.control_answers):
        return EncryptedCandidateResult(candidate, CANDIDATE_QUALIFIED, latency_ms)
    return EncryptedCandidateResult(candidate, CANDIDATE_ANSWER_MISMATCH, latency_ms)


# ------------------------------------------------------------ orchestration

@dataclass(frozen=True, slots=True)
class QualificationHint:
    """Ordering hint from per-network memory; never a verdict."""

    encrypted_urls: tuple[str, ...] = ()
    link_resolvers: tuple[str, ...] = ()


def _order_candidates(
    candidates: Sequence[EncryptedCandidate],
    hint: QualificationHint | None,
) -> list[EncryptedCandidate]:
    preferred = set(hint.encrypted_urls) if hint is not None else set()
    # Remembered winners first, then one transport round per provider so a
    # capped worker pool still gives every operator an early attempt.
    return sorted(
        candidates,
        key=lambda candidate: (
            0 if candidate.url in preferred else 1,
            0 if candidate.transport == TRANSPORT_DOH else 1,
            candidate.provider_id,
        ),
    )


def qualify_resolvers(
    *,
    budget_seconds: float = QUALIFICATION_BUDGET_SECONDS,
    hint: QualificationHint | None = None,
    excluded_addresses: Iterable[str] = (),
    excluded_adapter_names: Iterable[str] = _DEFAULT_EXCLUDED_ADAPTER_NAMES,
    discovery: Callable[..., LinkDiscovery] = discover_link_resolvers,
    plain_probe: Callable[..., Mapping[str, PlainProbeResult]] = probe_plain_resolvers,
    encrypted_probe: Callable[..., EncryptedCandidateResult] = probe_encrypted_candidate,
    outer_fake_ip_probe: Callable[..., bool | None] = detect_outer_fake_ip,
    catalog_candidates: Callable[..., Sequence[EncryptedCandidate]] = encrypted_candidates,
    clock=time.monotonic,
) -> ResolverQualification:
    """Run every probe concurrently inside ``budget_seconds`` and report evidence."""

    started = clock()
    deadline = started + max(0.2, float(budget_seconds))
    link = discovery(
        excluded_addresses=excluded_addresses,
        excluded_adapter_names=excluded_adapter_names,
    )
    ipv6_only = bool(link.ipv6_available and not link.ipv4_available)
    candidates = _order_candidates(
        catalog_candidates(
            include_ipv4=not ipv6_only,
            include_ipv6=ipv6_only,
        ),
        hint,
    )[:MAX_ENCRYPTED_PROBES]

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_PROBE_WORKERS, thread_name_prefix="dns-qualify",
    )
    futures: dict[concurrent.futures.Future, tuple[str, object]] = {}
    try:
        if link.resolvers:
            futures[executor.submit(
                outer_fake_ip_probe, LINK_RESOLVER_CONTROL_NAMES[:1],
            )] = ("outer_fake_ip", None)
            futures[executor.submit(
                plain_probe,
                tuple(link.resolvers),
                LINK_RESOLVER_CONTROL_NAMES,
                budget_seconds=min(LINK_PROBE_BUDGET_SECONDS, max(0.1, deadline - clock())),
            )] = ("link", None)
        for candidate in candidates:
            futures[executor.submit(
                encrypted_probe,
                candidate,
                timeout_seconds=min(ENCRYPTED_PROBE_TIMEOUT_SECONDS, max(0.1, deadline - clock())),
            )] = ("encrypted", candidate)

        link_results: Mapping[str, PlainProbeResult] = {}
        outer_fake_ip: bool | None = None
        encrypted_results: dict[str, EncryptedCandidateResult] = {}
        pending = set(futures)
        while pending:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                kind, payload = futures[future]
                try:
                    value = future.result()
                except Exception:
                    continue
                if kind == "link" and isinstance(value, Mapping):
                    link_results = value
                elif kind == "outer_fake_ip":
                    outer_fake_ip = value if isinstance(value, bool) else None
                elif kind == "encrypted" and isinstance(value, EncryptedCandidateResult):
                    encrypted_results[value.candidate.url] = value
            qualified_urls = {
                url for url, item in encrypted_results.items() if item.verdict == CANDIDATE_QUALIFIED
            }
            remembered_winner = bool(
                hint is not None and qualified_urls & set(hint.encrypted_urls)
            )
            if (
                len(qualified_urls) >= EARLY_RETURN_QUALIFIED_COUNT or remembered_winner
            ) and (link_results or not link.resolvers):
                break
        unfinished_running = {
            futures[future][1] for future in pending if future.running()
        }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    link_rows: list[LinkResolverResult] = []
    for resolver in link.resolvers:
        adapter_index, has_gateway = link.resolver_adapters.get(resolver, (None, False))
        result = link_results.get(resolver)
        if result is None:
            verdict = LINK_NOT_PROBED if not link_results else LINK_SILENT
            latency = None
        else:
            verdict = _classify_link_result(result)
            latency = result.latency_ms
        if outer_fake_ip is True and verdict != LINK_SILENT:
            verdict = LINK_OUTER_FAKE_IP
        link_rows.append(
            LinkResolverResult(
                address=resolver,
                verdict=verdict,
                latency_ms=latency,
                interface_index=adapter_index,
                has_gateway=bool(has_gateway),
            )
        )

    candidate_rows: list[EncryptedCandidateResult] = []
    for candidate in candidates:
        result = encrypted_results.get(candidate.url)
        if result is not None:
            candidate_rows.append(result)
        elif candidate in unfinished_running:
            candidate_rows.append(EncryptedCandidateResult(candidate, CANDIDATE_TIMEOUT))
        else:
            candidate_rows.append(EncryptedCandidateResult(candidate, CANDIDATE_NOT_PROBED))

    any_qualified = any(item.verdict == CANDIDATE_QUALIFIED for item in candidate_rows)
    link_verdicts = {item.verdict for item in link_rows}
    inconclusive = not any_qualified and link_verdicts <= {LINK_SILENT, LINK_NOT_PROBED}
    captive = LINK_CAPTIVE_SUSPECTED in link_verdicts
    return ResolverQualification(
        fingerprint=link.fingerprint or "unknown",
        wall_time_ms=max(0, int(round((clock() - started) * 1000.0))),
        link_resolvers=tuple(link_rows),
        dns_suffixes=tuple(link.suffixes),
        candidates=tuple(candidate_rows),
        ipv4_available=bool(link.ipv4_available),
        ipv6_available=bool(link.ipv6_available),
        ipv6_only=ipv6_only,
        outer_fake_ip=outer_fake_ip is True,
        captive_suspected=captive,
        inconclusive=inconclusive,
    )


__all__ = [
    "DiscoveredAdapter",
    "LinkDiscovery",
    "QualificationHint",
    "discover_link_resolvers",
    "probe_encrypted_candidate",
    "qualify_resolvers",
]
