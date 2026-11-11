from __future__ import annotations

"""Read-only connection evidence for physical-interface audit processes."""

import ctypes
import ipaddress
import os
import socket
import struct
from collections.abc import Iterable, Mapping


def _address(value: str):
    parsed = ipaddress.ip_address(value.split("%", 1)[0])
    return parsed.ipv4_mapped or parsed if isinstance(parsed, ipaddress.IPv6Address) else parsed


def classify_owned_tcp_bindings(
    rows: Iterable[Mapping], process_ids: set[int], physical_addresses: Iterable[str],
) -> dict:
    physical = {_address(value) for value in physical_addresses}
    if not physical or any(value.is_unspecified or value.is_loopback for value in physical):
        raise ValueError("physical witness requires current non-loopback interface addresses")
    result = dict(owned_nonloopback=0, physical_bound=0, unexpected_bound=0,
                  owned_loopback=0, states={})
    for row in rows:
        if int(row["pid"]) not in process_ids:
            continue
        remote = _address(row["remote"])
        if remote.is_unspecified:
            continue
        if remote.is_loopback:
            result["owned_loopback"] += 1
            continue
        result["owned_nonloopback"] += 1
        result["physical_bound" if _address(row["local"]) in physical else "unexpected_bound"] += 1
        state = str(int(row["state"]))
        result["states"][state] = result["states"].get(state, 0) + 1
    return result


def windows_owned_tcp_binding_sample(process_ids: set[int], physical_addresses: Iterable[str]) -> dict:
    if os.name != "nt":
        raise OSError("Windows socket witness is unavailable")
    from ctypes import wintypes
    from runtime_listener_preflight import (
        _MibTcpRowOwnerPid, _MibTcp6RowOwnerPid, _query_windows_table, _rows_from_table,
    )

    library = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_tcp = library.GetExtendedTcpTable
    get_tcp.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL,
                       wintypes.ULONG, wintypes.ULONG, wintypes.ULONG]
    get_tcp.restype = wintypes.DWORD
    rows = []
    for family, row_type in ((socket.AF_INET, _MibTcpRowOwnerPid),
                             (socket.AF_INET6, _MibTcp6RowOwnerPid)):
        raw = _query_windows_table(get_tcp, family=family, table_class=5)
        for row in _rows_from_table(raw, row_type):
            if int(row.owning_pid) not in process_ids:
                continue
            if family == socket.AF_INET:
                local = socket.inet_ntoa(struct.pack("<I", int(row.local_addr)))
                remote = socket.inet_ntoa(struct.pack("<I", int(row.remote_addr)))
            else:
                local = socket.inet_ntop(family, bytes(row.local_addr))
                remote = socket.inet_ntop(family, bytes(row.remote_addr))
            rows.append(dict(pid=int(row.owning_pid), local=local, remote=remote, state=int(row.state)))
    return classify_owned_tcp_bindings(rows, process_ids, physical_addresses)


def physical_tcp_witness_valid(samples: Iterable[Mapping]) -> bool:
    samples = tuple(samples)
    return bool(samples and sum(int(sample.get("owned_nonloopback", 0)) for sample in samples) > 0
        and all(not sample.get("error") and int(sample.get("unexpected_bound", 0)) == 0
                and sample.get("physical_bound") == sample.get("owned_nonloopback")
                for sample in samples))
