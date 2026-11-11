"""Ephemeral single-node probes whose successful latency may be applied by the runtime owner."""

from __future__ import annotations

import hashlib
import ctypes
import json
import logging
import math
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes
from typing import Any, Callable, Iterable, Literal, NotRequired, Protocol, TypedDict
from urllib.request import ProxyHandler, build_opener

import yaml

from clash_formatter import (
    SUPPORTED_CLASH_PROTOCOLS,
    node_to_clash_proxy,
)
from dns_resolver_policy import helper_dns_block
from mihomo_api import MihomoAPIClient
from models import Node, utc_now_iso
from tier1_protocols import is_tier1_protocol_variant, tier1_proxy_name
from tier3_windows_job import IS_WINDOWS, WindowsJob
from windows_subprocess import hidden_subprocess_kwargs


ProbeState = Literal["success", "failed", "unavailable"]
DiagnosticProbeMode = Literal["all", "direct", "routed"]
EphemeralDiagnosticMode = Literal["direct", "chain"]
EphemeralDiagnosticState = Literal["success", "failed", "cancelled"]


class NodeDiagnosticProbeResult(TypedDict):
    method: str
    state: ProbeState
    latency_ms: float | None
    target: str
    error: str | None


class NodeDiagnosticResult(TypedDict):
    request_id: str
    node_identity: str
    node_name: str
    config_fingerprint: str
    started_at: str
    finished_at: str
    probes: dict[str, NodeDiagnosticProbeResult]


class EphemeralNodeDiagnosticResult(TypedDict):
    """UI-safe outcome from one request-scoped Mihomo process."""

    request_id: str
    node_identity: str
    config_fingerprint: str
    mode: EphemeralDiagnosticMode
    started_at: str
    completed_at: str
    state: EphemeralDiagnosticState
    latency_ms: float | None
    reason: str
    status: NotRequired[str]
    status_applied: NotRequired[bool]
    last_checked_at: NotRequired[str]
    quality_reason: NotRequired[str]
    latency_limit_ms: NotRequired[int | None]


class MihomoDiagnosticClient(Protocol):
    def proxies(self) -> dict[str, Any]: ...

    def proxy_delay(self, name: str, *, url: str, timeout_ms: int = 5000) -> dict[str, Any]: ...


class EphemeralMihomoDiagnosticClient(Protocol):
    def version(self) -> dict[str, Any]: ...

    def proxy_delay(self, name: str, *, url: str, timeout_ms: int = 5000) -> dict[str, Any]: ...


class EphemeralProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class EphemeralProcessOwner(Protocol):
    """Own one complete process domain from creation through confirmed reap."""

    def launch(self, command: list[str], **kwargs: Any) -> EphemeralProcess: ...

    def terminate_and_wait(self) -> bool: ...

    def resources_released(self) -> bool: ...


TcpConnector = Callable[[tuple[str, int], float], Any]
CoreBindingValidator = Callable[["NodeDiagnosticCoreBinding"], tuple[bool, str | None]]
ProcessOwnerFactory = Callable[[], EphemeralProcessOwner]


@dataclass(frozen=True)
class NodeDiagnosticCoreBinding:
    """Immutable description of the proxy configuration loaded by Mihomo."""

    generation: int
    runtime_config_digest: str
    proxy_config_fingerprints: tuple[tuple[str, str], ...]
    core_generation: int = 0
    proxy_protocol_counts: tuple[tuple[str, int], ...] = ()

    def proxy_fingerprint(self, proxy_name: str) -> str | None:
        for name, fingerprint in self.proxy_config_fingerprints:
            if name == proxy_name:
                return fingerprint
        return None

    def protocol_counts(self) -> dict[str, int]:
        return dict(self.proxy_protocol_counts)


_SILENT_FORMATTER_LOGGER = logging.Logger("tesseract.node-diagnostic.formatter")
_SILENT_FORMATTER_LOGGER.disabled = True


def _payload_fingerprint(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _effective_node_proxy_config(node: Node) -> dict[str, Any] | None:
    try:
        proxy = node_to_clash_proxy(
            node,
            logger=_SILENT_FORMATTER_LOGGER,
            seen_names=set(),
        )
    except Exception:
        return None
    if not isinstance(proxy, dict):
        return None
    effective = dict(proxy)
    if str(node.tier or "").strip().lower() == "exit":
        effective["dialer-proxy"] = "PROXY-FALLBACK"
    return effective


def _fallback_node_config(node: Node) -> dict[str, Any]:
    """Stable config-only identity for nodes that Mihomo cannot export."""
    return {
        "name": node.name,
        "protocol": node.protocol,
        "uri": node.uri,
        "address": node.address,
        "port": node.port,
        "tier": node.tier,
        "encryption": node.encryption,
        "security": node.security,
        "network": node.network,
        "host": node.host,
        "path": node.path,
        "sni": node.sni,
        "uuid": node.uuid,
        "password": node.password,
        "method": node.method,
        "plugin": node.plugin,
        "remarks": node.remarks,
        "tls": node.tls,
    }


def diagnostic_node_identity(node: Node) -> str:
    return "|".join([str(node.tier), *node.dedupe_key])


def node_config_fingerprint(node: Node) -> str:
    """Fingerprint only configuration that can affect the node's runtime behavior."""
    proxy = _effective_node_proxy_config(node)
    payload = {
        "tier": str(node.tier or ""),
        "proxy": proxy if proxy is not None else _fallback_node_config(node),
    }
    return _payload_fingerprint(payload)


def node_effective_proxy_config_fingerprint(node: Node) -> str | None:
    """Fingerprint the effective Mihomo proxy payload, excluding its display name."""
    proxy = _effective_node_proxy_config(node)
    if proxy is None:
        return None
    return _payload_fingerprint({key: value for key, value in proxy.items() if key != "name"})


def build_node_diagnostic_core_binding(
    runtime_config: bytes | str,
    *,
    generation: int,
    core_generation: int = 0,
) -> NodeDiagnosticCoreBinding:
    """Parse the exact successfully loaded runtime config into an immutable binding."""
    raw = runtime_config.encode("utf-8") if isinstance(runtime_config, str) else bytes(runtime_config)
    payload = yaml.safe_load(raw.decode("utf-8-sig")) or {}
    if not isinstance(payload, dict):
        raise ValueError("mihomo runtime config must be a mapping")
    proxies = payload.get("proxies") or []
    if not isinstance(proxies, list):
        raise ValueError("mihomo runtime config proxies must be a list")
    proxy_fingerprints: dict[str, str] = {}
    protocol_counts: dict[str, int] = {}
    for entry in proxies:
        if not isinstance(entry, dict):
            raise ValueError("mihomo runtime proxy entry must be a mapping")
        name = str(entry.get("name") or "")
        if not name:
            raise ValueError("mihomo runtime proxy entry has no name")
        if name in proxy_fingerprints:
            raise ValueError(f"duplicate mihomo runtime proxy name: {name}")
        proxy_fingerprints[name] = _payload_fingerprint(
            {key: value for key, value in entry.items() if key != "name"}
        )
        protocol = str(entry.get("type") or "").strip().lower()
        if protocol:
            protocol_counts[protocol] = protocol_counts.get(protocol, 0) + 1
    return NodeDiagnosticCoreBinding(
        generation=max(1, int(generation)),
        runtime_config_digest=hashlib.sha256(raw).hexdigest(),
        proxy_config_fingerprints=tuple(sorted(proxy_fingerprints.items())),
        core_generation=max(0, int(core_generation)),
        proxy_protocol_counts=tuple(sorted(protocol_counts.items())),
    )


def _probe_result(
    method: str,
    state: ProbeState,
    *,
    target: str,
    latency_ms: float | None = None,
    error: str | None = None,
) -> NodeDiagnosticProbeResult:
    return {
        "method": method,
        "state": state,
        "latency_ms": latency_ms,
        "target": target,
        "error": error,
    }


def _tcp_target(node: Node) -> str:
    address = str(node.address or "").strip()
    rendered_address = f"[{address}]" if ":" in address and not address.startswith("[") else address
    try:
        port = int(node.port)
    except (TypeError, ValueError):
        port = 0
    return f"tcp://{rendered_address}:{port}"


def _tcp_connect_probe(
    node: Node,
    *,
    timeout_ms: int,
    connector: TcpConnector,
    monotonic: Callable[[], float],
) -> NodeDiagnosticProbeResult:
    target = _tcp_target(node)
    address = str(node.address or "").strip()
    try:
        port = int(node.port)
    except (TypeError, ValueError):
        port = 0
    if not address:
        return _probe_result("tcp_connect", "failed", target=target, error="node address is empty")
    if not 1 <= port <= 65535:
        return _probe_result("tcp_connect", "failed", target=target, error="node port is invalid")

    started = monotonic()
    connection: Any | None = None
    try:
        connection = connector((address, port), timeout_ms / 1000.0)
        latency_ms = max(0.0, (monotonic() - started) * 1000.0)
        return _probe_result(
            "tcp_connect",
            "success",
            target=target,
            latency_ms=round(latency_ms, 2),
        )
    except Exception as exc:
        return _probe_result("tcp_connect", "failed", target=target, error=str(exc) or type(exc).__name__)
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _mihomo_proxy_base_name(node: Node) -> str | None:
    protocol = str(node.protocol or "").strip().lower()
    if protocol not in SUPPORTED_CLASH_PROTOCOLS:
        return None
    if is_tier1_protocol_variant(node):
        return tier1_proxy_name(node)
    return str(node.name or node.remarks or f"{node.protocol}-{node.address}:{node.port}")


def _resolve_unique_proxy_name(target_node: Node, runtime_nodes: list[Node]) -> tuple[str | None, str | None]:
    expected_name = _mihomo_proxy_base_name(target_node)
    if not expected_name:
        return None, "node protocol is not available in the running Mihomo configuration"

    matches = [node for node in runtime_nodes if _mihomo_proxy_base_name(node) == expected_name]
    if len(matches) != 1 or node_config_fingerprint(matches[0]) != node_config_fingerprint(target_node):
        return None, "target proxy name is not unique in the captured runtime configuration"
    return expected_name, None


def _core_binding_error(
    *,
    binding: NodeDiagnosticCoreBinding | None,
    validator: CoreBindingValidator | None,
    proxy_name: str,
    expected_proxy_fingerprint: str | None,
) -> str | None:
    if binding is None:
        return "loaded Mihomo configuration binding is unavailable"
    if not expected_proxy_fingerprint:
        return "node has no effective Mihomo proxy configuration"
    loaded_fingerprint = binding.proxy_fingerprint(proxy_name)
    if loaded_fingerprint is None:
        return "target proxy is not present in the loaded Mihomo configuration binding"
    if loaded_fingerprint != expected_proxy_fingerprint:
        return "target proxy configuration does not match the captured node"
    if validator is None:
        return None
    try:
        valid, reason = validator(binding)
    except Exception as exc:
        return f"loaded Mihomo configuration binding validation failed: {str(exc) or type(exc).__name__}"
    if not valid:
        return str(reason or "loaded Mihomo configuration binding changed")
    return None


def _mihomo_proxy_probe(
    node: Node,
    *,
    runtime_nodes: list[Node],
    core_running: bool,
    client: MihomoDiagnosticClient | None,
    probe_url: str,
    timeout_ms: int,
    core_binding: NodeDiagnosticCoreBinding | None,
    binding_validator: CoreBindingValidator | None,
    proxy_name: str | None,
    resolution_error: str | None,
    expected_proxy_fingerprint: str | None,
    initial_binding_error: str | None,
) -> NodeDiagnosticProbeResult:
    method = "mihomo_proxy_http"
    if not core_running:
        return _probe_result(method, "unavailable", target=probe_url, error="mihomo core is not running")
    if client is None:
        return _probe_result(method, "unavailable", target=probe_url, error="mihomo controller is unavailable")

    if not proxy_name:
        return _probe_result(method, "unavailable", target=probe_url, error=resolution_error)
    target = f"{proxy_name} -> {probe_url}"
    if initial_binding_error:
        return _probe_result(method, "unavailable", target=target, error=initial_binding_error)

    binding_error = _core_binding_error(
        binding=core_binding,
        validator=binding_validator,
        proxy_name=proxy_name,
        expected_proxy_fingerprint=expected_proxy_fingerprint,
    )
    if binding_error:
        return _probe_result(method, "unavailable", target=target, error=binding_error)

    try:
        inventory = client.proxies()
    except Exception as exc:
        binding_error = _core_binding_error(
            binding=core_binding,
            validator=binding_validator,
            proxy_name=proxy_name,
            expected_proxy_fingerprint=expected_proxy_fingerprint,
        )
        if binding_error:
            return _probe_result(method, "unavailable", target=target, error=binding_error)
        return _probe_result(method, "failed", target=target, error=str(exc) or type(exc).__name__)

    binding_error = _core_binding_error(
        binding=core_binding,
        validator=binding_validator,
        proxy_name=proxy_name,
        expected_proxy_fingerprint=expected_proxy_fingerprint,
    )
    if binding_error:
        return _probe_result(method, "unavailable", target=target, error=binding_error)

    proxies = inventory.get("proxies") if isinstance(inventory, dict) else None
    entry = proxies.get(proxy_name) if isinstance(proxies, dict) else None
    if not isinstance(entry, dict):
        return _probe_result(method, "unavailable", target=target, error="target proxy is not loaded in Mihomo")
    if isinstance(entry.get("all"), list) and entry["all"]:
        return _probe_result(method, "unavailable", target=target, error="resolved Mihomo target is a proxy group")

    try:
        payload = client.proxy_delay(proxy_name, url=probe_url, timeout_ms=timeout_ms)
    except Exception as exc:
        binding_error = _core_binding_error(
            binding=core_binding,
            validator=binding_validator,
            proxy_name=proxy_name,
            expected_proxy_fingerprint=expected_proxy_fingerprint,
        )
        if binding_error:
            return _probe_result(method, "unavailable", target=target, error=binding_error)
        return _probe_result(method, "failed", target=target, error=str(exc) or type(exc).__name__)

    binding_error = _core_binding_error(
        binding=core_binding,
        validator=binding_validator,
        proxy_name=proxy_name,
        expected_proxy_fingerprint=expected_proxy_fingerprint,
    )
    if binding_error:
        return _probe_result(method, "unavailable", target=target, error=binding_error)
    try:
        delay = payload.get("delay") if isinstance(payload, dict) else None
        latency_ms = float(delay)
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("mihomo returned an invalid delay")
    except Exception as exc:
        return _probe_result(method, "failed", target=target, error=str(exc) or type(exc).__name__)
    return _probe_result(method, "success", target=target, latency_ms=round(latency_ms, 2))


def diagnose_node_snapshot(
    node: Node,
    *,
    node_identity: str,
    runtime_nodes: list[Node],
    core_running: bool,
    mihomo_client: MihomoDiagnosticClient | None,
    probe_url: str,
    timeout_ms: int = 5000,
    probe_mode: DiagnosticProbeMode = "all",
    config_fingerprint: str | None = None,
    core_binding: NodeDiagnosticCoreBinding | None = None,
    binding_validator: CoreBindingValidator | None = None,
    _tcp_connector: TcpConnector = socket.create_connection,
    _monotonic: Callable[[], float] = time.perf_counter,
    _timestamp: Callable[[], str] = utc_now_iso,
    _request_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> NodeDiagnosticResult:
    """Run isolated, read-only probes against an already cloned node snapshot."""
    try:
        effective_timeout_ms = int(timeout_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_ms must be a positive integer") from exc
    if effective_timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    effective_probe_mode = str(probe_mode or "all").strip().lower()
    if effective_probe_mode not in {"all", "direct", "routed"}:
        raise ValueError("probe_mode must be one of: all, direct, routed")

    request_id = _request_id()
    started_at = _timestamp()
    captured_fingerprint = str(config_fingerprint or node_config_fingerprint(node))
    proxy_name: str | None = None
    resolution_error: str | None = None
    expected_proxy_fingerprint: str | None = None
    initial_binding_error: str | None = None
    if effective_probe_mode in {"all", "routed"}:
        proxy_name, resolution_error = _resolve_unique_proxy_name(node, runtime_nodes)
        expected_proxy_fingerprint = node_effective_proxy_config_fingerprint(node)
        if core_running and mihomo_client is not None and proxy_name:
            initial_binding_error = _core_binding_error(
                binding=core_binding,
                validator=binding_validator,
                proxy_name=proxy_name,
                expected_proxy_fingerprint=expected_proxy_fingerprint,
            )
    probes: dict[str, NodeDiagnosticProbeResult] = {}
    if effective_probe_mode in {"all", "direct"}:
        probes["tcp_connect"] = _tcp_connect_probe(
            node,
            timeout_ms=effective_timeout_ms,
            connector=_tcp_connector,
            monotonic=_monotonic,
        )
    if effective_probe_mode in {"all", "routed"}:
        probes["mihomo_proxy_http"] = _mihomo_proxy_probe(
            node,
            runtime_nodes=runtime_nodes,
            core_running=core_running,
            client=mihomo_client,
            probe_url=str(probe_url or ""),
            timeout_ms=effective_timeout_ms,
            core_binding=core_binding,
            binding_validator=binding_validator,
            proxy_name=proxy_name,
            resolution_error=resolution_error,
            expected_proxy_fingerprint=expected_proxy_fingerprint,
            initial_binding_error=initial_binding_error,
        )
    return {
        "request_id": request_id,
        "node_identity": str(node_identity),
        "node_name": str(node.name or node.remarks or node.address or ""),
        "config_fingerprint": captured_fingerprint,
        "started_at": started_at,
        "finished_at": _timestamp(),
        "probes": probes,
    }


# Request-scoped Mihomo diagnostics -------------------------------------------------

_EPHEMERAL_TARGET_NAME = "DIAG-TARGET"
_EPHEMERAL_UPSTREAM_GROUP = "DIAG-UPSTREAM"
_EPHEMERAL_LAUNCH_ATTEMPTS = 3


class _DiagnosticSetupError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize_ephemeral_mode(value: str) -> EphemeralDiagnosticMode:
    normalized = str(value or "direct").strip().lower()
    if normalized in {"all", "direct"}:
        return "direct"
    if normalized in {"routed", "chain"}:
        return "chain"
    raise ValueError("mode must be one of: all, direct, routed, chain")


def _ephemeral_proxy(node: Node, *, name: str) -> dict[str, Any] | None:
    try:
        proxy = node_to_clash_proxy(
            node,
            logger=_SILENT_FORMATTER_LOGGER,
            seen_names=set(),
        )
    except Exception:
        return None
    if not isinstance(proxy, dict):
        return None
    result = dict(proxy)
    result["name"] = name
    # A persisted Exit snapshot may imply the formal PROXY-FALLBACK group. The
    # request-scoped config owns its entire graph and must never retain that
    # formal runtime reference.
    result.pop("dialer-proxy", None)
    return result


def _build_ephemeral_mihomo_config(
    node: Node,
    *,
    mode: EphemeralDiagnosticMode,
    upstream_nodes: list[Node],
    mixed_port: int,
    controller_port: int,
    controller_secret: str,
    probe_url: str,
    timeout_ms: int,
    ipv6: bool,
) -> dict[str, Any]:
    target_proxy = _ephemeral_proxy(node, name=_EPHEMERAL_TARGET_NAME)
    if target_proxy is None:
        raise _DiagnosticSetupError("unsupported_protocol")

    proxies: list[dict[str, Any]] = []
    proxy_groups: list[dict[str, Any]] = []
    if mode == "chain":
        for index, upstream_node in enumerate(upstream_nodes, start=1):
            upstream_proxy = _ephemeral_proxy(
                upstream_node,
                name=f"DIAG-UPSTREAM-{index:03d}",
            )
            if upstream_proxy is not None:
                proxies.append(upstream_proxy)
        if not proxies:
            raise _DiagnosticSetupError("upstream_missing")
        upstream_names = [str(proxy["name"]) for proxy in proxies]
        proxy_groups.append(
            {
                "name": _EPHEMERAL_UPSTREAM_GROUP,
                "type": "fallback",
                "empty-fallback": "REJECT",
                "proxies": upstream_names,
                "url": probe_url,
                "interval": 60,
                "lazy": True,
                "timeout": int(timeout_ms),
            }
        )
        target_proxy["dialer-proxy"] = _EPHEMERAL_UPSTREAM_GROUP

    proxies.append(target_proxy)
    return {
        "mixed-port": int(mixed_port),
        "external-controller": f"127.0.0.1:{int(controller_port)}",
        "secret": controller_secret,
        "bind-address": "127.0.0.1",
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
        "ipv6": bool(ipv6),
        "profile": {
            "store-selected": False,
            "store-fake-ip": False,
        },
        "dns": helper_dns_block(ipv6=bool(ipv6)),
        "proxies": proxies,
        "proxy-groups": proxy_groups,
        "rules": [f"MATCH,{_EPHEMERAL_TARGET_NAME}"],
    }


def _reserve_ephemeral_ports(
    excluded_ports: Iterable[int] = (),
) -> tuple[socket.socket, socket.socket, int, int]:
    excluded = {
        int(port)
        for port in excluded_ports
        if isinstance(port, int) and 1 <= int(port) <= 65535
    }
    reservations: list[socket.socket] = []
    try:
        attempts = 0
        while len(reservations) < 2 and attempts < 64:
            attempts += 1
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                reservation.bind(("127.0.0.1", 0))
                reservation.listen(1)
                candidate = int(reservation.getsockname()[1])
                if candidate in excluded:
                    reservation.close()
                    continue
                excluded.add(candidate)
                reservations.append(reservation)
            except Exception:
                reservation.close()
                raise
        if len(reservations) != 2:
            raise RuntimeError("no isolated diagnostic ports are available")
        mixed_port = int(reservations[0].getsockname()[1])
        controller_port = int(reservations[1].getsockname()[1])
        if mixed_port == controller_port:
            raise RuntimeError("ephemeral port reservation collision")
        return reservations[0], reservations[1], mixed_port, controller_port
    except Exception:
        for reservation in reservations:
            try:
                reservation.close()
            except Exception:
                pass
        raise


def _process_has_exited(process: subprocess.Popen[Any]) -> bool:
    try:
        return process.poll() is not None
    except Exception:
        return False


_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_INFINITE = 0xFFFFFFFF
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESHOWWINDOW = 0x00000001
_STARTF_USESTDHANDLES = 0x00000100
_SW_HIDE = 0
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080


class _WindowsSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _WindowsStartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _WindowsStartupInfoEx(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _WindowsStartupInfo),
        ("lpAttributeList", wintypes.LPVOID),
    ]


class _WindowsProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


if IS_WINDOWS:
    _diagnostic_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _diagnostic_kernel32.InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _diagnostic_kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _diagnostic_kernel32.UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _diagnostic_kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _diagnostic_kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    _diagnostic_kernel32.DeleteProcThreadAttributeList.restype = None
    _diagnostic_kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_WindowsStartupInfo),
        ctypes.POINTER(_WindowsProcessInformation),
    ]
    _diagnostic_kernel32.CreateProcessW.restype = wintypes.BOOL
    _diagnostic_kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_WindowsSecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _diagnostic_kernel32.CreateFileW.restype = wintypes.HANDLE
    _diagnostic_kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _diagnostic_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _diagnostic_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _diagnostic_kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _diagnostic_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _diagnostic_kernel32.TerminateProcess.restype = wintypes.BOOL
    _diagnostic_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _diagnostic_kernel32.CloseHandle.restype = wintypes.BOOL


def _windows_error(action: str) -> OSError:
    code = int(ctypes.get_last_error() or 1)
    return OSError(code, f"{action} failed", None, code)


class _WindowsNativeProcess:
    """Small Popen-compatible wrapper around one native process handle."""

    def __init__(self, handle: int, pid: int, command: list[str]) -> None:
        self._handle = int(handle)
        self.pid = int(pid)
        self._command = list(command)
        self._lock = threading.RLock()
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if not IS_WINDOWS:
            return self._returncode
        with self._lock:
            handle = int(self._handle or 0)
            if not handle:
                return self._returncode
            code = wintypes.DWORD()
            if not _diagnostic_kernel32.GetExitCodeProcess(
                wintypes.HANDLE(handle),
                ctypes.byref(code),
            ):
                raise _windows_error("GetExitCodeProcess")
            if int(code.value) == _STILL_ACTIVE:
                return None
            self._returncode = int(code.value)
            return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if not IS_WINDOWS:
            return int(self._returncode or 0)
        with self._lock:
            handle = int(self._handle or 0)
            if not handle:
                return int(self._returncode or 0)
            if timeout is None:
                timeout_ms = _INFINITE
            else:
                timeout_ms = max(0, min(_INFINITE - 1, int(float(timeout) * 1000.0)))
            waited = int(
                _diagnostic_kernel32.WaitForSingleObject(
                    wintypes.HANDLE(handle),
                    timeout_ms,
                )
            )
            if waited == _WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(self._command, timeout)
            if waited != _WAIT_OBJECT_0:
                raise _windows_error("WaitForSingleObject")
            result = self.poll()
            return int(result or 0)

    def terminate(self) -> None:
        self._terminate(75)

    def kill(self) -> None:
        self._terminate(76)

    def _terminate(self, exit_code: int) -> None:
        if not IS_WINDOWS:
            return
        with self._lock:
            handle = int(self._handle or 0)
            if not handle or self.poll() is not None:
                return
            if not _diagnostic_kernel32.TerminateProcess(
                wintypes.HANDLE(handle),
                int(exit_code),
            ):
                raise _windows_error("TerminateProcess")

    def close_handle(self) -> bool:
        if not IS_WINDOWS:
            return True
        with self._lock:
            handle = int(self._handle or 0)
            if not handle:
                return True
            if self.poll() is None:
                return False
            if not _diagnostic_kernel32.CloseHandle(wintypes.HANDLE(handle)):
                return False
            self._handle = 0
            return True

    def __del__(self) -> None:
        try:
            with self._lock:
                handle = int(self._handle or 0)
                self._handle = 0
            if IS_WINDOWS and handle:
                _diagnostic_kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass


def _open_inheritable_null_handle(access: int) -> int:
    attributes = _WindowsSecurityAttributes()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.bInheritHandle = True
    raw = _diagnostic_kernel32.CreateFileW(
        "NUL",
        int(access),
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        ctypes.byref(attributes),
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    value = int(raw or 0)
    invalid = int(ctypes.c_void_p(-1).value or -1)
    if not value or value == invalid:
        raise _windows_error("CreateFileW(NUL)")
    return value


def _create_windows_process_in_job(
    command: list[str],
    *,
    cwd: str,
    job_handle: int,
) -> _WindowsNativeProcess:
    """Create Mihomo already assigned to its request Job, never after the fact."""
    if not IS_WINDOWS:
        raise OSError("atomic Windows process ownership is unavailable")
    if not command or not int(job_handle or 0):
        raise OSError("atomic Windows process ownership is unavailable")

    attribute_buffer: ctypes.Array[Any] | None = None
    attribute_list: wintypes.LPVOID | None = None
    attributes_initialized = False
    null_handles: list[int] = []
    process_info = _WindowsProcessInformation()
    try:
        null_input = _open_inheritable_null_handle(_GENERIC_READ)
        null_handles.append(null_input)
        null_output = _open_inheritable_null_handle(_GENERIC_WRITE)
        null_handles.append(null_output)

        attribute_size = ctypes.c_size_t()
        _diagnostic_kernel32.InitializeProcThreadAttributeList(
            None,
            2,
            0,
            ctypes.byref(attribute_size),
        )
        if int(attribute_size.value) <= 0:
            raise _windows_error("InitializeProcThreadAttributeList(size)")
        attribute_buffer = ctypes.create_string_buffer(int(attribute_size.value))
        attribute_list = ctypes.cast(attribute_buffer, wintypes.LPVOID)
        if not _diagnostic_kernel32.InitializeProcThreadAttributeList(
            attribute_list,
            2,
            0,
            ctypes.byref(attribute_size),
        ):
            raise _windows_error("InitializeProcThreadAttributeList")
        attributes_initialized = True

        job_value = wintypes.HANDLE(int(job_handle))
        if not _diagnostic_kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.byref(job_value),
            ctypes.sizeof(job_value),
            None,
            None,
        ):
            raise _windows_error("UpdateProcThreadAttribute(job_list)")

        inherited_values = (wintypes.HANDLE * 2)(
            wintypes.HANDLE(null_input),
            wintypes.HANDLE(null_output),
        )
        if not _diagnostic_kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.cast(inherited_values, wintypes.LPVOID),
            ctypes.sizeof(inherited_values),
            None,
            None,
        ):
            raise _windows_error("UpdateProcThreadAttribute(handle_list)")

        startup = _WindowsStartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESHOWWINDOW | _STARTF_USESTDHANDLES
        startup.StartupInfo.wShowWindow = _SW_HIDE
        startup.StartupInfo.hStdInput = wintypes.HANDLE(null_input)
        startup.StartupInfo.hStdOutput = wintypes.HANDLE(null_output)
        startup.StartupInfo.hStdError = wintypes.HANDLE(null_output)
        startup.lpAttributeList = attribute_list

        executable = str(Path(command[0]).resolve())
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        creation_flags = (
            _CREATE_UNICODE_ENVIRONMENT
            | _EXTENDED_STARTUPINFO_PRESENT
            | _CREATE_NO_WINDOW
        )
        if not _diagnostic_kernel32.CreateProcessW(
            executable,
            command_line,
            None,
            None,
            True,
            creation_flags,
            None,
            str(Path(cwd).resolve()),
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_info),
        ):
            raise _windows_error("CreateProcessW(job_list)")
        process_handle = int(process_info.hProcess or 0)
        process_info.hProcess = None
        return _WindowsNativeProcess(
            process_handle,
            int(process_info.dwProcessId),
            command,
        )
    finally:
        thread_handle = int(process_info.hThread or 0)
        if thread_handle:
            try:
                _diagnostic_kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
            except Exception:
                pass
        process_handle = int(process_info.hProcess or 0)
        if process_handle:
            try:
                _diagnostic_kernel32.TerminateProcess(
                    wintypes.HANDLE(process_handle),
                    77,
                )
            except Exception:
                pass
            try:
                _diagnostic_kernel32.CloseHandle(wintypes.HANDLE(process_handle))
            except Exception:
                pass
        if attributes_initialized and attribute_list is not None:
            try:
                _diagnostic_kernel32.DeleteProcThreadAttributeList(attribute_list)
            except Exception:
                pass
        for handle in null_handles:
            try:
                _diagnostic_kernel32.CloseHandle(wintypes.HANDLE(handle))
            except Exception:
                pass


def _destroy_ephemeral_process(
    process: subprocess.Popen[Any] | None,
    *,
    terminate_timeout: float = 0.75,
    kill_timeout: float = 0.75,
) -> bool:
    """Reap one request process, escalating terminate -> wait -> kill -> wait."""
    if process is None:
        return True
    if _process_has_exited(process):
        try:
            process.wait(timeout=max(0.01, terminate_timeout))
        except Exception:
            pass
        return _process_has_exited(process)
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=max(0.01, terminate_timeout))
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=max(0.01, kill_timeout))
        except Exception:
            pass
    return _process_has_exited(process)


class _EphemeralOwnerClosed(RuntimeError):
    pass


class _PortablePopenProcessOwner:
    """Injectable/non-Windows owner with cancellation-safe launch publication."""

    def __init__(self, process_factory: Callable[..., EphemeralProcess]) -> None:
        self._process_factory = process_factory
        self._lock = threading.RLock()
        self._process: EphemeralProcess | None = None
        self._launching = False
        self._sealed = False

    def launch(self, command: list[str], **kwargs: Any) -> EphemeralProcess:
        with self._lock:
            if self._sealed or self._launching or self._process is not None:
                raise _EphemeralOwnerClosed("process owner is sealed")
            self._launching = True
        try:
            process = self._process_factory(command, **kwargs)
        except Exception:
            with self._lock:
                self._launching = False
            raise
        with self._lock:
            self._process = process
            self._launching = False
            sealed = self._sealed
        if sealed:
            self.terminate_and_wait()
            raise _EphemeralOwnerClosed("process owner was cancelled during launch")
        return process

    def terminate_and_wait(self) -> bool:
        with self._lock:
            self._sealed = True
            if self._launching:
                return False
            process = self._process
        stopped = _destroy_ephemeral_process(process)
        with self._lock:
            if stopped and self._process is process:
                self._process = None
            return not self._launching and self._process is None

    def resources_released(self) -> bool:
        with self._lock:
            return not self._launching and self._process is None


class _WindowsAtomicJobProcessOwner:
    """Create the child inside a non-inheritable KILL_ON_JOB_CLOSE Job."""

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise OSError("atomic Windows process ownership is unavailable")
        self._lock = threading.RLock()
        self._job: WindowsJob | None = WindowsJob()
        self._process: _WindowsNativeProcess | None = None
        self._launching = False
        self._sealed = False

    def launch(self, command: list[str], **kwargs: Any) -> EphemeralProcess:
        cwd = str(kwargs.get("cwd") or "")
        with self._lock:
            job = self._job
            if (
                self._sealed
                or self._launching
                or self._process is not None
                or job is None
            ):
                raise _EphemeralOwnerClosed("process owner is sealed")
            launch_job = job.duplicate()
            self._launching = True
        try:
            process = _create_windows_process_in_job(
                command,
                cwd=cwd,
                job_handle=int(launch_job.handle),
            )
        except Exception:
            with self._lock:
                self._launching = False
            raise
        finally:
            if "process" not in locals():
                try:
                    launch_job.close()
                except Exception:
                    pass

        with self._lock:
            self._process = process
            self._launching = False
            sealed = self._sealed
        if sealed:
            self.terminate_and_wait()
            try:
                launch_job.close()
            except Exception:
                pass
            raise _EphemeralOwnerClosed("process owner was cancelled during launch")
        try:
            launch_job.close()
        except Exception:
            # The main owner handle is still authoritative. A duplicate close
            # failure cannot permit the request to publish an unowned process.
            self.terminate_and_wait()
            raise OSError("diagnostic Job duplicate did not close") from None
        return process

    def terminate_and_wait(self) -> bool:
        with self._lock:
            self._sealed = True
            if self._launching:
                return False
            job = self._job
            process = self._process

        if job is not None:
            try:
                job.terminate(exit_code=78)
            except Exception:
                pass
        process_stopped = _destroy_ephemeral_process(process)
        job_empty = True
        if job is not None:
            try:
                job_empty = bool(job.wait_empty(1.5))
            except Exception:
                job_empty = False
        handle_closed = True
        if process_stopped and process is not None:
            try:
                handle_closed = bool(process.close_handle())
            except Exception:
                handle_closed = False
        if job_empty and process_stopped and handle_closed and job is not None:
            try:
                job.close()
            except Exception:
                job_empty = False

        with self._lock:
            if process_stopped and handle_closed and self._process is process:
                self._process = None
            if job_empty and process_stopped and handle_closed and self._job is job:
                self._job = None
            return (
                not self._launching
                and self._process is None
                and self._job is None
            )

    def resources_released(self) -> bool:
        with self._lock:
            return (
                not self._launching
                and self._process is None
                and self._job is None
            )


def _cleanup_ephemeral_workdir(path: Path | None) -> bool:
    """Best-effort recursive cleanup that never lets filesystem errors escape."""
    if path is None:
        return True

    def absent() -> bool:
        try:
            return not path.exists()
        except Exception:
            return False

    if absent():
        return True
    for attempt in range(3):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except Exception:
            pass
        if absent():
            return True
        if attempt < 2:
            try:
                time.sleep(0.05)
            except Exception:
                pass
    return absent()


def _diagnostic_request_tag(request_id: str) -> str:
    return hashlib.sha256(str(request_id).encode("utf-8", errors="replace")).hexdigest()[:12]


def _log_ephemeral_stage(
    logger: logging.Logger,
    *,
    request_tag: str,
    mode: EphemeralDiagnosticMode,
    stage: str,
    reason: str = "",
    elapsed_ms: float | None = None,
) -> None:
    # Never include node names, addresses, URLs, controller endpoints, secrets,
    # configuration payloads, or raw exception text in this diagnostic channel.
    fields = [
        f"request={request_tag}",
        f"mode={mode}",
        f"stage={stage}",
    ]
    if reason:
        fields.append(f"reason={reason}")
    if elapsed_ms is not None:
        fields.append(f"elapsed_ms={max(0, int(round(elapsed_ms)))}")
    logger.info("Ephemeral node diagnostic %s", " ".join(fields))


def _new_ephemeral_mihomo_client(
    *,
    base_url: str,
    secret: str,
    timeout: float,
) -> EphemeralMihomoDiagnosticClient:
    # The request-owned controller is loopback-only. Explicitly bypass process
    # and system proxy settings so its secret can never leave the machine.
    local_opener = build_opener(ProxyHandler({})).open
    return MihomoAPIClient(
        base_url=base_url,
        secret=secret,
        timeout=timeout,
        opener=local_opener,
    )


class _EphemeralDiagnosticRequest:
    def __init__(self, request_id: str, mode: EphemeralDiagnosticMode) -> None:
        self.request_id = request_id
        self.mode = mode
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        self.cleanup_complete_event = threading.Event()
        self._resource_lock = threading.RLock()
        self._owner: EphemeralProcessOwner | None = None
        self._work_dir: Path | None = None

    def attach_work_dir(self, work_dir: Path) -> bool:
        with self._resource_lock:
            self._work_dir = work_dir
            cancelled = self.cancel_event.is_set()
        if cancelled:
            self.cleanup_resources()
            return False
        return True

    def attach_owner(self, owner: EphemeralProcessOwner) -> bool:
        with self._resource_lock:
            if self._owner is not None:
                return False
            self._owner = owner
            cancelled = self.cancel_event.is_set()
        if cancelled:
            self.release_owner_after_attempt()
            return False
        return True

    def release_owner_after_attempt(self) -> bool:
        """Reap one failed launch while retaining the request work directory."""
        try:
            with self._resource_lock:
                owner = self._owner
                if owner is None:
                    return True
                released = bool(owner.terminate_and_wait())
                if released and owner.resources_released() and self._owner is owner:
                    self._owner = None
                    return True
                return False
        except Exception:
            return False

    def cleanup_resources(self) -> bool:
        """No-throw cleanup; the request retains every resource that remains."""
        try:
            with self._resource_lock:
                owner = self._owner
                if owner is not None:
                    released = bool(owner.terminate_and_wait())
                    if released and owner.resources_released() and self._owner is owner:
                        self._owner = None
                    else:
                        return False
                work_dir = self._work_dir
                if not _cleanup_ephemeral_workdir(work_dir):
                    return False
                if self._work_dir is work_dir:
                    self._work_dir = None
                self.cleanup_complete_event.set()
                return True
        except Exception:
            return False

    def resources_released(self) -> bool:
        try:
            with self._resource_lock:
                owner = self._owner
                owner_released = owner is None or bool(owner.resources_released())
                return bool(
                    owner_released
                    and self._owner is None
                    and self._work_dir is None
                )
        except Exception:
            return False

    def cancel(self) -> bool:
        already_cancelled = self.cancel_event.is_set()
        self.cancel_event.set()
        self.cleanup_resources()
        return not already_cancelled


class EphemeralNodeDiagnosticService:
    """Owns one strictly isolated Mihomo process per manual node request.

    This service deliberately accepts no formal Core runner, controller client,
    runtime config path, or Manager object. The only network fact it publishes
    is Mihomo's proxy ``/delay`` result from the request-owned controller.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        process_owner_factory: ProcessOwnerFactory | None = None,
        client_factory: Callable[..., EphemeralMihomoDiagnosticClient] = _new_ephemeral_mihomo_client,
        timestamp: Callable[[], str] = utc_now_iso,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger or logging.getLogger("tesseract.node-diagnostic.ephemeral")
        self._process_factory = process_factory
        self._process_owner_factory = process_owner_factory
        self._client_factory = client_factory
        self._timestamp = timestamp
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._active: _EphemeralDiagnosticRequest | None = None
        self._closed = False

    def _new_process_owner(self) -> EphemeralProcessOwner:
        if self._process_owner_factory is not None:
            return self._process_owner_factory()
        if IS_WINDOWS and self._process_factory is subprocess.Popen:
            # Production Windows diagnostics must enter their Job at process
            # creation. Failure is intentionally fail-closed; never fall back
            # to an ordinary Popen that can outlive the request.
            return _WindowsAtomicJobProcessOwner()
        return _PortablePopenProcessOwner(self._process_factory)

    def _immediate_result(
        self,
        *,
        request_id: str,
        node_identity: str,
        fingerprint: str,
        mode: EphemeralDiagnosticMode,
        started_at: str,
        reason: str,
    ) -> EphemeralNodeDiagnosticResult:
        return {
            "request_id": request_id,
            "node_identity": str(node_identity),
            "config_fingerprint": fingerprint,
            "mode": mode,
            "started_at": started_at,
            "completed_at": self._timestamp(),
            "state": "failed",
            "latency_ms": None,
            "reason": reason,
        }

    def diagnose(
        self,
        node: Node,
        *,
        node_identity: str,
        binary_path: str | Path,
        probe_url: str,
        request_id: str | None = None,
        mode: str = "direct",
        upstream_nodes: list[Node] | tuple[Node, ...] = (),
        timeout_ms: int = 5000,
        startup_timeout: float = 8.0,
        ipv6: bool = False,
        temp_parent: str | Path | None = None,
        excluded_ports: Iterable[int] = (),
    ) -> EphemeralNodeDiagnosticResult:
        effective_mode = _normalize_ephemeral_mode(mode)
        effective_request_id = str(request_id or uuid.uuid4())
        started_at = self._timestamp()
        started_monotonic = self._monotonic()
        fingerprint = node_config_fingerprint(node)
        request_tag = _diagnostic_request_tag(effective_request_id)
        try:
            effective_timeout_ms = int(timeout_ms)
            effective_startup_timeout = float(startup_timeout)
        except (TypeError, ValueError):
            return self._immediate_result(
                request_id=effective_request_id,
                node_identity=node_identity,
                fingerprint=fingerprint,
                mode=effective_mode,
                started_at=started_at,
                reason="invalid_request",
            )
        if (
            effective_timeout_ms <= 0
            or effective_startup_timeout <= 0
            or not str(probe_url or "").strip()
        ):
            return self._immediate_result(
                request_id=effective_request_id,
                node_identity=node_identity,
                fingerprint=fingerprint,
                mode=effective_mode,
                started_at=started_at,
                reason="invalid_request",
            )

        binary = Path(binary_path).expanduser().resolve()
        if not binary.is_file():
            return self._immediate_result(
                request_id=effective_request_id,
                node_identity=node_identity,
                fingerprint=fingerprint,
                mode=effective_mode,
                started_at=started_at,
                reason="component_missing",
            )

        request = _EphemeralDiagnosticRequest(effective_request_id, effective_mode)
        with self._lock:
            if self._closed:
                return self._immediate_result(
                    request_id=effective_request_id,
                    node_identity=node_identity,
                    fingerprint=fingerprint,
                    mode=effective_mode,
                    started_at=started_at,
                    reason="service_closed",
                )
            if self._active is not None:
                return self._immediate_result(
                    request_id=effective_request_id,
                    node_identity=node_identity,
                    fingerprint=fingerprint,
                    mode=effective_mode,
                    started_at=started_at,
                    reason="diagnostic_in_progress",
                )
            self._active = request

        state: EphemeralDiagnosticState = "failed"
        reason = "probe_failed"
        latency_ms: float | None = None
        reservations: list[socket.socket] = []
        work_dir: Path | None = None
        work_dir_attached = False
        process_started = False
        cleanup_ok = True
        try:
            _log_ephemeral_stage(
                self._logger,
                request_tag=request_tag,
                mode=effective_mode,
                stage="started",
            )
            if request.cancel_event.is_set():
                raise _DiagnosticSetupError("cancelled")

            upstream_snapshots = [upstream.clone() for upstream in upstream_nodes]
            node_snapshot = node.clone()
            parent = Path(temp_parent).resolve() if temp_parent is not None else None
            if parent is not None:
                parent.mkdir(parents=True, exist_ok=True)
            work_dir = Path(
                tempfile.mkdtemp(
                    prefix="pproxy-node-diagnostic-",
                    dir=str(parent) if parent is not None else None,
                )
            ).resolve()
            if not request.attach_work_dir(work_dir):
                raise _DiagnosticSetupError("cancelled")
            work_dir_attached = True
            config_path = work_dir / "diagnostic.yaml"
            client: EphemeralMihomoDiagnosticClient | None = None
            for launch_attempt in range(_EPHEMERAL_LAUNCH_ATTEMPTS):
                if request.cancel_event.is_set():
                    raise _DiagnosticSetupError("cancelled")
                mixed_socket, controller_socket, mixed_port, controller_port = (
                    _reserve_ephemeral_ports(excluded_ports)
                )
                reservations.extend([mixed_socket, controller_socket])
                controller_secret = secrets.token_urlsafe(32)
                config = _build_ephemeral_mihomo_config(
                    node_snapshot,
                    mode=effective_mode,
                    upstream_nodes=upstream_snapshots,
                    mixed_port=mixed_port,
                    controller_port=controller_port,
                    controller_secret=controller_secret,
                    probe_url=str(probe_url).strip(),
                    timeout_ms=effective_timeout_ms,
                    ipv6=bool(ipv6),
                )
                config_path.write_text(
                    yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                try:
                    owner = self._new_process_owner()
                except Exception:
                    raise _DiagnosticSetupError("startup_failed") from None
                if not request.attach_owner(owner):
                    raise _DiagnosticSetupError("cancelled")
                for reservation in reservations:
                    reservation.close()
                reservations.clear()
                if request.cancel_event.is_set():
                    raise _DiagnosticSetupError("cancelled")

                try:
                    process = owner.launch(
                        [str(binary), "-f", str(config_path), "-d", str(work_dir)],
                        cwd=str(work_dir),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **hidden_subprocess_kwargs(),
                    )
                except Exception:
                    if not request.release_owner_after_attempt():
                        raise _DiagnosticSetupError("cleanup_failed") from None
                    raise _DiagnosticSetupError(
                        "cancelled" if request.cancel_event.is_set() else "startup_failed"
                    ) from None
                process_started = True
                client = self._client_factory(
                    base_url=f"http://127.0.0.1:{controller_port}",
                    secret=controller_secret,
                    timeout=max(0.2, min(1.0, effective_startup_timeout)),
                )
                readiness_deadline = self._monotonic() + effective_startup_timeout
                retry_launch = False
                while True:
                    if request.cancel_event.is_set():
                        raise _DiagnosticSetupError("cancelled")
                    if _process_has_exited(process):
                        if launch_attempt + 1 < _EPHEMERAL_LAUNCH_ATTEMPTS:
                            if not request.release_owner_after_attempt():
                                raise _DiagnosticSetupError("cleanup_failed")
                            retry_launch = True
                            break
                        raise _DiagnosticSetupError("startup_failed")
                    try:
                        client.version()
                        break
                    except Exception:
                        if self._monotonic() >= readiness_deadline:
                            raise _DiagnosticSetupError("startup_failed")
                        request.cancel_event.wait(0.05)
                if retry_launch:
                    continue
                break
            if client is None:
                raise _DiagnosticSetupError("startup_failed")

            _log_ephemeral_stage(
                self._logger,
                request_tag=request_tag,
                mode=effective_mode,
                stage="ready",
            )
            if request.cancel_event.is_set():
                raise _DiagnosticSetupError("cancelled")
            try:
                payload = client.proxy_delay(
                    _EPHEMERAL_TARGET_NAME,
                    url=str(probe_url).strip(),
                    timeout_ms=effective_timeout_ms,
                )
            except Exception as exc:
                if request.cancel_event.is_set():
                    raise _DiagnosticSetupError("cancelled") from None
                text = str(exc).lower()
                if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text or "timeout" in text:
                    raise _DiagnosticSetupError("timeout") from None
                raise _DiagnosticSetupError("probe_failed") from None
            if request.cancel_event.is_set():
                raise _DiagnosticSetupError("cancelled")
            try:
                raw_delay = payload.get("delay") if isinstance(payload, dict) else None
                measured_delay = float(raw_delay)
                if not math.isfinite(measured_delay) or measured_delay < 0:
                    raise ValueError("invalid delay")
            except (TypeError, ValueError, OverflowError):
                raise _DiagnosticSetupError("probe_failed") from None
            state = "success"
            reason = "ok"
            latency_ms = round(measured_delay, 2)
        except _DiagnosticSetupError as exc:
            reason = exc.reason
            state = "cancelled" if reason == "cancelled" else "failed"
            latency_ms = None
        except Exception:
            reason = "cancelled" if request.cancel_event.is_set() else (
                "startup_failed" if process_started else "setup_failed"
            )
            state = "cancelled" if reason == "cancelled" else "failed"
            latency_ms = None
        finally:
            try:
                for reservation in reservations:
                    try:
                        reservation.close()
                    except Exception:
                        cleanup_ok = False
                try:
                    if not request.cleanup_resources():
                        cleanup_ok = False
                except Exception:
                    cleanup_ok = False
                # The directory is attached immediately after mkdtemp. Keep a
                # no-throw fallback only for an exception inside that handoff.
                if not work_dir_attached and not _cleanup_ephemeral_workdir(work_dir):
                    cleanup_ok = False
                if not cleanup_ok:
                    state = "failed"
                    reason = "cleanup_failed"
                    latency_ms = None
                try:
                    elapsed_ms = (self._monotonic() - started_monotonic) * 1000.0
                    _log_ephemeral_stage(
                        self._logger,
                        request_tag=request_tag,
                        mode=effective_mode,
                        stage="completed",
                        reason=reason,
                        elapsed_ms=elapsed_ms,
                    )
                except Exception:
                    pass
            finally:
                request.done_event.set()
                if request.resources_released():
                    with self._lock:
                        if self._active is request:
                            self._active = None

        return {
            "request_id": effective_request_id,
            "node_identity": str(node_identity),
            "config_fingerprint": fingerprint,
            "mode": effective_mode,
            "started_at": started_at,
            "completed_at": self._timestamp(),
            "state": state,
            "latency_ms": latency_ms,
            "reason": reason,
        }

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            request = self._active
        if request is None or request.request_id != str(request_id):
            return False
        request.cancel()
        return True

    def cancel_all(self) -> int:
        with self._lock:
            request = self._active
        if request is None:
            return 0
        request.cancel()
        return 1

    def _release_completed_request_if_clean(
        self,
        request: _EphemeralDiagnosticRequest,
    ) -> bool:
        if not request.done_event.is_set() or not request.resources_released():
            return False
        with self._lock:
            if self._active is request:
                self._active = None
            return self._active is not request

    def close(self, *, timeout: float = 3.0) -> bool:
        with self._lock:
            self._closed = True
            request = self._active
        if request is None:
            return True
        request.cancel()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                request.cleanup_resources()
            except Exception:
                pass
            if self._release_completed_request_if_clean(request):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            request.done_event.wait(min(0.05, remaining))
