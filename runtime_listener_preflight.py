from __future__ import annotations

import ctypes
import ipaddress
import os
import socket
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable


class RuntimeListenerPreflightState(str, Enum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    INDETERMINATE = "indeterminate"
    CONFIGURATION_ERROR = "configuration_error"


@dataclass(frozen=True, slots=True)
class RuntimeListenerSpec:
    kind: str
    transport: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class RuntimeListenerProbeFailure:
    spec: RuntimeListenerSpec
    stage: str
    family: int | None = None
    target_host: str = ""
    error_number: int | None = None
    windows_error: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBoundEndpoint:
    transport: str
    family: int
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class RuntimeListenerPreflightResult:
    state: RuntimeListenerPreflightState
    specs: tuple[RuntimeListenerSpec, ...]
    conflicts: tuple[RuntimeListenerSpec, ...] = ()
    failures: tuple[RuntimeListenerProbeFailure, ...] = ()
    transient_failures: tuple[RuntimeListenerProbeFailure, ...] = ()
    attempts: int = 1
    endpoint_snapshot_available: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedRuntimeListenerSpec:
    spec: RuntimeListenerSpec
    family: int
    socket_type: int
    target: tuple[object, ...]
    target_host: str


_ERROR_INSUFFICIENT_BUFFER = 122
_NO_ERROR = 0
_TCP_TABLE_OWNER_PID_LISTENER = 3
_UDP_TABLE_OWNER_PID = 1


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = (
        ("state", ctypes.c_ulong),
        ("local_addr", ctypes.c_ulong),
        ("local_port", ctypes.c_ulong),
        ("remote_addr", ctypes.c_ulong),
        ("remote_port", ctypes.c_ulong),
        ("owning_pid", ctypes.c_ulong),
    )


class _MibTcp6RowOwnerPid(ctypes.Structure):
    _fields_ = (
        ("local_addr", ctypes.c_ubyte * 16),
        ("local_scope_id", ctypes.c_ulong),
        ("local_port", ctypes.c_ulong),
        ("remote_addr", ctypes.c_ubyte * 16),
        ("remote_scope_id", ctypes.c_ulong),
        ("remote_port", ctypes.c_ulong),
        ("state", ctypes.c_ulong),
        ("owning_pid", ctypes.c_ulong),
    )


class _MibUdpRowOwnerPid(ctypes.Structure):
    _fields_ = (
        ("local_addr", ctypes.c_ulong),
        ("local_port", ctypes.c_ulong),
        ("owning_pid", ctypes.c_ulong),
    )


class _MibUdp6RowOwnerPid(ctypes.Structure):
    _fields_ = (
        ("local_addr", ctypes.c_ubyte * 16),
        ("local_scope_id", ctypes.c_ulong),
        ("local_port", ctypes.c_ulong),
        ("owning_pid", ctypes.c_ulong),
    )


def _canonical_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    text = str(value or "").strip().strip("[]").split("%", 1)[0]
    if text.casefold() == "localhost":
        text = "127.0.0.1"
    if text in {"", "*"}:
        text = "0.0.0.0"
    return ipaddress.ip_address(text)


def _socket_type(transport: str) -> int:
    normalized = str(transport or "").strip().casefold()
    if normalized == "tcp":
        return socket.SOCK_STREAM
    if normalized == "udp":
        return socket.SOCK_DGRAM
    raise ValueError("listener transport must be tcp or udp")


def _resolve_listener_spec(
    spec: RuntimeListenerSpec,
    *,
    resolver: Callable[..., list[tuple[object, ...]]],
) -> _ResolvedRuntimeListenerSpec:
    if not str(spec.kind or "").strip():
        raise ValueError("listener kind is required")
    port = int(spec.port)
    if not 1 <= port <= 65535:
        raise ValueError("listener port is out of range")
    socket_type = _socket_type(spec.transport)
    host = str(spec.host or "127.0.0.1").strip().strip("[]")
    if host.casefold() == "localhost":
        host = "127.0.0.1"
    if host in {"", "*"}:
        host = "0.0.0.0"
    try:
        address = _canonical_ip(host)
    except ValueError:
        addresses = resolver(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket_type,
        )
        if not addresses:
            raise OSError(f"listener address is unavailable: {host}")
        family, _kind, _protocol, _canonical, target = addresses[0]
        target_tuple = tuple(target)
        return _ResolvedRuntimeListenerSpec(
            spec=spec,
            family=int(family),
            socket_type=socket_type,
            target=target_tuple,
            target_host=str(target_tuple[0]).split("%", 1)[0],
        )
    if address.version == 6:
        return _ResolvedRuntimeListenerSpec(
            spec=spec,
            family=socket.AF_INET6,
            socket_type=socket_type,
            target=(str(address), port, 0, 0),
            target_host=str(address),
        )
    return _ResolvedRuntimeListenerSpec(
        spec=spec,
        family=socket.AF_INET,
        socket_type=socket_type,
        target=(str(address), port),
        target_host=str(address),
    )


def _hosts_overlap(
    left_family: int,
    left_host: str,
    right_family: int,
    right_host: str,
) -> bool:
    try:
        left = _canonical_ip(left_host)
        right = _canonical_ip(right_host)
    except ValueError:
        return str(left_host).casefold() == str(right_host).casefold()
    if int(left_family) != int(right_family):
        # IP Helper does not expose IPV6_V6ONLY. Treating an unrelated IPv6
        # wildcard as proof that an IPv4 endpoint is occupied (or vice versa)
        # would recreate the false-positive startup block. The bundled Core
        # remains the authority for an ambiguous dual-stack bind failure.
        return False
    return bool(left == right or left.is_unspecified or right.is_unspecified)


def _resolved_specs_overlap(
    left: _ResolvedRuntimeListenerSpec,
    right: _ResolvedRuntimeListenerSpec,
) -> bool:
    return bool(
        left.spec.transport.casefold() == right.spec.transport.casefold()
        and int(left.spec.port) == int(right.spec.port)
        and _hosts_overlap(
            left.family,
            left.target_host,
            right.family,
            right.target_host,
        )
    )


def _probe_failure(
    resolved: _ResolvedRuntimeListenerSpec,
    stage: str,
    exc: OSError,
) -> RuntimeListenerProbeFailure:
    return RuntimeListenerProbeFailure(
        spec=resolved.spec,
        stage=stage,
        family=resolved.family,
        target_host=resolved.target_host,
        error_number=(int(exc.errno) if exc.errno is not None else None),
        windows_error=(
            int(getattr(exc, "winerror"))
            if getattr(exc, "winerror", None) is not None
            else None
        ),
    )


def _reserve_listener_specs(
    specs: Iterable[_ResolvedRuntimeListenerSpec],
    *,
    socket_factory: Callable[[int, int], socket.socket],
) -> tuple[list[socket.socket], tuple[RuntimeListenerProbeFailure, ...]]:
    reserved: list[socket.socket] = []
    failures: list[RuntimeListenerProbeFailure] = []
    for resolved in specs:
        probe: socket.socket | None = None
        stage = "socket"
        try:
            probe = socket_factory(resolved.family, resolved.socket_type)
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                stage = "exclusive"
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            if resolved.family == socket.AF_INET6 and hasattr(
                socket,
                "IPV6_V6ONLY",
            ):
                stage = "ipv6_only"
                probe.setsockopt(
                    socket.IPPROTO_IPV6,
                    socket.IPV6_V6ONLY,
                    1,
                )
            stage = "bind"
            probe.bind(resolved.target)
            if resolved.socket_type == socket.SOCK_STREAM:
                stage = "listen"
                probe.listen(1)
            reserved.append(probe)
            probe = None
        except OSError as exc:
            failures.append(_probe_failure(resolved, stage, exc))
        finally:
            if probe is not None:
                probe.close()
    return reserved, tuple(failures)


def _close_sockets(sockets: Iterable[socket.socket]) -> None:
    for probe in sockets:
        try:
            probe.close()
        except OSError:
            pass


def _query_windows_table(
    function: object,
    *,
    family: int,
    table_class: int,
) -> bytes:
    size = ctypes.c_ulong(0)
    for _attempt in range(3):
        code = int(
            function(
                None,
                ctypes.byref(size),
                False,
                int(family),
                int(table_class),
                0,
            )
        )
        if code not in {_NO_ERROR, _ERROR_INSUFFICIENT_BUFFER}:
            raise OSError(code, "Windows listener table query failed")
        if size.value <= 0:
            return b"\x00\x00\x00\x00"
        buffer = ctypes.create_string_buffer(int(size.value))
        code = int(
            function(
                buffer,
                ctypes.byref(size),
                False,
                int(family),
                int(table_class),
                0,
            )
        )
        if code == _NO_ERROR:
            return bytes(buffer.raw[: int(size.value)])
        if code != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(code, "Windows listener table query failed")
    raise OSError(_ERROR_INSUFFICIENT_BUFFER, "Windows listener table changed repeatedly")


def _rows_from_table(raw: bytes, row_type: type[ctypes.Structure]) -> tuple[ctypes.Structure, ...]:
    if len(raw) < ctypes.sizeof(ctypes.c_ulong):
        return ()
    count = int(ctypes.c_ulong.from_buffer_copy(raw[:4]).value)
    row_size = ctypes.sizeof(row_type)
    available = max(0, (len(raw) - 4) // row_size)
    return tuple(
        row_type.from_buffer_copy(raw, 4 + index * row_size)
        for index in range(min(count, available))
    )


def windows_bound_listener_snapshot() -> frozenset[RuntimeBoundEndpoint] | None:
    """Return active Windows TCP listeners and UDP bindings without process data."""

    if os.name != "nt":
        return None
    from ctypes import wintypes

    ip_helper = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_tcp = ip_helper.GetExtendedTcpTable
    get_udp = ip_helper.GetExtendedUdpTable
    argument_types = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
    )
    for function in (get_tcp, get_udp):
        function.argtypes = list(argument_types)
        function.restype = wintypes.DWORD

    endpoints: set[RuntimeBoundEndpoint] = set()
    table_specs = (
        (get_tcp, socket.AF_INET, _TCP_TABLE_OWNER_PID_LISTENER, _MibTcpRowOwnerPid, "tcp"),
        (get_tcp, socket.AF_INET6, _TCP_TABLE_OWNER_PID_LISTENER, _MibTcp6RowOwnerPid, "tcp"),
        (get_udp, socket.AF_INET, _UDP_TABLE_OWNER_PID, _MibUdpRowOwnerPid, "udp"),
        (get_udp, socket.AF_INET6, _UDP_TABLE_OWNER_PID, _MibUdp6RowOwnerPid, "udp"),
    )
    for function, family, table_class, row_type, transport in table_specs:
        raw = _query_windows_table(
            function,
            family=family,
            table_class=table_class,
        )
        for row in _rows_from_table(raw, row_type):
            port = socket.ntohs(int(row.local_port) & 0xFFFF)
            if not 1 <= port <= 65535:
                continue
            if family == socket.AF_INET:
                host = socket.inet_ntoa(struct.pack("<I", int(row.local_addr)))
            else:
                host = socket.inet_ntop(socket.AF_INET6, bytes(row.local_addr))
            endpoints.add(
                RuntimeBoundEndpoint(
                    transport=transport,
                    family=family,
                    host=host,
                    port=port,
                )
            )
    return frozenset(endpoints)


def _failure_has_active_owner(
    failure: RuntimeListenerProbeFailure,
    endpoints: Iterable[RuntimeBoundEndpoint],
) -> bool:
    if failure.stage not in {"bind", "listen"} or failure.family is None:
        return False
    for endpoint in endpoints:
        if (
            endpoint.transport == failure.spec.transport.casefold()
            and int(endpoint.port) == int(failure.spec.port)
            and _hosts_overlap(
                failure.family,
                failure.target_host,
                endpoint.family,
                endpoint.host,
            )
        ):
            return True
    return False


def preflight_runtime_listeners(
    specs: Iterable[RuntimeListenerSpec],
    *,
    endpoint_snapshot_provider: Callable[
        [], frozenset[RuntimeBoundEndpoint] | None
    ] = windows_bound_listener_snapshot,
    socket_factory: Callable[[int, int], socket.socket] | None = None,
    resolver: Callable[..., list[tuple[object, ...]]] | None = None,
) -> RuntimeListenerPreflightResult:
    """Classify required local listeners without inferring from process names."""

    original_specs = tuple(specs)
    factory = socket.socket if socket_factory is None else socket_factory
    resolve = socket.getaddrinfo if resolver is None else resolver
    resolved: list[_ResolvedRuntimeListenerSpec] = []
    failures: list[RuntimeListenerProbeFailure] = []
    for spec in original_specs:
        try:
            candidate = _resolve_listener_spec(spec, resolver=resolve)
        except ValueError:
            failure = RuntimeListenerProbeFailure(
                spec=spec,
                stage="configuration",
            )
            return RuntimeListenerPreflightResult(
                state=RuntimeListenerPreflightState.CONFIGURATION_ERROR,
                specs=original_specs,
                failures=(failure,),
                attempts=0,
            )
        except OSError as exc:
            failures.append(
                RuntimeListenerProbeFailure(
                    spec=spec,
                    stage="resolve",
                    error_number=(
                        int(exc.errno)
                        if isinstance(exc, OSError) and exc.errno is not None
                        else None
                    ),
                    windows_error=(
                        int(getattr(exc, "winerror"))
                        if getattr(exc, "winerror", None) is not None
                        else None
                    ),
                )
            )
            continue
        if any(_resolved_specs_overlap(current, candidate) for current in resolved):
            failure = RuntimeListenerProbeFailure(
                spec=spec,
                stage="configuration",
                family=candidate.family,
                target_host=candidate.target_host,
            )
            return RuntimeListenerPreflightResult(
                state=RuntimeListenerPreflightState.CONFIGURATION_ERROR,
                specs=original_specs,
                failures=(failure,),
                attempts=0,
            )
        resolved.append(candidate)

    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if _resolved_specs_overlap(left, right):
                failure = RuntimeListenerProbeFailure(
                    spec=right.spec,
                    stage="configuration",
                    family=right.family,
                    target_host=right.target_host,
                )
                return RuntimeListenerPreflightResult(
                    state=RuntimeListenerPreflightState.CONFIGURATION_ERROR,
                    specs=original_specs,
                    failures=(failure,),
                    attempts=0,
                )

    first_sockets, first_failures = _reserve_listener_specs(
        resolved,
        socket_factory=factory,
    )
    _close_sockets(first_sockets)
    if not first_failures and not failures:
        return RuntimeListenerPreflightResult(
            state=RuntimeListenerPreflightState.AVAILABLE,
            specs=original_specs,
        )

    failed_specs = {
        failure.spec
        for failure in first_failures
    }
    failed_resolved = tuple(
        candidate for candidate in resolved if candidate.spec in failed_specs
    )
    second_sockets, second_failures = _reserve_listener_specs(
        failed_resolved,
        socket_factory=factory,
    )
    _close_sockets(second_sockets)
    persistent_failures = tuple((*failures, *second_failures))
    if not persistent_failures:
        return RuntimeListenerPreflightResult(
            state=RuntimeListenerPreflightState.AVAILABLE,
            specs=original_specs,
            transient_failures=first_failures,
            attempts=2,
        )

    snapshot_available = False
    endpoints: frozenset[RuntimeBoundEndpoint] = frozenset()
    if any(failure.stage in {"bind", "listen"} for failure in persistent_failures):
        try:
            snapshot = endpoint_snapshot_provider()
        except OSError:
            snapshot = None
        if snapshot is not None:
            endpoints = frozenset(snapshot)
            snapshot_available = True
    conflicts = tuple(
        failure.spec
        for failure in persistent_failures
        if _failure_has_active_owner(failure, endpoints)
    )
    state = (
        RuntimeListenerPreflightState.OCCUPIED
        if conflicts
        else RuntimeListenerPreflightState.INDETERMINATE
    )
    return RuntimeListenerPreflightResult(
        state=state,
        specs=original_specs,
        conflicts=tuple(dict.fromkeys(conflicts)),
        failures=persistent_failures,
        transient_failures=tuple(
            failure
            for failure in first_failures
            if failure.spec not in {item.spec for item in second_failures}
        ),
        attempts=2,
        endpoint_snapshot_available=snapshot_available,
    )


__all__ = [
    "RuntimeBoundEndpoint",
    "RuntimeListenerPreflightResult",
    "RuntimeListenerPreflightState",
    "RuntimeListenerProbeFailure",
    "RuntimeListenerSpec",
    "preflight_runtime_listeners",
    "windows_bound_listener_snapshot",
]
