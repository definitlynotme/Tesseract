from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import math
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from clash_formatter import node_to_clash_proxy
from dns_resolver_policy import helper_dns_block
from config_utils import coerce_bool
from log_event_reports import log_raw_diagnostic
from mihomo_api import MihomoAPIClient, MihomoAPIError, MihomoDelayTestFailed
from models import Node, NodeStatus, sort_nodes_for_use, utc_now_iso
from runtime_settings import (
    helper_block_from_testing,
    node_test_concurrency_from_testing,
    node_test_tcp_timeout_seconds,
    primary_gateway_max_latency_ms_from_testing,
    slow_threshold_ms_from_testing,
)
from tier3_validation_config import build_single_proxy_validation_config
from tier3_tcp_dns import Tier3TcpDnsBusy
from windows_subprocess import hidden_subprocess_kwargs


def _config_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if minimum is not None:
        parsed = max(float(minimum), parsed)
    return parsed


def _config_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(int(minimum), parsed)
    if maximum is not None:
        parsed = min(int(maximum), parsed)
    return parsed


def _healthy_status(latency_ms: float, slow_threshold_ms: int) -> NodeStatus:
    return NodeStatus.SLOW if latency_ms > slow_threshold_ms else NodeStatus.ALIVE


_STRICT_LATENCY_TIERS = {"tier1", "tier2", "exit"}
_LATENCY_POLICY_EXTRA_KEYS = (
    "latency_policy_state",
    "latency_policy_limit_ms",
    "latency_policy_observed_ms",
    "latency_policy_scope",
)


def _clear_latency_policy_extra(node: Node) -> None:
    for key in _LATENCY_POLICY_EXTRA_KEYS:
        node.extra.pop(key, None)


def apply_success_latency_policy(
    node: Node,
    latency_ms: float,
    testing_config: dict[str, Any] | None,
    scope: str,
    *,
    slow_threshold_ms: int | None = None,
) -> Node:
    """Classify one successful formal latency measurement.

    Tier1, Tier2, and Gateway (``exit``) use the primary/Gateway hard limit.
    Tier3 and unknown tiers intentionally retain the legacy slow threshold.
    The returned node is a clone so callers can safely reuse this contract for
    both scheduled checks and user-triggered checks.
    """

    updated = node.clone()
    measured_ms = float(latency_ms)
    observed_ms = round(measured_ms, 2)
    testing = testing_config if isinstance(testing_config, dict) else {}
    tier = str(updated.tier or "").strip().lower()

    _clear_latency_policy_extra(updated)
    updated.latency_ms = observed_ms
    updated.suspicious_reason = None

    if tier not in _STRICT_LATENCY_TIERS:
        legacy_slow_threshold_ms = (
            int(slow_threshold_ms)
            if slow_threshold_ms is not None
            else slow_threshold_ms_from_testing(testing)
        )
        updated.status = _healthy_status(measured_ms, legacy_slow_threshold_ms)
        return updated

    limit_ms = primary_gateway_max_latency_ms_from_testing(testing)
    if measured_ms > limit_ms:
        updated.status = NodeStatus.DEAD
        updated.suspicious_reason = "latency_limit_exceeded"
        updated.extra["latency_policy_state"] = "over_limit"
        updated.extra["latency_policy_limit_ms"] = int(limit_ms)
        updated.extra["latency_policy_observed_ms"] = observed_ms
        updated.extra["latency_policy_scope"] = str(scope or "unknown")
    elif measured_ms > (float(limit_ms) / 2.0):
        updated.status = NodeStatus.SLOW
    else:
        updated.status = NodeStatus.ALIVE
    return updated


def _is_executor_shutdown_error(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "cannot schedule new futures" in str(exc).lower()


@dataclass(frozen=True)
class TcpProbeCollectionOutcome:
    nodes: list[Node]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Tier3TcpScanOutcome:
    nodes: list[Node]
    metadata: dict[str, Any]


@dataclass(slots=True)
class Tier3TcpProbeEntitlement:
    """Per-item TCP ownership and timing, independent of wave collection."""

    node: Node
    dispatched_at: float = field(default_factory=time.monotonic)
    transport_started_at: float = 0.0
    envelope_revision: int = 0
    entitlement_seconds: float = 0.0
    transport_deadline: float = 0.0
    delivery_grace_deadline: float = 0.0
    completed_at: float = 0.0
    entitlement_exhausted: bool = False
    late_completion_ignored: bool = False
    start_acknowledged: bool = False
    outcome_state: str = ""

    @property
    def deadline_at(self) -> float:
        """One-version alias for callers that still read the transport deadline."""

        return self.transport_deadline

    def start_transport(
        self,
        entitlement_seconds: float,
        *,
        delivery_grace_seconds: float = 0.25,
        started_at: float | None = None,
        envelope_revision: int = 0,
    ) -> None:
        self.transport_started_at = (
            time.monotonic() if started_at is None else float(started_at)
        )
        self.envelope_revision = max(0, int(envelope_revision))
        self.entitlement_seconds = max(
            0.001,
            float(entitlement_seconds),
        )
        self.transport_deadline = (
            self.transport_started_at + self.entitlement_seconds
        )
        self.delivery_grace_deadline = self.transport_deadline + max(
            0.0,
            min(0.25, float(delivery_grace_seconds)),
        )


@dataclass(frozen=True)
class _MihomoRealProbeAttemptResult:
    probe_url: str
    success: bool
    status_code: int | None
    body_ok: bool
    latency_ms: float
    failure: str


@dataclass(frozen=True)
class _MihomoProbeAttemptFact:
    attempt_id: str
    generation_id: str
    candidate_fingerprint: str
    session_lease_id: str
    endpoint: str
    stage: str
    transport_started_at: float
    granted_budget_seconds: float
    lifecycle: str
    result: str
    failure_domain: str
    failure: str
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "session_lease_id": self.session_lease_id,
            "endpoint": self.endpoint,
            "stage": self.stage,
            "transport_started_at": round(self.transport_started_at, 6),
            "granted_budget_seconds": round(self.granted_budget_seconds, 3),
            "lifecycle": self.lifecycle,
            "result": self.result,
            "failure_domain": self.failure_domain,
            "failure": self.failure,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass(frozen=True)
class _MihomoRealProbeOutcome:
    result: _MihomoRealProbeAttemptResult | None
    failures: list[str]
    failures_by_url: dict[str, str]
    attempted_urls: list[str]
    elapsed_ms: int
    deadline_hit: bool
    probe_sequence_starved: bool
    late_completed_ignored: int
    next_probe_index: int
    total_probe_count: int
    exposure_ms_by_url: dict[str, float] = field(default_factory=dict)
    completed_urls: list[str] = field(default_factory=list)
    exhausted_urls: list[str] = field(default_factory=list)
    censored_urls: list[str] = field(default_factory=list)
    attempt_facts: list[_MihomoProbeAttemptFact] = field(default_factory=list)
    settled_in_cleanup_count: int = 0

    @property
    def started_urls(self) -> list[str]:
        """Compatibility-safe name for probes whose transport thread started."""

        return list(self.attempted_urls)


@dataclass
class _MihomoProbeEndpointState:
    started: int = 0
    attempts: int = 0
    successes: int = 0
    transients: int = 0
    censored: int = 0
    exposure_ms: float = 0.0
    roots: set[str] = field(default_factory=set)
    protocols: set[str] = field(default_factory=set)
    cooldown_until: float = 0.0
    half_open: bool = False


class _MihomoProbeEndpointHealth:
    """Session-local probe facility state; it never classifies candidates or sources."""

    def __init__(
        self,
        *,
        minimum_attempts: int = 8,
        transient_threshold: float = 0.80,
        cooldown_seconds: float = 10.0,
    ) -> None:
        self.minimum_attempts = max(1, int(minimum_attempts))
        self.transient_threshold = max(0.0, min(1.0, float(transient_threshold)))
        self.cooldown_seconds = max(0.1, float(cooldown_seconds))
        self._states: dict[str, _MihomoProbeEndpointState] = {}

    @staticmethod
    def _root_for_node(node: Node) -> str:
        return str(
            node.extra.get("tier3_source_root_id")
            or node.extra.get("tier3_source_source_root_key")
            or node.extra.get("tier3_source_root_key")
            or node.source
            or "unknown"
        )

    def available_urls(self, urls: list[str], *, now: float | None = None) -> list[str]:
        current = time.monotonic() if now is None else float(now)
        available: list[str] = []
        for url in urls:
            state = self._states.get(url)
            if state is None or state.cooldown_until <= 0.0:
                available.append(url)
                continue
            if current < state.cooldown_until or state.half_open:
                continue
            state.half_open = True
            available.append(url)
        return available

    def record(self, node: Node, outcome: _MihomoRealProbeOutcome) -> None:
        root = self._root_for_node(node)
        protocol = str(node.protocol or "unknown")
        success_url = outcome.result.probe_url if outcome.result is not None else ""
        failures = dict(outcome.failures_by_url)
        completed_urls = set(outcome.completed_urls)
        exhausted_urls = set(outcome.exhausted_urls)
        if not completed_urls and not exhausted_urls and not outcome.censored_urls:
            # Outcomes created by older callers/tests predate the progress
            # contract and represent completed attempts.
            completed_urls.update(outcome.attempted_urls)
            exhausted_urls.update(outcome.attempted_urls)
        for url in outcome.attempted_urls:
            state = self._states.setdefault(url, _MihomoProbeEndpointState())
            state.started += 1
            state.exposure_ms += max(
                0.0,
                float(outcome.exposure_ms_by_url.get(url, 0.0) or 0.0),
            )
            if url not in completed_urls and url not in exhausted_urls:
                # A wave-level deadline is right-censoring. It must not teach
                # endpoint health that the endpoint itself was transient.
                state.censored += 1
                continue
            state.attempts += 1
            state.roots.add(root)
            state.protocols.add(protocol)
            if url == success_url:
                state.successes += 1
                state.cooldown_until = 0.0
                state.half_open = False
                continue
            failure = failures.get(url, "probe deadline")
            if _real_probe_failure_is_transient(failure):
                state.transients += 1
            state.half_open = False
            transient_rate = state.transients / float(max(1, state.attempts))
            if (
                state.attempts >= self.minimum_attempts
                and len(state.roots) >= 2
                and len(state.protocols) >= 2
                and transient_rate >= self.transient_threshold
            ):
                state.cooldown_until = time.monotonic() + self.cooldown_seconds

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        return {
            url: {
                "started": state.started,
                "attempted": state.attempts,
                "success": state.successes,
                "transient": state.transients,
                "censored": state.censored,
                "exposure_ms": round(state.exposure_ms, 3),
                "root_count": len(state.roots),
                "protocol_count": len(state.protocols),
                "cooldown": bool(state.cooldown_until > now),
                "half_open": bool(state.half_open),
                "cooldown_remaining_ms": int(
                    round(max(0.0, state.cooldown_until - now) * 1000)
                ),
            }
            for url, state in self._states.items()
        }


async def _tcp_probe(
    node: Node,
    timeout_seconds: float,
    slow_threshold_ms: int,
    testing_config: dict[str, Any] | None = None,
) -> Node:
    updated = node.clone()
    started = time.perf_counter()
    try:
        connection = asyncio.open_connection(node.address, int(node.port))
        reader, writer = await asyncio.wait_for(connection, timeout=timeout_seconds)
        latency_ms = (time.perf_counter() - started) * 1000
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.05)
        except Exception:
            pass
        updated = apply_success_latency_policy(
            updated,
            latency_ms,
            testing_config,
            "tcp",
            slow_threshold_ms=slow_threshold_ms,
        )
    except Tier3TcpDnsBusy:
        return _tcp_wave_deferred_result(node, "tcp_dns_facility_saturated")
    except Exception as exc:
        _clear_latency_policy_extra(updated)
        updated.status = NodeStatus.DEAD
        updated.latency_ms = None
        updated.suspicious_reason = str(exc)
    updated.last_checked_at = utc_now_iso()
    if updated.status is NodeStatus.DEAD:
        _annotate_tier3_failure(updated, str(updated.suspicious_reason or "dead"))
    return updated


def _pick_free_port() -> int:
    with _reserve_free_port() as sock:
        return int(sock.getsockname()[1])


def _reserve_free_port() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock
    except Exception:
        sock.close()
        raise


def _build_xray_outbound(node: Node) -> dict[str, Any] | None:
    if node.protocol == "vless":
        return {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": node.address,
                        "port": int(node.port),
                        "users": [
                            {
                                "id": node.uuid,
                                "encryption": node.encryption or "none",
                                "flow": node.extra.get("flow"),
                            }
                        ],
                    }
                ]
            },
            "streamSettings": {
                "network": node.network or "tcp",
                "security": node.security or "none",
                "realitySettings": {
                    "serverName": node.sni or node.extra.get("serverName") or node.host,
                    "publicKey": node.extra.get("pbk"),
                    "shortId": node.extra.get("sid"),
                    "fingerprint": node.extra.get("fp", "chrome"),
                }
                if (node.security or "").lower() == "reality"
                else {
                    "serverName": node.sni or node.host,
                }
                if (node.security or "").lower() == "tls"
                else {},
                "wsSettings": {"path": node.path or "/", "headers": {"Host": node.host}}
                if (node.network or "").lower() == "ws"
                else {},
            },
        }
    if node.protocol == "vmess":
        return {
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": node.address,
                        "port": int(node.port),
                        "users": [{"id": node.uuid, "alterId": int(node.extra.get("aid", 0)), "security": "auto"}],
                    }
                ]
            },
            "streamSettings": {
                "network": node.network or "tcp",
                "security": node.security or "none",
                "tlsSettings": {"serverName": node.sni or node.host} if node.tls else {},
                "wsSettings": {"path": node.path or "/", "headers": {"Host": node.host}}
                if (node.network or "").lower() == "ws"
                else {},
            },
        }
    if node.protocol == "trojan":
        return {
            "protocol": "trojan",
            "settings": {
                "servers": [
                    {
                        "address": node.address,
                        "port": int(node.port),
                        "password": node.password,
                    }
                ]
            },
            "streamSettings": {
                "network": node.network or "tcp",
                "security": "tls",
                "tlsSettings": {"serverName": node.sni or node.host},
                "wsSettings": {"path": node.path or "/", "headers": {"Host": node.host}}
                if (node.network or "").lower() == "ws"
                else {},
            },
        }
    if node.protocol == "ss":
        return {
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": node.address,
                        "port": int(node.port),
                        "method": node.method,
                        "password": node.password,
                    }
                ]
            },
        }
    return None


def _build_xray_config(node: Node, inbound_port: int) -> dict[str, Any] | None:
    outbound = _build_xray_outbound(node)
    if not outbound:
        return None
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "local-http",
                "listen": "127.0.0.1",
                "port": inbound_port,
                "protocol": "http",
                "settings": {},
            }
        ],
        "outbounds": [
            {"tag": "proxy", **outbound},
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
        "routing": {"rules": [{"type": "field", "inboundTag": ["local-http"], "outboundTag": "proxy"}]},
    }
    return config


def _xray_command(binary_path: str, config_path: str) -> list[str]:
    return [binary_path, "run", "-config", config_path]


def _full_validate(
    node: Node,
    helper_config: dict[str, Any],
    logger: logging.Logger,
) -> tuple[NodeStatus, str | None, float | None]:
    binary_path = helper_config.get("binary_path")
    if not binary_path:
        return node.status, None, None
    reserved_port = _reserve_free_port()
    inbound_port = int(reserved_port.getsockname()[1])
    xray_config = _build_xray_config(node, inbound_port)
    if not xray_config:
        reserved_port.close()
        return node.status, None, None

    probe_url = helper_config.get("probe_url", "https://www.google.com/generate_204")
    startup_delay = float(helper_config.get("startup_delay", 1.5))
    process: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="pproxy-xray-") as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(xray_config), encoding="utf-8")
            reserved_port.close()
            process = subprocess.Popen(
                _xray_command(binary_path, str(config_path)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            )
            time.sleep(startup_delay)
            proxy_url = f"http://127.0.0.1:{inbound_port}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            request_started = time.perf_counter()
            request = urllib.request.Request(probe_url, method="GET")
            with opener.open(request, timeout=float(helper_config.get("probe_timeout", 8))) as response:
                status_code = getattr(response, "status", None) or response.getcode()
                body = response.read(128)
            request_finished = time.perf_counter()
            if int(status_code) != 204 or body not in {b"", b"\n"}:
                return NodeStatus.SUSPICIOUS, f"Unexpected probe response status={status_code}", None
            return node.status, None, (request_finished - request_started) * 1000
    except Exception as exc:
        log_raw_diagnostic(
            logger,
            "warning",
            "Full validation failed for %s:%s: %s",
            node.address,
            node.port,
            exc,
        )
        return (
            NodeStatus.SUSPICIOUS,
            str(exc) if node.status != NodeStatus.DEAD else node.suspicious_reason,
            None,
        )
    finally:
        try:
            reserved_port.close()
        except OSError:
            pass
        _stop_process(process)


def _mihomo_command(binary_path: str, config_path: str, work_dir: str) -> list[str]:
    return [binary_path, "-f", config_path, "-d", work_dir]


def _validation_temp_parent(testing_config: dict[str, Any]) -> str | None:
    for key in ("mihomo_validation_temp_dir", "_mihomo_validation_temp_dir", "validation_temp_dir"):
        value = str(testing_config.get(key) or "").strip()
        if value:
            parent = Path(value)
            parent.mkdir(parents=True, exist_ok=True)
            return str(parent)
    return None


class _ValidationTempDir:
    def __init__(self, prefix: str, parent: str | None) -> None:
        base = Path(parent) if parent else Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        for _attempt in range(20):
            path = base / f"{prefix}{uuid.uuid4().hex}"
            try:
                path.mkdir()
            except FileExistsError:
                continue
            self.path = path
            self.name = str(path)
            return
        raise FileExistsError(f"could not create validation temp directory under {base}")

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _temporary_validation_dir(prefix: str, testing_config: dict[str, Any]) -> _ValidationTempDir:
    return _ValidationTempDir(prefix, _validation_temp_parent(testing_config))


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
    except Exception:
        pass
    try:
        process.terminate()
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            pass
    try:
        if process.poll() is None:
            process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=3)
    except Exception:
        pass


def _stop_process_bounded(
    process: subprocess.Popen[str] | None,
    *,
    terminate_timeout: float = 0.25,
    kill_timeout: float = 0.25,
) -> bool:
    """Stop one validator process without the multi-second taskkill fallback."""

    if process is None:
        return True
    try:
        if process.poll() is not None:
            return True
    except Exception:
        pass
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=max(0.01, float(terminate_timeout)))
        return True
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        try:
            if process.poll() is not None:
                return True
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=max(0.01, float(kill_timeout)))
        return True
    except Exception:
        try:
            return process.poll() is not None
        except Exception:
            return False


def _wait_for_tcp_port(
    host: str,
    port: int,
    timeout: float,
    *,
    cancel_event: Any | None = None,
) -> None:
    deadline = time.monotonic() + max(0.1, timeout)
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        if _validation_cancel_requested(cancel_event):
            raise RuntimeError("validation_cancelled: manager_closing")
        try:
            with socket.create_connection(
                (host, int(port)),
                timeout=min(0.1, max(0.01, deadline - time.monotonic())),
            ):
                return
        except OSError as exc:
            last_error = exc
            pause = min(0.05, max(0.0, deadline - time.monotonic()))
            if cancel_event is not None:
                wait = getattr(cancel_event, "wait", None)
                if callable(wait):
                    try:
                        if wait(pause):
                            raise RuntimeError(
                                "validation_cancelled: manager_closing"
                            )
                        continue
                    except RuntimeError:
                        raise
                    except Exception:
                        pass
            time.sleep(pause)
    raise TimeoutError(str(last_error) if last_error else f"timed out waiting for {host}:{port}")


def _build_mihomo_chain_config(
    entry_node: Node,
    gateway_node: Node,
    inbound_port: int,
    logger: logging.Logger,
    *,
    ipv6: bool = False,
) -> dict[str, Any] | None:
    entry_proxy = node_to_clash_proxy(entry_node, logger=logger, seen_names=set())
    gateway_proxy = node_to_clash_proxy(gateway_node, logger=logger, seen_names={entry_proxy["name"]} if entry_proxy else set())
    if not entry_proxy or not gateway_proxy:
        return None
    entry_proxy = dict(entry_proxy)
    gateway_proxy = dict(gateway_proxy)
    entry_proxy["name"] = "TEST-ENTRY"
    gateway_proxy["name"] = "TEST-GATEWAY"
    gateway_proxy["dialer-proxy"] = "TEST-ENTRY"
    return {
        "mixed-port": inbound_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": coerce_bool(ipv6, False),
        "proxies": [entry_proxy, gateway_proxy],
        "proxy-groups": [
            {
                "name": "CHAIN-EXIT",
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": ["TEST-GATEWAY"],
            }
        ],
        "rules": ["MATCH,CHAIN-EXIT"],
    }


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = []
    items: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _mihomo_probe_urls(testing_config: dict[str, Any]) -> list[str]:
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    urls: list[str] = []
    for value in (
        helper_config.get("probe_urls"),
        testing_config.get("probe_urls"),
        helper_config.get("probe_url"),
        testing_config.get("probe_url"),
        [
            "https://cp.cloudflare.com",
            "https://www.gstatic.com/generate_204",
            "http://connectivitycheck.gstatic.com/generate_204",
            "http://www.gstatic.com/generate_204",
        ],
    ):
        for url in _as_string_list(value):
            if url not in urls:
                urls.append(url)
    return urls or [
        "https://cp.cloudflare.com",
        "https://www.gstatic.com/generate_204",
        "http://connectivitycheck.gstatic.com/generate_204",
        "http://www.gstatic.com/generate_204",
    ]


def _mihomo_validation_probe_portfolio(
    probe_urls: list[str],
    *,
    limit: int,
) -> list[str]:
    """Build the canonical portfolio without letting HTTPS crowd out HTTP."""
    ordered = list(
        dict.fromkeys(str(url).strip() for url in probe_urls if str(url or "").strip())
    )
    http_defaults = (
        "http://connectivitycheck.gstatic.com/generate_204",
        "http://www.gstatic.com/generate_204",
    )
    http_urls = [url for url in ordered if url.lower().startswith("http://")]
    for url in http_defaults:
        if url not in http_urls:
            http_urls.append(url)
    https_urls = [url for url in ordered if url.lower().startswith("https://")]
    other_urls = [
        url
        for url in ordered
        if not url.lower().startswith(("http://", "https://"))
    ]
    portfolio_limit = max(3, int(limit or 0))
    portfolio = [*http_urls[:2], *https_urls, *http_urls[2:], *other_urls]
    return portfolio[:portfolio_limit]


def _mihomo_expected_statuses(testing_config: dict[str, Any]) -> set[int]:
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    raw_values = (
        helper_config.get("expected_statuses")
        or helper_config.get("expected_status")
        or testing_config.get("expected_statuses")
        or [204, 200]
    )
    statuses: set[int] = set()
    for value in _as_string_list(raw_values):
        try:
            statuses.add(int(value))
        except ValueError:
            continue
    return statuses or {204, 200}


def _probe_body_ok(status_code: int, body: bytes) -> bool:
    if status_code == 204:
        return body in {b"", b"\n"}
    return True


def _mihomo_probe_timeout(testing_config: dict[str, Any]) -> float:
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    xray_config = testing_config.get("xray_helper", {})
    xray_config = xray_config if isinstance(xray_config, dict) else {}
    raw_value = (
        helper_config.get("probe_timeout")
        or testing_config.get("request_timeout")
        or xray_config.get("probe_timeout")
        or 8
    )
    try:
        return max(1.0, float(raw_value))
    except (TypeError, ValueError):
        return 8.0


def _build_mihomo_direct_config(
    node: Node,
    inbound_port: int,
    logger: logging.Logger,
    *,
    ipv6: bool = False,
    proxy_name: str = "TEST-PROXY",
) -> dict[str, Any] | None:
    return build_single_proxy_validation_config(
        node,
        inbound_port,
        ipv6=coerce_bool(ipv6, False),
        proxy_name=proxy_name,
        logger=logger,
    )


def _build_mihomo_prewarm_config(*, ipv6: bool = False) -> dict[str, Any]:
    """Build a local-only validator config without candidate business state."""
    return {
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": coerce_bool(ipv6, False),
        "proxies": [],
        "proxy-groups": [],
        "rules": ["MATCH,REJECT"],
    }


def _build_mihomo_batch_config(
    nodes: list[Node],
    mixed_port: int,
    controller_port: int,
    logger: logging.Logger,
    *,
    ipv6: bool = False,
    proxy_name_prefix: str = "TEST-PROXY",
) -> tuple[dict[str, Any] | None, list[tuple[Node, str]], list[Node]]:
    proxies: list[dict[str, Any]] = []
    entries: list[tuple[Node, str]] = []
    unsupported: list[Node] = []
    seen_names: set[str] = set()
    for node in nodes:
        proxy = node_to_clash_proxy(node, logger=logger, seen_names=seen_names)
        if not proxy:
            unsupported.append(node)
            continue
        proxy = dict(proxy)
        proxy_name = f"{proxy_name_prefix}-{len(proxies) + 1}"
        proxy["name"] = proxy_name
        proxies.append(proxy)
        entries.append((node, proxy_name))
    if not proxies:
        return None, [], unsupported
    proxy_names = [proxy["name"] for proxy in proxies]
    return (
        {
            "mixed-port": mixed_port,
            "external-controller": f"127.0.0.1:{controller_port}",
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "ipv6": coerce_bool(ipv6, False),
            "dns": helper_dns_block(ipv6=coerce_bool(ipv6, False)),
            "proxies": proxies,
            "proxy-groups": [
                {
                    "name": "TEST-BATCH",
                    "type": "select",
                    "empty-fallback": "REJECT",
                    "proxies": proxy_names,
                }
            ],
            "rules": ["MATCH,TEST-BATCH"],
        },
        entries,
        unsupported,
    )


def _notify_result_observer(
    result_observer: Callable[[Node], None] | None,
    node: Node,
    logger: logging.Logger,
) -> None:
    if result_observer is None:
        return
    try:
        result_observer(node)
    except Exception:
        log_raw_diagnostic(
            logger,
            "warning",
            "Node validation result observer failed for %s:%s",
            node.address,
            node.port,
            exc_info=True,
        )


def _tier3_failure_stage(reason: str, status: NodeStatus | None = None) -> str:
    text = str(reason or "")
    lowered = text.lower()
    if text.startswith("candidate_transient_503:"):
        return "candidate_transient_503"
    if text.startswith("candidate_transient_504:"):
        return "candidate_transient_504"
    if text.startswith("all_probe_transient:"):
        return "all_probe_transient"
    if text.startswith("full_validation_all_probe_transient:"):
        return "full_validation_all_probe_transient"
    if text.startswith("tcp_deferred"):
        return "tcp_deferred"
    if text.startswith("tcp_cancelled"):
        return "tcp_cancelled"
    if text.startswith("validation_deferred"):
        return "validation_deferred"
    if text.startswith("validation_cancelled"):
        return "validation_cancelled"
    if text.startswith("validator_facility_failure:"):
        return "validator_facility_failure"
    if text.startswith("mihomo_batch_validation_failed:"):
        if "http error 503" in lowered or "http error 504" in lowered:
            return "mihomo_delay_transient"
        return "mihomo_delay_failed"
    if "unsupported proxy" in lowered:
        return "unsupported_proxy"
    if text.startswith("mihomo_validation_failed:"):
        return "mihomo_direct_failed"
    if text.startswith("all_gateways_unavailable:"):
        return "gateway_validation_failed"
    if status is NodeStatus.DEAD:
        if "getaddrinfo" in lowered or "name or service" in lowered:
            return "tcp_dns_failed"
        if "refused" in lowered or "拒绝" in text:
            return "tcp_refused"
        if "timed out" in lowered or "timeout" in lowered or "超时" in text:
            return "tcp_timeout"
        return "tcp_dead"
    return "validation_failed"


def _annotate_tier3_failure(updated: Node, reason: str) -> Node:
    if str(updated.tier or "").lower() != "tier3":
        return updated
    stage = _tier3_failure_stage(reason, updated.status)
    updated.extra["tier3_failure_stage"] = stage
    updated.extra["tier3_failure_reason"] = str(reason or "")
    updated.extra["tier3_failure_protocol"] = updated.protocol
    updated.extra["tier3_failure_port"] = int(updated.port or 0)
    source = str(updated.source or updated.extra.get("source") or "")
    source_type = str(updated.extra.get("source_type") or updated.extra.get("tier3_source_type") or "")
    if source:
        updated.extra["tier3_failure_source_id"] = f"{source_type}:{source}" if source_type else source
    elif updated.extra.get("tier3_source_id"):
        updated.extra["tier3_failure_source_id"] = str(updated.extra.get("tier3_source_id"))
    if source_type:
        updated.extra["tier3_failure_source_type"] = source_type
    lane = updated.extra.get("tier3_source_lane") or updated.extra.get("tier3_outside_lane")
    if lane:
        updated.extra["tier3_failure_lane"] = lane
    return updated


def _tcp_wave_deferred_result(node: Node, reason: str) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.SUSPICIOUS
    updated.latency_ms = None
    updated.suspicious_reason = f"tcp_deferred: {reason}"
    updated.last_checked_at = utc_now_iso()
    return _annotate_tier3_failure(updated, str(updated.suspicious_reason or reason))


async def _collect_tcp_probe_tasks(
    task_map: dict[asyncio.Task[Node], Node],
    *,
    deadline_seconds: float = 0.0,
    cleanup_slack_seconds: float = 0.25,
    logger: logging.Logger | None = None,
    cancel_event: Any | None = None,
) -> TcpProbeCollectionOutcome:
    scan_started_at = time.monotonic()
    cleanup_slack = max(0.0, min(0.5, float(cleanup_slack_seconds)))
    deadline = max(0.0, float(deadline_seconds))
    metadata: dict[str, Any] = {
        "scan_started_at": scan_started_at,
        "scan_finished_at": scan_started_at,
        "tcp_scan_elapsed_ms": 0,
        "deadline_seconds": deadline,
        "cleanup_slack_ms": int(round(cleanup_slack * 1000)),
        "scheduled_count": len(task_map),
        "completed_before_deadline": 0,
        "deferred_by_deadline": 0,
        "late_completed_ignored": 0,
        "deadline_hit": False,
        "deadline_violation": False,
        "cancelled": False,
    }
    if not task_map:
        return TcpProbeCollectionOutcome(nodes=[], metadata=metadata)
    pending: set[asyncio.Task[Node]] = set(task_map)
    results: list[Node] = []
    deadline_at = scan_started_at + deadline if deadline > 0 else 0.0

    def defer_node(node: Node | None, reason: str = "tcp_wave_deadline") -> None:
        if node is None:
            return
        results.append(_tcp_wave_deferred_result(node, reason))
        metadata["deferred_by_deadline"] = int(metadata["deferred_by_deadline"]) + 1
        if reason == "tcp_wave_deadline":
            metadata["deadline_hit"] = True

    def ignore_late_task(task: asyncio.Task[Node], node: Node | None) -> None:
        if node is None:
            return
        metadata["late_completed_ignored"] = int(metadata["late_completed_ignored"]) + 1
        try:
            if task.cancelled():
                pass
            else:
                task.result()
        except BaseException:
            pass
        defer_node(node)

    cancelled = False
    while pending:
        if _validation_cancel_requested(cancel_event):
            cancelled = True
            metadata["cancelled"] = True
            break
        timeout = None
        if deadline_at > 0:
            timeout = max(0.0, deadline_at - time.monotonic())
            if timeout <= 0:
                break
        if cancel_event is not None:
            timeout = min(0.05, timeout) if timeout is not None else 0.05
        done, pending = await asyncio.wait(
            pending,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        now = time.monotonic()
        if not done:
            if deadline_at > 0 and time.monotonic() >= deadline_at:
                break
            continue
        for task in done:
            node = task_map.get(task)
            if deadline_at > 0 and now > deadline_at:
                ignore_late_task(task, node)
                continue
            try:
                results.append(await task)
                metadata["completed_before_deadline"] = int(metadata["completed_before_deadline"]) + 1
            except asyncio.CancelledError:
                if node is not None:
                    results.append(_tcp_wave_deferred_result(node, "tcp_wave_cancelled"))
            except Exception as exc:
                if logger is not None:
                    log_raw_diagnostic(
                        logger,
                        "warning",
                        "Bastion Layer TCP scanner task failed: %s",
                        exc,
                        exc_info=True,
                    )
                if node is not None:
                    updated = node.clone()
                    updated.status = NodeStatus.DEAD
                    updated.latency_ms = None
                    updated.suspicious_reason = str(exc)
                    updated.last_checked_at = utc_now_iso()
                    results.append(_annotate_tier3_failure(updated, str(exc)))
    if pending:
        pending_at_deadline = list(pending)
        defer_reason = (
            "tcp_wave_cancelled" if cancelled else "tcp_wave_deadline"
        )
        for task in pending_at_deadline:
            defer_node(task_map.get(task), defer_reason)
        for task in pending:
            task.cancel()
        done_after_cancel: set[asyncio.Task[Node]] = set()
        if cleanup_slack > 0:
            done_after_cancel, pending = await asyncio.wait(
                pending,
                timeout=cleanup_slack,
                return_when=asyncio.ALL_COMPLETED,
            )
        for task in done_after_cancel:
            try:
                if not task.cancelled():
                    task.result()
                    metadata["late_completed_ignored"] = int(metadata["late_completed_ignored"]) + 1
            except asyncio.CancelledError:
                pass
            except Exception:
                metadata["late_completed_ignored"] = int(metadata["late_completed_ignored"]) + 1
    scan_finished_at = time.monotonic()
    elapsed = max(0.0, scan_finished_at - scan_started_at)
    metadata["scan_finished_at"] = scan_finished_at
    metadata["tcp_scan_elapsed_ms"] = int(round(elapsed * 1000))
    if deadline > 0 and elapsed > deadline + cleanup_slack:
        metadata["deadline_violation"] = True
    return TcpProbeCollectionOutcome(nodes=results, metadata=metadata)


async def _run_tcp_probe_entitlement(
    entitlement: Tier3TcpProbeEntitlement,
    *,
    semaphore: asyncio.Semaphore,
    entitlement_seconds: float,
    cleanup_slack_seconds: float,
    cancel_event: Any | None,
    probe_factory: Callable[[float], Awaitable[Node]],
    transport_start_observer: Callable[[Node, float], bool] | None = None,
    transport_entitlement_provider: (
        Callable[[Node, float], tuple[int, float]] | None
    ) = None,
    transport_began_observer: Callable[[Node, float], None] | None = None,
) -> Node:
    async with semaphore:
        if _validation_cancel_requested(cancel_event):
            entitlement.completed_at = time.monotonic()
            return _tcp_wave_deferred_result(
                entitlement.node,
                "tcp_wave_cancelled",
            )
        # ``started_at`` names this start attempt: the provider and the start
        # ACK key their shared envelope selection by it. It is not the
        # entitlement clock. The ACK runs synchronously on this loop and may
        # wait for the prestart-ledger and scheduler locks, and that wait is
        # not transport time.
        started_at = time.monotonic()
        effective_revision = 0
        effective_entitlement_seconds = max(
            0.001,
            float(entitlement_seconds),
        )
        if transport_entitlement_provider is not None:
            try:
                revision, timeout_seconds = transport_entitlement_provider(
                    entitlement.node,
                    started_at,
                )
                effective_revision = max(0, int(revision))
                effective_entitlement_seconds = max(
                    0.001,
                    float(timeout_seconds),
                )
            except Exception:
                # A diagnostic provider must never prevent a real TCP attempt;
                # the batch-captured entitlement remains the bounded fallback.
                effective_revision = 0
        if transport_start_observer is not None:
            try:
                start_accepted = bool(
                    transport_start_observer(entitlement.node, started_at)
                )
            except Exception:
                start_accepted = False
            if not start_accepted:
                entitlement.completed_at = time.monotonic()
                return _tcp_wave_deferred_result(
                    entitlement.node,
                    "tcp_handoff_start_rejected",
                )
            entitlement.start_acknowledged = True

        def start_entitlement_clock() -> None:
            entitlement.start_transport(
                effective_entitlement_seconds,
                delivery_grace_seconds=cleanup_slack_seconds,
                envelope_revision=effective_revision,
            )

        # Mark the start boundary as soon as the ACK returns, so collection
        # never treats an acknowledged probe as unstarted.
        start_entitlement_clock()

        async def probe_with_completion_time() -> Node:
            # The entitlement measures transport time only, so restart its
            # clock when the transport begins. The wave's other start ACKs run
            # on this loop between our ACK and this first step, and their lock
            # waits are not our transport time. ``probe_factory`` arms its own
            # transport timeout in this same step.
            if transport_began_observer is not None:
                # Diagnostic only: lets the owner time its lease on the same
                # transport clock. It is reported before the clock restarts,
                # so the observer's own lock wait is not transport time.
                try:
                    transport_began_observer(entitlement.node, time.monotonic())
                except Exception:
                    pass
            start_entitlement_clock()
            try:
                return await probe_factory(effective_entitlement_seconds)
            finally:
                # Capture transport completion inside the child coroutine.
                # Parent collection latency must not consume the entitlement,
                # while a coroutine that suppresses cancellation cannot turn a
                # post-deadline result into a valid completion.
                entitlement.completed_at = time.monotonic()

        probe_task = asyncio.create_task(probe_with_completion_time())
        # Callbacks run in scheduling order, so this one yield lets the probe
        # task start its transport and restart the clock before the hard cap
        # below is armed. The cap then never ends before the probe's own
        # transport deadline plus delivery grace.
        await asyncio.sleep(0)
        # ``probe_factory`` owns the transport timeout.  The parent only
        # supplies bounded delivery/cleanup grace so an inner wait_for that
        # resolves at the budget edge can publish its complete DEAD outcome.
        # Using another timer with the same duration races the inner timeout
        # and systematically turns full-budget results into censorship.
        hard_cap_seconds = max(
            0.001,
            float(effective_entitlement_seconds)
            + max(0.0, min(0.25, float(cleanup_slack_seconds))),
        )
        done, _pending = await asyncio.wait(
            {probe_task},
            timeout=hard_cap_seconds,
            return_when=asyncio.ALL_COMPLETED,
        )
        if probe_task not in done:
            entitlement.entitlement_exhausted = True
            entitlement.outcome_state = "hard_censored"
            probe_task.cancel()
            probe_task.add_done_callback(
                lambda task: task.exception()
                if not task.cancelled()
                else None
            )
            return _tcp_wave_deferred_result(
                entitlement.node,
                "tcp_probe_entitlement_exhausted",
            )
        try:
            result = probe_task.result()
        except asyncio.CancelledError:
            entitlement.outcome_state = "cancelled_before_start"
            return _tcp_wave_deferred_result(
                entitlement.node,
                "tcp_wave_cancelled",
            )
        completed_within_transport = bool(
            entitlement.transport_deadline > 0.0
            and entitlement.completed_at <= entitlement.transport_deadline
        )
        completed_full_budget_dead = bool(
            entitlement.delivery_grace_deadline > 0.0
            and entitlement.completed_at <= entitlement.delivery_grace_deadline
            and result.status is NodeStatus.DEAD
        )
        if not (completed_within_transport or completed_full_budget_dead):
            entitlement.late_completion_ignored = True
            entitlement.entitlement_exhausted = True
            entitlement.outcome_state = "hard_censored"
            return _tcp_wave_deferred_result(
                entitlement.node,
                "tcp_probe_entitlement_exhausted",
            )
        entitlement.outcome_state = "completed"
        return result


async def _collect_tcp_probe_entitlements(
    task_map: dict[asyncio.Task[Node], Tier3TcpProbeEntitlement],
    *,
    entitlement_seconds: float,
    dispatch_start_guard_seconds: float,
    cleanup_slack_seconds: float,
    logger: logging.Logger | None = None,
    cancel_event: Any | None = None,
    result_observer: Callable[[Node], None] | None = None,
) -> TcpProbeCollectionOutcome:
    """Collect independently timed TCP probes without a shared wave deadline."""

    scan_started_at = time.monotonic()
    start_guard = max(0.001, min(0.5, float(dispatch_start_guard_seconds)))
    cleanup_slack = max(0.0, min(0.25, float(cleanup_slack_seconds)))
    pending: set[asyncio.Task[Node]] = set(task_map)
    results: list[Node] = []
    deferred_tasks: set[asyncio.Task[Node]] = set()
    metadata: dict[str, Any] = {
        "scan_started_at": scan_started_at,
        "scan_finished_at": scan_started_at,
        "tcp_scan_elapsed_ms": 0,
        "deadline_seconds": round(max(0.0, float(entitlement_seconds)), 3),
        "dispatch_start_guard_ms": int(round(start_guard * 1000)),
        "cleanup_slack_ms": int(round(cleanup_slack * 1000)),
        "scheduled_count": len(task_map),
        "transport_started_count": 0,
        "transport_start_acknowledged_count": 0,
        "transport_start_cancelled_count": 0,
        "transport_start_latency_samples_ms": [],
        "transport_start_latency_p50_ms": 0.0,
        "transport_start_latency_p95_ms": 0.0,
        "transport_start_latency_p99_ms": 0.0,
        "completed_before_deadline": 0,
        "completed_count": 0,
        "hard_censored_count": 0,
        "cancelled_before_start_count": 0,
        "deferred_before_transport_start": 0,
        "entitlement_exhausted_count": 0,
        "deferred_by_deadline": 0,
        "late_completed_ignored": 0,
        "deadline_hit": False,
        "deadline_violation": False,
        "cancelled": False,
    }

    def defer_task(task: asyncio.Task[Node], reason: str) -> None:
        if task in deferred_tasks:
            return
        deferred_tasks.add(task)
        entitlement = task_map[task]
        if reason in {
            "tcp_transport_start_deferred",
            "tcp_handoff_start_rejected",
            "tcp_wave_cancelled",
        } and entitlement.transport_started_at <= 0.0:
            entitlement.outcome_state = "cancelled_before_start"
        updated = _tcp_wave_deferred_result(entitlement.node, reason)
        results.append(updated)
        _notify_result_observer(
            result_observer, updated, logger or logging.getLogger(__name__),
        )
        metadata["deferred_by_deadline"] = int(metadata["deferred_by_deadline"]) + 1
        if reason == "tcp_transport_start_deferred":
            metadata["deferred_before_transport_start"] = int(
                metadata["deferred_before_transport_start"]
            ) + 1
        elif reason == "tcp_probe_entitlement_exhausted":
            metadata["entitlement_exhausted_count"] = int(
                metadata["entitlement_exhausted_count"]
            ) + 1
            metadata["hard_censored_count"] = int(
                metadata["hard_censored_count"]
            ) + 1
        metadata["deadline_hit"] = True

    while pending:
        if _validation_cancel_requested(cancel_event):
            metadata["cancelled"] = True
            for task in tuple(pending):
                defer_task(task, "tcp_wave_cancelled")
                task.cancel()
            break
        now = time.monotonic()
        for task in tuple(pending):
            entitlement = task_map[task]
            if (
                entitlement.transport_started_at <= 0.0
                and now - entitlement.dispatched_at >= start_guard
            ):
                defer_task(task, "tcp_transport_start_deferred")
                task.cancel()
                pending.discard(task)
        if not pending:
            break
        next_guard = min(
            (
                max(
                    0.0,
                    entitlement.dispatched_at + start_guard - time.monotonic(),
                )
                for task, entitlement in task_map.items()
                if task in pending and entitlement.transport_started_at <= 0.0
            ),
            default=0.05,
        )
        done, still_pending = await asyncio.wait(
            pending,
            timeout=min(0.05, max(0.001, next_guard)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        pending = set(still_pending)
        for task in done:
            entitlement = task_map[task]
            if task in deferred_tasks:
                continue
            try:
                updated = task.result()
            except asyncio.CancelledError:
                defer_task(task, "tcp_wave_cancelled")
                continue
            except Exception as exc:
                if logger is not None:
                    log_raw_diagnostic(
                        logger,
                        "warning",
                        "Bastion Layer TCP scanner task failed: %s",
                        exc,
                        exc_info=True,
                    )
                updated = entitlement.node.clone()
                updated.status = NodeStatus.DEAD
                updated.latency_ms = None
                updated.suspicious_reason = str(exc)
                updated.last_checked_at = utc_now_iso()
                updated = _annotate_tier3_failure(updated, str(exc))
            if entitlement.entitlement_exhausted:
                if entitlement.late_completion_ignored:
                    metadata["late_completed_ignored"] = int(
                        metadata["late_completed_ignored"]
                    ) + 1
                defer_task(task, "tcp_probe_entitlement_exhausted")
                continue
            results.append(updated)
            if (
                entitlement.transport_deadline > 0.0
                and entitlement.completed_at <= entitlement.transport_deadline
            ):
                metadata["completed_before_deadline"] = int(
                    metadata["completed_before_deadline"]
                ) + 1
            metadata["completed_count"] = int(metadata["completed_count"]) + 1
            _notify_result_observer(
                result_observer, updated, logger or logging.getLogger(__name__),
            )

    pending_cleanup = {task for task in task_map if not task.done()}
    if pending_cleanup:
        for task in pending_cleanup:
            task.cancel()
        done_after_cancel, pending_cleanup = await asyncio.wait(
            pending_cleanup,
            timeout=cleanup_slack,
            return_when=asyncio.ALL_COMPLETED,
        )
        for task in done_after_cancel:
            try:
                if not task.cancelled():
                    task.result()
                    metadata["late_completed_ignored"] = int(
                        metadata["late_completed_ignored"]
                    ) + 1
            except BaseException:
                pass
        if pending_cleanup:
            metadata["deadline_violation"] = True
    finished_at = time.monotonic()
    start_latencies_ms = sorted(
        max(
            0.0,
            (entitlement.transport_started_at - entitlement.dispatched_at)
            * 1000.0,
        )
        for entitlement in task_map.values()
        if entitlement.transport_started_at > 0.0
    )
    started_entitlements = [
        entitlement
        for entitlement in task_map.values()
        if entitlement.transport_started_at > 0.0
    ]
    metadata["transport_envelope_revisions"] = sorted(
        {
            max(0, int(entitlement.envelope_revision))
            for entitlement in started_entitlements
        }
    )
    entitlement_samples = sorted(
        round(max(0.0, float(entitlement.entitlement_seconds)), 3)
        for entitlement in started_entitlements
    )
    metadata["transport_entitlement_seconds_samples"] = entitlement_samples
    metadata["transport_entitlement_seconds_min"] = (
        entitlement_samples[0] if entitlement_samples else 0.0
    )
    metadata["transport_entitlement_seconds_max"] = (
        entitlement_samples[-1] if entitlement_samples else 0.0
    )

    def percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        index = min(
            len(values) - 1,
            max(0, int(math.ceil(len(values) * fraction)) - 1),
        )
        return round(float(values[index]), 3)

    metadata["transport_started_count"] = len(start_latencies_ms)
    metadata["transport_start_acknowledged_count"] = sum(
        1 for entitlement in task_map.values() if entitlement.start_acknowledged
    )
    no_transport_start_count = sum(
        1
        for entitlement in task_map.values()
        if entitlement.transport_started_at <= 0.0
    )
    metadata["transport_start_cancelled_count"] = max(
        0,
        no_transport_start_count
        - int(metadata["deferred_before_transport_start"]),
    )
    metadata["cancelled_before_start_count"] = int(
        metadata["transport_start_cancelled_count"]
    )
    metadata["transport_start_latency_samples_ms"] = [
        round(value, 3) for value in start_latencies_ms
    ]
    metadata["transport_start_latency_p50_ms"] = percentile(
        start_latencies_ms,
        0.50,
    )
    metadata["transport_start_latency_p95_ms"] = percentile(
        start_latencies_ms,
        0.95,
    )
    metadata["transport_start_latency_p99_ms"] = percentile(
        start_latencies_ms,
        0.99,
    )
    metadata["scan_finished_at"] = finished_at
    metadata["tcp_scan_elapsed_ms"] = int(
        round(max(0.0, finished_at - scan_started_at) * 1000)
    )
    return TcpProbeCollectionOutcome(nodes=results, metadata=metadata)


def _mihomo_validation_failure(node: Node, reason: str) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.SUSPICIOUS
    updated.latency_ms = None
    updated.suspicious_reason = reason
    updated.last_checked_at = utc_now_iso()
    return _annotate_tier3_failure(updated, reason)


def _validation_cancel_requested(cancel_event: Any | None) -> bool:
    if cancel_event is None:
        return False
    is_set = getattr(cancel_event, "is_set", None)
    if not callable(is_set):
        return False
    try:
        return bool(is_set())
    except Exception:
        return False


def _cancellation_filtered_result_observer(
    result_observer: Callable[[Node], None] | None,
    cancel_event: Any | None,
) -> Callable[[Node], None] | None:
    """Prevent a completed-but-stale validation from publishing after stop."""

    if result_observer is None:
        return None

    def publish(node: Node) -> None:
        if _validation_cancel_requested(cancel_event):
            return
        result_observer(node)

    return publish


def _notify_many_result_observer(
    result_observer: Callable[[Node], None] | None,
    nodes: list[Node],
    logger: logging.Logger,
) -> None:
    for node in nodes:
        _notify_result_observer(result_observer, node, logger)


def _mihomo_validation_cancelled_results(nodes: list[Node]) -> list[Node]:
    return [
        _mihomo_validation_failure(node, "validation_cancelled: manager_closing")
        for node in nodes
    ]


def _mihomo_validation_deferred_results(nodes: list[Node], reason: str) -> list[Node]:
    detail = str(reason or "wave_deadline")
    results: list[Node] = []
    for node in nodes:
        updated = _mihomo_validation_failure(node, f"validation_deferred: {detail}")
        if detail.startswith("full_validation_"):
            updated.extra["tier3_full_validation_deadline_hit"] = True
        results.append(updated)
    return results


def _mihomo_unsupported_results(nodes: list[Node]) -> list[Node]:
    return [
        _mihomo_validation_failure(node, "mihomo_validation_failed: unsupported proxy")
        for node in nodes
    ]


def _mihomo_validator_facility_failure_results(nodes: list[Node], reason: str) -> list[Node]:
    return [
        _mihomo_validation_failure(node, f"validator_facility_failure: {reason}")
        for node in nodes
    ]


def _mihomo_binary_path(testing_config: dict[str, Any]) -> str:
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    return str(
        helper_config.get("binary_path")
        or testing_config.get("mihomo_binary_path")
        or testing_config.get("_mihomo_binary_path")
        or ""
    )


def _mihomo_direct_validate_many(
    nodes: list[Node],
    testing_config: dict[str, Any],
    slow_threshold_ms: int,
    logger: logging.Logger,
    *,
    result_observer: Callable[[Node], None] | None = None,
    max_workers: int = 1,
    reason: str,
) -> list[Node]:
    if not nodes:
        return []
    log_raw_diagnostic(
        logger,
        "warning",
        "Bastion Layer batch Mihomo validation falling back to per-node validation "
        "(reason=%s nodes=%s workers=%s)",
        reason,
        len(nodes),
        max(1, int(max_workers)),
    )
    results: list[Node] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(nodes), int(max_workers)))) as executor:
        futures = {
            executor.submit(_mihomo_direct_validate, node, testing_config, slow_threshold_ms, logger): node
            for node in nodes
        }
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                updated = future.result()
            except Exception as exc:
                updated = _mihomo_validation_failure(
                    node,
                    f"mihomo_validation_failed: direct fallback exception: {exc}",
                )
            results.append(updated)
            _notify_result_observer(result_observer, updated, logger)
    return results


def _http_error_code_from_text(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"\bHTTP Error\s+(\d{3})\b", text, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    match = re.search(r"\bstatus[=:\s]+(\d{3})\b", text, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _mihomo_proxy_delay_failure_result(node: Node, failures: list[str]) -> Node:
    text = "; ".join(failures[:3]) or "proxy delay failed"
    codes = [_http_error_code_from_text(failure) for failure in failures]
    transient_codes = [code for code in codes if code in {503, 504}]
    if failures and len(transient_codes) == len(failures):
        if len(set(transient_codes)) == 1:
            return _mihomo_validation_failure(node, f"candidate_transient_{transient_codes[0]}: {text}")
        return _mihomo_validation_failure(node, f"all_probe_transient: {text}")
    return _mihomo_validation_failure(node, "mihomo_batch_validation_failed: " + text)


def _real_probe_failure_is_transient(failure: str) -> bool:
    lowered = str(failure or "").lower()
    code = _http_error_code_from_text(lowered)
    if code is not None and 500 <= code <= 504:
        return True
    transient_markers = (
        "timed out",
        "timeout",
        "handshake operation timed out",
        "unexpected_eof",
        "eof occurred in violation of protocol",
        "ssl:",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "remote end closed connection",
    )
    return any(marker in lowered for marker in transient_markers)


def _mihomo_real_probe_failure_result(node: Node, failures: list[str]) -> Node:
    text = "; ".join(failures[:3]) or "real probe failed"
    if failures and all(_real_probe_failure_is_transient(failure) for failure in failures):
        return _mihomo_validation_failure(node, "full_validation_all_probe_transient: " + text)
    return _mihomo_validation_failure(node, "mihomo_batch_validation_failed: " + text)


def _validation_result_passed(node: Node) -> bool:
    return node.status in {NodeStatus.ALIVE, NodeStatus.SLOW}


def _validation_result_summary(node: Node | None) -> dict[str, Any]:
    if node is None:
        return {"status": "missing"}
    return {
        "status": getattr(node.status, "value", str(node.status)),
        "reason": str(node.suspicious_reason or ""),
        "failure_stage": str(node.extra.get("tier3_failure_stage") or ""),
        "failure_reason": str(node.extra.get("tier3_failure_reason") or ""),
        "validation_profile": str(node.extra.get("validation_profile") or ""),
        "validated_by": str(node.extra.get("validated_by") or ""),
        "probe_url": str(node.extra.get("validation_probe_url") or ""),
        "probe_urls_attempted": list(node.extra.get("validation_probe_urls_attempted") or []),
        "probe_failures_by_url": dict(node.extra.get("validation_probe_failures_by_url") or {}),
    }


def _attach_validation_matrix_summary(
    node: Node,
    *,
    session_result: Node | None = None,
    canonical_result: Node | None = None,
    legacy_result: Node | None = None,
    diagnosis: str = "",
) -> Node:
    node.extra["tier3_validation_matrix"] = {
        "session": _validation_result_summary(session_result),
        "canonical_single_proxy": _validation_result_summary(canonical_result),
        "legacy_per_node": _validation_result_summary(legacy_result),
    }
    if diagnosis:
        node.extra["tier3_validation_matrix_diagnosis"] = diagnosis
    return node


def _mihomo_probe_urls_for_initial_cohort(probe_urls: list[str], initial_parallelism: int) -> list[str]:
    urls = [str(url) for url in probe_urls if str(url or "").strip()]
    if len(urls) <= 1 or initial_parallelism <= 1:
        return urls
    first = urls[0]
    selected = [first]
    target_count = min(len(urls), max(1, int(initial_parallelism)))
    http_target = min(2, sum(1 for url in urls if url.lower().startswith("http://")))
    https_target = min(1, sum(1 for url in urls if url.lower().startswith("https://")))

    def add_matching(prefix: str, limit: int) -> None:
        while sum(1 for url in selected if url.lower().startswith(prefix)) < limit:
            candidate = next(
                (
                    url
                    for url in urls
                    if url not in selected and url.lower().startswith(prefix)
                ),
                "",
            )
            if not candidate:
                break
            selected.append(candidate)

    add_matching("https://", https_target)
    add_matching("http://", http_target)
    for url in urls:
        if len(selected) >= target_count:
            break
        if url not in selected:
            selected.append(url)
    ordered = list(selected)
    ordered.extend(url for url in urls if url not in ordered)
    return ordered


def _mihomo_primary_fast_probe_urls(
    probe_urls: list[str],
    *,
    selection_key: str = "",
) -> list[str]:
    """Choose one cheap canonical probe without changing its pass contract."""
    urls = [str(url).strip() for url in probe_urls if str(url).strip()]
    http_probes = [url for url in urls if url.lower().startswith("http://")]
    if http_probes:
        if not selection_key or len(http_probes) == 1:
            return [http_probes[0]]
        digest = hashlib.sha256(selection_key.encode("utf-8", errors="replace")).digest()
        return [http_probes[int.from_bytes(digest[:8], "big") % len(http_probes)]]
    return [urls[0] if urls else "https://cp.cloudflare.com"]


def _mihomo_canonical_probe_urls(
    probe_urls: list[str],
    *,
    attempted_urls: list[str] | tuple[str, ...] | set[str] = (),
    available_urls: list[str] | None = None,
    preferred_urls: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Order untried probes so alternate HTTP and HTTPS both receive a full slice."""
    attempted = {str(url) for url in attempted_urls if str(url or "").strip()}
    allowed = None if available_urls is None else set(available_urls)
    remaining = [
        str(url).strip()
        for url in probe_urls
        if str(url or "").strip()
        and str(url).strip() not in attempted
        and (allowed is None or str(url).strip() in allowed)
    ]
    preferred = [
        str(url).strip()
        for url in preferred_urls
        if str(url or "").strip() in remaining
    ]
    http_urls = [url for url in remaining if url.lower().startswith("http://")]
    https_urls = [url for url in remaining if url.lower().startswith("https://")]
    ordered: list[str] = []
    ordered.extend(url for url in preferred if url not in ordered)
    if http_urls and not any(url.lower().startswith("http://") for url in ordered):
        ordered.append(http_urls[0])
    if https_urls and not any(url.lower().startswith("https://") for url in ordered):
        ordered.append(https_urls[0])
    ordered.extend(url for url in remaining if url not in ordered)
    return ordered


def _mihomo_probe_candidate_fingerprint(node: Node) -> str:
    payload = "|".join(
        (
            str(node.protocol or "unknown").lower(),
            str(node.address or "").lower(),
            str(int(node.port or 0)),
            str(node.extra.get("tier3_source_root_id") or node.source or "unknown"),
        )
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


def _run_mihomo_real_probe_attempts(
    opener: Any,
    probe_urls: list[str],
    expected_statuses: set[int],
    *,
    probe_timeout_seconds: float,
    deadline_at: float | None,
    initial_parallelism: int = 2,
    probe_start_index: int = 0,
    generation_id: str = "",
    candidate_fingerprint: str = "",
    session_lease_id: str = "",
    probe_stage: str = "canonical",
    attempt_observer: Callable[[_MihomoProbeAttemptFact], None] | None = None,
    transport_start_grace_seconds: float = 0.10,
    cleanup_settle_seconds: float = 0.0,
) -> _MihomoRealProbeOutcome:
    started = time.perf_counter()
    ordered_urls = _mihomo_probe_urls_for_initial_cohort(
        probe_urls,
        initial_parallelism,
    )
    if not ordered_urls:
        ordered_urls = ["https://cp.cloudflare.com"]
    start_index = max(0, min(int(probe_start_index or 0), len(ordered_urls)))
    urls = ordered_urls[start_index:]
    queue_results: queue.Queue[_MihomoRealProbeAttemptResult] = queue.Queue()
    attempted_urls: list[str] = []
    completed_urls: list[str] = []
    failures: list[str] = []
    failures_by_url: dict[str, str] = {}
    exposure_ms_by_url: dict[str, float] = {}
    pending_urls: set[str] = set()
    attempt_states: dict[str, dict[str, Any]] = {}
    facts_lock = threading.Lock()
    attempt_facts: list[_MihomoProbeAttemptFact] = []
    next_index = 0
    parallelism = max(1, min(max(1, int(initial_parallelism)), len(urls)))
    start_grace = max(0.01, min(0.5, float(transport_start_grace_seconds)))

    def emit_fact(fact: _MihomoProbeAttemptFact) -> None:
        with facts_lock:
            attempt_facts.append(fact)
            if len(attempt_facts) > 64:
                del attempt_facts[:-64]
        if attempt_observer is not None:
            try:
                attempt_observer(fact)
            except Exception:
                pass

    def remaining_hard_deadline_seconds() -> float | None:
        if deadline_at is None:
            return None
        return float(deadline_at) - time.monotonic()

    def active_attempt_remaining_seconds() -> float | None:
        deadlines = [
            float(state.get("attempt_deadline_at") or 0.0)
            for url, state in attempt_states.items()
            if url in pending_urls and float(state.get("attempt_deadline_at") or 0.0) > 0.0
        ]
        hard_remaining = remaining_hard_deadline_seconds()
        if deadlines:
            attempt_remaining = min(deadlines) - time.monotonic()
            if hard_remaining is None:
                return attempt_remaining
            return min(hard_remaining, attempt_remaining)
        return hard_remaining

    def attempt_probe(probe_url: str, attempt_id: str) -> None:
        state = attempt_states[probe_url]
        transport_started_at = time.monotonic()
        perf_started_at = time.perf_counter()
        hard_remaining = (
            float(deadline_at) - transport_started_at
            if deadline_at is not None
            else None
        )
        granted_budget = max(0.05, float(probe_timeout_seconds))
        if hard_remaining is not None:
            granted_budget = min(granted_budget, max(0.05, hard_remaining))
        state["transport_started_at"] = transport_started_at
        state["perf_started_at"] = perf_started_at
        state["granted_budget_seconds"] = granted_budget
        state["attempt_deadline_at"] = transport_started_at + granted_budget
        state["started_event"].set()
        emit_fact(
            _MihomoProbeAttemptFact(
                attempt_id=attempt_id,
                generation_id=generation_id,
                candidate_fingerprint=candidate_fingerprint,
                session_lease_id=session_lease_id,
                endpoint=probe_url,
                stage=probe_stage,
                transport_started_at=transport_started_at,
                granted_budget_seconds=granted_budget,
                lifecycle="started",
                result="pending",
                failure_domain="",
                failure="",
                elapsed_ms=0.0,
            )
        )
        try:
            request = urllib.request.Request(probe_url, method="GET")
            with opener.open(request, timeout=granted_budget) as response:
                status_code = int(
                    getattr(response, "status", None) or response.getcode()
                )
                body = response.read(128)
            latency_ms = (time.perf_counter() - perf_started_at) * 1000
            body_ok = _probe_body_ok(status_code, body)
            success = bool(status_code in expected_statuses and body_ok)
            failure = "" if success else f"unexpected probe status={status_code}"
            result = _MihomoRealProbeAttemptResult(
                probe_url=probe_url,
                success=success,
                status_code=status_code,
                body_ok=body_ok,
                latency_ms=latency_ms,
                failure=failure,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - perf_started_at) * 1000
            failure = str(exc)
            result = _MihomoRealProbeAttemptResult(
                probe_url=probe_url,
                success=False,
                status_code=None,
                body_ok=False,
                latency_ms=latency_ms,
                failure=failure,
            )
        failure_domain = ""
        if not result.success:
            failure_domain = (
                "probe_transient"
                if _real_probe_failure_is_transient(result.failure)
                else "candidate_probe"
            )
        emit_fact(
            _MihomoProbeAttemptFact(
                attempt_id=attempt_id,
                generation_id=generation_id,
                candidate_fingerprint=candidate_fingerprint,
                session_lease_id=session_lease_id,
                endpoint=probe_url,
                stage=probe_stage,
                transport_started_at=transport_started_at,
                granted_budget_seconds=granted_budget,
                lifecycle="completed",
                result="pass" if result.success else "failed",
                failure_domain=failure_domain,
                failure=result.failure,
                elapsed_ms=result.latency_ms,
            )
        )
        queue_results.put(result)

    def start_next_probe() -> bool:
        nonlocal next_index
        if next_index >= len(urls):
            return False
        remaining = remaining_hard_deadline_seconds()
        if remaining is not None and remaining <= 0.0:
            return False
        probe_url = urls[next_index]
        next_index += 1
        attempt_id = uuid.uuid4().hex
        started_event = threading.Event()
        attempt_states[probe_url] = {
            "attempt_id": attempt_id,
            "started_event": started_event,
            "transport_started_at": 0.0,
            "perf_started_at": 0.0,
            "granted_budget_seconds": 0.0,
            "attempt_deadline_at": 0.0,
        }
        attempted_urls.append(probe_url)
        pending_urls.add(probe_url)
        worker = threading.Thread(
            target=attempt_probe,
            args=(probe_url, attempt_id),
            name="tier3-real-probe",
            daemon=True,
        )
        worker.start()
        wait_for_start = start_grace
        if remaining is not None:
            wait_for_start = min(wait_for_start, max(0.01, remaining))
        started_event.wait(timeout=wait_for_start)
        return True

    for _ in range(parallelism):
        if not start_next_probe():
            break

    deadline_hit = False
    success_result: _MihomoRealProbeAttemptResult | None = None
    while pending_urls:
        remaining = active_attempt_remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            deadline_hit = True
            break
        wait_timeout = 0.05 if remaining is None else max(0.01, min(0.05, remaining))
        try:
            result = queue_results.get(timeout=wait_timeout)
        except queue.Empty:
            continue
        if result.probe_url not in pending_urls:
            continue
        pending_urls.remove(result.probe_url)
        completed_urls.append(result.probe_url)
        exposure_ms_by_url[result.probe_url] = max(0.0, float(result.latency_ms))
        if result.success:
            success_result = result
            break
        failure = result.failure or "probe failed"
        failures.append(f"{result.probe_url}: {failure}")
        failures_by_url[result.probe_url] = failure
        if not pending_urls:
            while next_index < len(urls) and len(pending_urls) < parallelism:
                if not start_next_probe():
                    break
            if not pending_urls and next_index >= len(urls):
                break

    settled_in_cleanup_count = 0
    if deadline_hit and pending_urls and cleanup_settle_seconds > 0.0:
        settle_deadline = time.monotonic() + min(
            0.5,
            max(0.0, float(cleanup_settle_seconds)),
        )
        while pending_urls:
            settle_remaining = settle_deadline - time.monotonic()
            if settle_remaining <= 0.0:
                break
            try:
                result = queue_results.get(
                    timeout=max(0.01, min(0.05, settle_remaining))
                )
            except queue.Empty:
                continue
            if result.probe_url not in pending_urls:
                continue
            pending_urls.remove(result.probe_url)
            completed_urls.append(result.probe_url)
            settled_in_cleanup_count += 1
            exposure_ms_by_url[result.probe_url] = max(
                0.0,
                float(result.latency_ms),
            )
            if result.success:
                success_result = result
                deadline_hit = False
                break
            failure = result.failure or "probe failed"
            failures.append(f"{result.probe_url}: {failure}")
            failures_by_url[result.probe_url] = failure
        if success_result is None:
            deadline_hit = bool(pending_urls or next_index < len(urls))

    finished_at = time.perf_counter()
    elapsed_ms = int(round((finished_at - started) * 1000))
    censored_urls = [url for url in attempted_urls if url in pending_urls]
    for pending_url in censored_urls:
        state = attempt_states.get(pending_url, {})
        transport_started_at = float(state.get("transport_started_at") or 0.0)
        perf_started_at = float(state.get("perf_started_at") or started)
        exposure_ms = max(0.0, (finished_at - perf_started_at) * 1000.0)
        exposure_ms_by_url[pending_url] = exposure_ms
        emit_fact(
            _MihomoProbeAttemptFact(
                attempt_id=str(state.get("attempt_id") or ""),
                generation_id=generation_id,
                candidate_fingerprint=candidate_fingerprint,
                session_lease_id=session_lease_id,
                endpoint=pending_url,
                stage=probe_stage,
                transport_started_at=transport_started_at,
                granted_budget_seconds=float(
                    state.get("granted_budget_seconds") or 0.0
                ),
                lifecycle="censored",
                result="right_censored",
                failure_domain="none",
                failure="",
                elapsed_ms=exposure_ms,
            )
        )
    late_ignored = len(censored_urls)
    probe_sequence_starved = bool(
        censored_urls or (deadline_hit and next_index < len(urls))
    )
    # Validator facts describe transport completion only. Whether a censored
    # full-budget endpoint is exhausted, retried, or deferred belongs to the
    # manager continuation policy.
    exhausted_urls = list(dict.fromkeys(completed_urls))
    next_probe_index = min(
        len(ordered_urls),
        start_index + len(exhausted_urls),
    )
    with facts_lock:
        outcome_facts = list(attempt_facts)
    return _MihomoRealProbeOutcome(
        result=success_result,
        failures=failures,
        failures_by_url=failures_by_url,
        attempted_urls=attempted_urls,
        elapsed_ms=elapsed_ms,
        deadline_hit=deadline_hit,
        probe_sequence_starved=probe_sequence_starved,
        late_completed_ignored=late_ignored,
        next_probe_index=next_probe_index,
        total_probe_count=len(ordered_urls),
        exposure_ms_by_url=exposure_ms_by_url,
        completed_urls=list(dict.fromkeys(completed_urls)),
        exhausted_urls=exhausted_urls,
        censored_urls=censored_urls,
        attempt_facts=outcome_facts,
        settled_in_cleanup_count=settled_in_cleanup_count,
    )


def _mihomo_session_real_probe_validate_node(
    client: MihomoAPIClient,
    node: Node,
    proxy_name: str,
    *,
    mixed_port: int,
    group_name: str = "TEST-BATCH",
    validation_profile: str = "session_batch",
    probe_urls: list[str],
    expected_statuses: set[int],
    probe_timeout_seconds: float,
    slow_threshold_ms: int,
    controller_delay_failure: str = "",
    semantic_mismatch: bool = False,
    deadline_at: float | None = None,
    probe_slot_deadline_seconds: float = 0.0,
    initial_parallelism: int = 2,
    probe_start_index: int = 0,
    deadline_failure_reason: str = "full_validation_probe_deadline",
    all_probe_urls: list[str] | None = None,
    previous_attempted_urls: list[str] | tuple[str, ...] | set[str] = (),
    previous_completed_urls: list[str] | tuple[str, ...] | set[str] = (),
    previous_exhausted_urls: list[str] | tuple[str, ...] | set[str] = (),
    previous_censored_urls: list[str] | tuple[str, ...] | set[str] = (),
    endpoint_health: _MihomoProbeEndpointHealth | None = None,
    generation_id: str = "",
    candidate_fingerprint: str = "",
    session_lease_id: str = "",
    probe_stage: str = "canonical",
    attempt_observer: Callable[[_MihomoProbeAttemptFact], None] | None = None,
    cleanup_settle_seconds: float = 0.0,
    generation_is_valid: Callable[[], bool] | None = None,
) -> Node | None:
    """Validate one session-loaded proxy through Mihomo mixed-port using legacy probe semantics.

    The session validator can use controller /delay as a fast path, but a /delay
    503/504 is not equivalent to legacy full validation failure. This helper
    keeps the optimized session process while reusing the old "real request via
    Mihomo mixed-port" admission contract.
    """
    if mixed_port <= 0:
        return None
    select_proxy = getattr(client, "select_proxy", None)
    if callable(select_proxy):
        try:
            select_proxy(group_name, proxy_name)
        except Exception:
            return None

    proxy_url = f"http://127.0.0.1:{int(mixed_port)}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    effective_deadline_at = deadline_at
    effective_probe_timeout_seconds = max(0.05, float(probe_timeout_seconds))
    if probe_slot_deadline_seconds > 0:
        effective_probe_timeout_seconds = min(
            effective_probe_timeout_seconds,
            max(0.05, float(probe_slot_deadline_seconds)),
        )
        # The entitlement starts when the transport thread actually starts.
        # The extra 100ms is only a bounded thread-start allowance, not probe
        # time that facility/reload work may consume.
        slot_deadline_at = (
            time.monotonic() + effective_probe_timeout_seconds + 0.10
        )
        if effective_deadline_at is None:
            effective_deadline_at = slot_deadline_at
        else:
            effective_deadline_at = min(float(effective_deadline_at), slot_deadline_at)
    probe_outcome = _run_mihomo_real_probe_attempts(
        opener,
        probe_urls,
        expected_statuses,
        probe_timeout_seconds=effective_probe_timeout_seconds,
        deadline_at=effective_deadline_at,
        initial_parallelism=max(1, int(initial_parallelism or 1)),
        probe_start_index=max(0, int(probe_start_index or 0)),
        generation_id=generation_id,
        candidate_fingerprint=candidate_fingerprint,
        session_lease_id=session_lease_id,
        probe_stage=probe_stage,
        attempt_observer=attempt_observer,
        cleanup_settle_seconds=cleanup_settle_seconds,
    )
    previous_attempted = [
        str(url)
        for url in previous_attempted_urls
        if str(url or "").strip()
    ]
    previous_completed = [
        str(url)
        for url in previous_completed_urls
        if str(url or "").strip()
    ]
    previous_exhausted = [
        str(url)
        for url in previous_exhausted_urls
        if str(url or "").strip()
    ]
    previous_censored = [
        str(url)
        for url in previous_censored_urls
        if str(url or "").strip()
    ]
    if previous_attempted and not (
        previous_completed or previous_exhausted or previous_censored
    ):
        # Migrate nodes created before the completed/censored contract. Those
        # versions treated every started URL as consumed.
        previous_completed = list(previous_attempted)
        previous_exhausted = list(previous_attempted)
    attempted_all = list(
        dict.fromkeys([*previous_attempted, *probe_outcome.attempted_urls])
    )
    completed_all = list(
        dict.fromkeys([*previous_completed, *probe_outcome.completed_urls])
    )
    exhausted_all = list(
        dict.fromkeys(
            [*previous_exhausted, *completed_all, *probe_outcome.exhausted_urls]
        )
    )
    censored_all = [
        url
        for url in dict.fromkeys([*previous_censored, *probe_outcome.censored_urls])
        if url not in exhausted_all
    ]
    complete_portfolio = list(
        dict.fromkeys(
            str(url)
            for url in (all_probe_urls if all_probe_urls is not None else probe_urls)
            if str(url or "").strip()
        )
    )
    remaining_urls = [url for url in complete_portfolio if url not in exhausted_all]
    generation_valid = generation_is_valid is None or bool(generation_is_valid())
    if endpoint_health is not None and generation_valid:
        endpoint_health.record(node, probe_outcome)
    endpoint_snapshot = endpoint_health.snapshot() if endpoint_health is not None else {}
    endpoint_outcomes: dict[str, str] = {}
    success_url = (
        probe_outcome.result.probe_url if probe_outcome.result is not None else ""
    )
    current_exhausted = set(probe_outcome.exhausted_urls)
    current_censored = set(probe_outcome.censored_urls)
    for attempted_url in probe_outcome.attempted_urls:
        if attempted_url == success_url:
            endpoint_outcomes[attempted_url] = "success"
            continue
        if attempted_url in current_censored and attempted_url not in current_exhausted:
            endpoint_outcomes[attempted_url] = "censored"
            continue
        failure = str(probe_outcome.failures_by_url.get(attempted_url) or "")
        if not failure:
            endpoint_outcomes[attempted_url] = (
                "deadline"
                if attempted_url in current_exhausted
                else "censored"
            )
        elif _real_probe_failure_is_transient(failure):
            endpoint_outcomes[attempted_url] = "transient"
        else:
            endpoint_outcomes[attempted_url] = "failure"

    def attach_probe_contract(updated: Node) -> Node:
        updated.extra["tier3_probe_urls_attempted_all"] = list(attempted_all)
        updated.extra["tier3_probe_urls_completed_all"] = list(completed_all)
        updated.extra["tier3_probe_urls_exhausted_all"] = list(exhausted_all)
        updated.extra["tier3_probe_urls_censored"] = list(censored_all)
        updated.extra["tier3_probe_urls_remaining"] = list(remaining_urls)
        prior_attempt_facts = [
            dict(value)
            for value in (node.extra.get("tier3_probe_attempt_facts") or [])
            if isinstance(value, dict)
        ]
        updated.extra["tier3_probe_attempt_facts"] = [
            *prior_attempt_facts,
            *[fact.to_dict() for fact in probe_outcome.attempt_facts],
        ][-16:]
        # Retain these fields for report compatibility while manager scheduling
        # moves to the URL-set contract.
        updated.extra["tier3_probe_cursor_next"] = len(exhausted_all)
        updated.extra["tier3_probe_total_count"] = len(complete_portfolio)
        updated.extra["validation_probe_endpoint_health"] = endpoint_snapshot
        updated.extra["validation_probe_endpoint_outcomes"] = dict(endpoint_outcomes)
        updated.extra["validation_probe_endpoint_elapsed_ms"] = {
            str(url): round(max(0.0, float(elapsed_ms)), 3)
            for url, elapsed_ms in probe_outcome.exposure_ms_by_url.items()
        }
        updated.extra["tier3_probe_endpoint_cooldown_count"] = sum(
            1 for value in endpoint_snapshot.values() if value.get("cooldown")
        )
        updated.extra["tier3_probe_endpoint_half_open_count"] = sum(
            1 for value in endpoint_snapshot.values() if value.get("half_open")
        )
        if not generation_valid:
            updated.extra["tier3_validator_generation_stale"] = True
        return updated

    if probe_outcome.result is not None:
        latency_ms = probe_outcome.result.latency_ms
        updated = node.clone()
        updated.status = _healthy_status(latency_ms, slow_threshold_ms)
        updated.latency_ms = round(latency_ms, 2)
        updated.suspicious_reason = None
        updated.extra.pop("tier3_failure_stage", None)
        updated.extra.pop("tier3_failure_reason", None)
        updated.extra.pop("tier3_full_validation_deadline_hit", None)
        checked_at = utc_now_iso()
        updated.extra["validated_by"] = "mihomo-batch"
        updated.extra["validation_profile"] = validation_profile
        updated.extra["validation_probe_url"] = probe_outcome.result.probe_url
        updated.extra["validation_probe_urls"] = list(probe_urls)
        updated.extra["validation_probe_urls_attempted"] = list(probe_outcome.attempted_urls)
        updated.extra["validation_probe_failures_by_url"] = dict(probe_outcome.failures_by_url)
        updated.extra["validation_proxy_name"] = proxy_name
        updated.extra["validation_verified_at"] = checked_at
        updated.extra["validation_runtime_profile"] = "mihomo-session-mixed-port"
        updated.extra["session_validator_real_probe_attempted"] = True
        if probe_slot_deadline_seconds > 0:
            updated.extra["tier3_probe_slot_deadline_seconds"] = float(probe_slot_deadline_seconds)
        updated.extra["session_validator_real_probe_elapsed_ms"] = probe_outcome.elapsed_ms
        updated.extra["session_validator_late_probe_results_ignored"] = (
            probe_outcome.late_completed_ignored
        )
        updated.extra["session_validator_probe_sequence_starved"] = (
            probe_outcome.probe_sequence_starved
        )
        updated.extra["tier3_probe_settled_in_cleanup_count"] = int(
            probe_outcome.settled_in_cleanup_count
        )
        updated.extra.pop("tier3_probe_urls_remaining", None)
        updated.extra.pop("tier3_canonical_probe_continuation_count", None)
        if controller_delay_failure:
            updated.extra["session_validator_controller_delay_failure"] = controller_delay_failure
        if semantic_mismatch:
            updated.extra["session_validator_semantic_mismatch"] = True
        updated.last_checked_at = checked_at
        return attach_probe_contract(updated)

    if probe_outcome.deadline_hit:
        # Distinguish the two deadlines that can fire here.  An external wave
        # deadline cuts the probe short for scheduling reasons, so the node was
        # never judged and "deferred" is correct.  A slot entitlement expiring
        # with no wave deadline in play means the node consumed its whole probe
        # allowance and never answered, which is node-attributable evidence.
        # Reporting that as "deferred" made it permanently unjudgeable: it is
        # treated as transient, never counted as a failure, and refused by the
        # negative cache, so the same unresponsive candidates are re-probed
        # every wave and crowd out candidates that were never tried.
        entitlement_exhausted = (
            deadline_at is None and probe_slot_deadline_seconds > 0
        )
        if entitlement_exhausted:
            result = _mihomo_validation_failure(
                node,
                "mihomo_batch_validation_failed: "
                f"{deadline_failure_reason or 'full_validation_probe_deadline'} "
                f"exhausted {float(probe_slot_deadline_seconds):.2f}s probe "
                "entitlement without a response",
            )
            result.extra["tier3_probe_entitlement_exhausted"] = True
        else:
            result = _mihomo_validation_failure(
                node,
                f"validation_deferred: {deadline_failure_reason or 'full_validation_probe_deadline'}",
            )
        result.extra["tier3_full_validation_deadline_hit"] = True
    else:
        result = _mihomo_real_probe_failure_result(node, probe_outcome.failures)
    result.extra["session_validator_real_probe_attempted"] = True
    if probe_slot_deadline_seconds > 0:
        result.extra["tier3_probe_slot_deadline_seconds"] = float(probe_slot_deadline_seconds)
    result.extra["validation_profile"] = validation_profile
    result.extra["validation_probe_urls"] = list(probe_urls)
    result.extra["validation_probe_urls_attempted"] = list(probe_outcome.attempted_urls)
    result.extra["validation_probe_failures_by_url"] = dict(probe_outcome.failures_by_url)
    result.extra["session_validator_real_probe_elapsed_ms"] = probe_outcome.elapsed_ms
    result.extra["session_validator_late_probe_results_ignored"] = probe_outcome.late_completed_ignored
    result.extra["session_validator_probe_sequence_starved"] = probe_outcome.probe_sequence_starved
    result.extra["tier3_probe_settled_in_cleanup_count"] = int(
        probe_outcome.settled_in_cleanup_count
    )
    if controller_delay_failure:
        result.extra["session_validator_controller_delay_failure"] = controller_delay_failure
    return attach_probe_contract(result)


def _mihomo_delay_validate_node(
    client: MihomoAPIClient,
    node: Node,
    proxy_name: str,
    *,
    probe_urls: list[str],
    mixed_port: int = 0,
    expected_statuses: set[int] | None = None,
    probe_timeout_ms: int,
    slow_threshold_ms: int,
    deadline_at: float | None = None,
    real_probe_initial_parallelism: int = 2,
    real_probe_deadline_reason: str = "full_validation_probe_deadline",
) -> Node:
    failures: list[str] = []
    expected = set(expected_statuses or {200, 204})
    probe_timeout_seconds = max(1.0, float(probe_timeout_ms) / 1000.0)
    for probe_url in probe_urls:
        effective_timeout_ms = int(probe_timeout_ms)
        if deadline_at is not None:
            remaining_seconds = float(deadline_at) - time.monotonic()
            if remaining_seconds <= 0.0:
                updated = _mihomo_validation_failure(
                    node,
                    "validation_deferred: full_validation_wave_deadline",
                )
                updated.extra["tier3_full_validation_deadline_hit"] = True
                return updated
            if remaining_seconds < 1.0:
                updated = _mihomo_validation_failure(
                    node,
                    "validation_deferred: full_validation_wave_deadline",
                )
                updated.extra["tier3_full_validation_deadline_hit"] = True
                return updated
            effective_timeout_ms = max(
                1000,
                min(effective_timeout_ms, int(max(1.0, remaining_seconds) * 1000)),
            )
        try:
            result = client.proxy_delay(proxy_name, url=probe_url, timeout_ms=effective_timeout_ms)
            raw_delay = result.get("delay") if isinstance(result, dict) else None
            latency_ms = float(raw_delay)
            if latency_ms >= 0:
                updated = node.clone()
                updated.status = _healthy_status(latency_ms, slow_threshold_ms)
                updated.latency_ms = round(latency_ms, 2)
                updated.suspicious_reason = None
                checked_at = utc_now_iso()
                updated.extra["validated_by"] = "mihomo-batch"
                updated.extra["validation_probe_url"] = probe_url
                updated.extra["validation_probe_urls"] = list(probe_urls)
                updated.extra["validation_proxy_name"] = proxy_name
                updated.extra["validation_verified_at"] = checked_at
                updated.extra["validation_runtime_profile"] = "mihomo-batch-proxy-delay"
                updated.last_checked_at = checked_at
                return updated
            failures.append(f"{probe_url}: invalid delay={raw_delay}")
        except Exception as exc:
            failure_text = f"{probe_url}: {exc}"
            failures.append(failure_text)
            if _http_error_code_from_text(exc) in {503, 504}:
                real_probe_result = _mihomo_session_real_probe_validate_node(
                    client,
                    node,
                    proxy_name,
                    mixed_port=mixed_port,
                    probe_urls=probe_urls,
                    expected_statuses=expected,
                    probe_timeout_seconds=probe_timeout_seconds,
                    slow_threshold_ms=slow_threshold_ms,
                    controller_delay_failure=failure_text,
                    semantic_mismatch=True,
                    deadline_at=deadline_at,
                    initial_parallelism=real_probe_initial_parallelism,
                    deadline_failure_reason=real_probe_deadline_reason,
                )
                if real_probe_result is not None:
                    return real_probe_result
    return _mihomo_proxy_delay_failure_result(node, failures)




def _mihomo_batch_validate(
    nodes: list[Node],
    testing_config: dict[str, Any],
    slow_threshold_ms: int,
    logger: logging.Logger,
    *,
    result_observer: Callable[[Node], None] | None = None,
    max_workers: int = 1,
    cancel_event: Any | None = None,
) -> list[Node]:
    if not nodes:
        return []
    result_observer = _cancellation_filtered_result_observer(
        result_observer,
        cancel_event,
    )
    if _validation_cancel_requested(cancel_event):
        results = _mihomo_validation_cancelled_results(nodes)
        return results
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    no_unsupported_fallback = coerce_bool(
        testing_config.get("tier3_no_unsupported_fallback")
        or helper_config.get("tier3_no_unsupported_fallback"),
        False,
    )
    binary_path = _mihomo_binary_path(testing_config)
    if not binary_path:
        results = [
            _mihomo_validation_failure(node, "Mihomo validation requires mihomo_binary_path")
            for node in nodes
        ]
        _notify_many_result_observer(result_observer, results, logger)
        return results
    if not Path(binary_path).exists():
        results = [
            _mihomo_validation_failure(node, f"mihomo binary not found: {binary_path}")
            for node in nodes
        ]
        _notify_many_result_observer(result_observer, results, logger)
        return results

    expected_statuses = _mihomo_expected_statuses(testing_config)
    if not expected_statuses.issubset({200, 204}):
        return _mihomo_direct_validate_many(
            nodes,
            testing_config,
            slow_threshold_ms,
            logger,
            result_observer=result_observer,
            max_workers=max_workers,
            reason=f"custom expected statuses {sorted(expected_statuses)}",
        )

    probe_urls = _mihomo_probe_urls(testing_config)
    probe_timeout = _mihomo_probe_timeout(testing_config)
    probe_timeout_ms = max(1000, int(round(probe_timeout * 1000)))
    real_probe_initial_parallelism = _config_int(
        testing_config.get("tier3_probe_initial_parallelism")
        or helper_config.get("tier3_probe_initial_parallelism"),
        3,
        minimum=1,
        maximum=8,
    )
    real_probe_deadline_reason = str(
        testing_config.get("tier3_real_probe_deadline_reason")
        or helper_config.get("tier3_real_probe_deadline_reason")
        or "full_validation_probe_deadline"
    ).strip() or "full_validation_probe_deadline"
    validation_deadline_seconds = _config_float(
        testing_config.get("tier3_validation_deadline_seconds")
        or helper_config.get("tier3_validation_deadline_seconds"),
        0.0,
        minimum=0.0,
    )
    startup_timeout = float(helper_config.get("startup_timeout") or 8)
    port_attempts = max(1, int(helper_config.get("port_attempts", 3)))
    failures: list[str] = []
    for _attempt in range(port_attempts):
        mixed_socket = _reserve_free_port()
        controller_socket = _reserve_free_port()
        mixed_port = int(mixed_socket.getsockname()[1])
        controller_port = int(controller_socket.getsockname()[1])
        mihomo_config, entries, unsupported = _build_mihomo_batch_config(
            nodes,
            mixed_port,
            controller_port,
            logger,
            ipv6=coerce_bool(testing_config.get("ipv6", False), False),
        )
        if not mihomo_config or not entries:
            try:
                mixed_socket.close()
                controller_socket.close()
            finally:
                if no_unsupported_fallback:
                    results = _mihomo_unsupported_results(nodes)
                    _notify_many_result_observer(result_observer, results, logger)
                    return results
                return _mihomo_direct_validate_many(
                    nodes,
                    testing_config,
                    slow_threshold_ms,
                    logger,
                    result_observer=result_observer,
                    max_workers=max_workers,
                    reason="unsupported proxy batch",
                )
        process: subprocess.Popen[str] | None = None
        temp_dir_handle: tempfile.TemporaryDirectory[str] | None = None
        try:
            temp_dir_handle = _temporary_validation_dir("pproxy-mihomo-batch-", testing_config)
            temp_dir = temp_dir_handle.name
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(yaml.safe_dump(mihomo_config, allow_unicode=True, sort_keys=False), encoding="utf-8")
            mixed_socket.close()
            controller_socket.close()
            process = subprocess.Popen(
                _mihomo_command(binary_path, str(config_path), temp_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            )
            _wait_for_tcp_port(
                "127.0.0.1",
                controller_port,
                startup_timeout,
                cancel_event=cancel_event,
            )
            client = MihomoAPIClient(
                base_url=f"http://127.0.0.1:{controller_port}",
                timeout=max(3.0, probe_timeout + 2.0),
            )
            results: list[Node] = []
            worker_count = max(1, min(len(entries), int(max_workers)))
            deadline_at = (
                time.monotonic() + validation_deadline_seconds
                if validation_deadline_seconds > 0
                else None
            )
            executor: concurrent.futures.ThreadPoolExecutor | None = concurrent.futures.ThreadPoolExecutor(
                max_workers=worker_count
            )
            try:
                future_nodes = {
                    executor.submit(
                        _mihomo_delay_validate_node,
                        client,
                        node,
                        proxy_name,
                        probe_urls=probe_urls,
                        mixed_port=mixed_port,
                        expected_statuses=expected_statuses,
                        probe_timeout_ms=probe_timeout_ms,
                        slow_threshold_ms=slow_threshold_ms,
                        deadline_at=deadline_at,
                        real_probe_initial_parallelism=real_probe_initial_parallelism,
                        real_probe_deadline_reason=real_probe_deadline_reason,
                    ): node
                    for node, proxy_name in entries
                }
                pending = set(future_nodes)
                while pending:
                    if deadline_at is not None and time.monotonic() >= deadline_at:
                        deferred_nodes = [future_nodes[future] for future in pending]
                        for future in pending:
                            future.cancel()
                        deferred_results = _mihomo_validation_deferred_results(
                            deferred_nodes,
                            "full_validation_wave_deadline",
                        )
                        for deferred in deferred_results:
                            deferred.extra["tier3_full_validation_deadline_seconds"] = (
                                validation_deadline_seconds
                            )
                        results.extend(deferred_results)
                        _notify_many_result_observer(result_observer, deferred_results, logger)
                        log_raw_diagnostic(
                            logger,
                            "warning",
                            "Bastion Layer batch Mihomo validation wave deadline reached "
                            "(pending=%s deadline=%.2fs)",
                            len(deferred_results),
                            validation_deadline_seconds,
                        )
                        executor.shutdown(wait=False, cancel_futures=True)
                        executor = None
                        _stop_process_bounded(process)
                        process = None
                        return results
                    if _validation_cancel_requested(cancel_event):
                        cancelled_nodes = [
                            future_nodes[future]
                            for future in pending
                            if future.cancel()
                        ]
                        running_nodes = [
                            future_nodes[future]
                            for future in pending
                            if not future.cancelled()
                        ]
                        cancelled_results = _mihomo_validation_cancelled_results(
                            [*cancelled_nodes, *running_nodes]
                        )
                        results.extend(cancelled_results)
                        executor.shutdown(wait=False, cancel_futures=True)
                        executor = None
                        return results
                    wait_timeout = 0.1
                    if deadline_at is not None:
                        wait_timeout = max(0.01, min(0.1, deadline_at - time.monotonic()))
                    done, pending = concurrent.futures.wait(
                        pending,
                        timeout=wait_timeout,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        try:
                            updated = future.result()
                        except Exception as exc:
                            updated = _mihomo_validation_failure(
                                future_nodes[future],
                                f"mihomo_batch_validation_failed: {exc}",
                            )
                        if updated.extra.get("tier3_full_validation_deadline_hit"):
                            updated.extra["tier3_full_validation_deadline_seconds"] = (
                                validation_deadline_seconds
                            )
                        results.append(updated)
                        _notify_result_observer(result_observer, updated, logger)
            finally:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=False)
            if unsupported:
                if no_unsupported_fallback:
                    unsupported_results = _mihomo_unsupported_results(unsupported)
                    results.extend(unsupported_results)
                    _notify_many_result_observer(result_observer, unsupported_results, logger)
                    return results
                results.extend(
                    _mihomo_direct_validate_many(
                        unsupported,
                        testing_config,
                        slow_threshold_ms,
                        logger,
                        result_observer=result_observer,
                        max_workers=max_workers,
                        reason="unsupported proxy in batch",
                    )
                )
            return results
        except Exception as exc:
            if _validation_cancel_requested(cancel_event):
                results = _mihomo_validation_cancelled_results(nodes)
                return results
            log_raw_diagnostic(
                logger,
                "warning",
                "Bastion Layer batch Mihomo validation failed; will fall back to per-node validation "
                "(nodes=%s): %s",
                len(nodes),
                exc,
            )
            failures.append(str(exc))
        finally:
            for reserved_socket in (mixed_socket, controller_socket):
                try:
                    reserved_socket.close()
                except OSError:
                    pass
            if _validation_cancel_requested(cancel_event):
                _stop_process_bounded(process)
            else:
                _stop_process(process)
            if temp_dir_handle is not None:
                temp_dir_handle.cleanup()

    return _mihomo_direct_validate_many(
        nodes,
        testing_config,
        slow_threshold_ms,
        logger,
        result_observer=result_observer,
        max_workers=max_workers,
        reason="batch startup failed: " + ("; ".join(failures[:3]) or "unknown"),
    )


# Verdicts of ``confirm_tier3_nodes_mainstream_https``.
TIER3_CONFIRM_PASS = "pass"
TIER3_CONFIRM_FAIL = "fail"
TIER3_CONFIRM_CENSORED = "censored"
TIER3_CONFIRM_FACILITY = "facility"


def _confirm_one_proxy_mainstream_https(
    client: MihomoAPIClient,
    proxy_name: str,
    confirm_urls: list[str],
    timeout_ms: int,
    cancel_event: Any | None,
) -> tuple[str, str]:
    for url in confirm_urls:
        # A failed URL test (reset/EOF, a 5xx, a wrong status) is sampled once
        # more before it is a verdict, like the process validator's confirm.
        for sample in range(2):
            if _validation_cancel_requested(cancel_event):
                return TIER3_CONFIRM_FACILITY, "validation_cancelled"
            try:
                client.proxy_delay(
                    proxy_name,
                    url=url,
                    timeout_ms=timeout_ms,
                    expected_status="204",
                    verify_expected_status=True,
                )
                break
            except MihomoDelayTestFailed as exc:
                text = str(exc)
                if "HTTP 504" in text:
                    return TIER3_CONFIRM_CENSORED, f"{url}: timeout"
                if "HTTP 503" in text and sample == 0:
                    continue
                return TIER3_CONFIRM_FAIL, f"{url}: {text}"[:256]
            except MihomoAPIError as exc:
                return TIER3_CONFIRM_FACILITY, f"{url}: {exc}"[:256]
    return TIER3_CONFIRM_PASS, ""


def confirm_tier3_nodes_mainstream_https(
    nodes: list[Node],
    testing_config: dict[str, Any],
    confirm_urls: list[str] | tuple[str, ...],
    logger: logging.Logger,
    *,
    max_workers: int = 8,
    per_url_timeout_seconds: float = 5.0,
) -> list[tuple[str, str]]:
    """Hold already-validated nodes to the Tier3 mainstream HTTPS gate.

    The process validator admits a node only after it loads every confirm URL
    (default Google ``generate_204``) over verified HTTPS; the legacy recheck
    path proves only the neutral probe URLs.  All nodes are loaded into ONE
    Mihomo (one process per call, not per node) whose controller URL-tests
    each confirm URL through each node with ``expected=204`` -- the same
    Mihomo URL test that TIER3-AUTO's route health check runs, so a recheck
    agrees with the live route's own judgement.

    Returns one ``(verdict, detail)`` per input node, in order: ``pass``;
    ``fail`` (a wrong status, or a failed test that repeated); ``censored``
    (the test timed out); ``facility`` (nothing was judged: no binary, an
    unsupported proxy, controller trouble, cancellation).
    """

    urls = [str(url).strip() for url in confirm_urls or () if str(url or "").strip()]
    if not nodes:
        return []
    if not urls:
        return [(TIER3_CONFIRM_PASS, "")] * len(nodes)
    cancel_event = testing_config.get("validation_cancel_event")
    binary_path = _mihomo_binary_path(testing_config)
    if not binary_path or not Path(binary_path).exists():
        return [(TIER3_CONFIRM_FACILITY, "mihomo_binary_unavailable")] * len(nodes)
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    startup_timeout = float(helper_config.get("startup_timeout") or 8)
    timeout_ms = int(round(max(0.25, min(5.0, float(per_url_timeout_seconds))) * 1000))
    results: list[tuple[str, str]] = [
        (TIER3_CONFIRM_FACILITY, "unsupported_proxy")
    ] * len(nodes)
    mixed_socket = _reserve_free_port()
    controller_socket = _reserve_free_port()
    mixed_port = int(mixed_socket.getsockname()[1])
    controller_port = int(controller_socket.getsockname()[1])
    process: subprocess.Popen[Any] | None = None
    temp_dir_handle: _ValidationTempDir | None = None
    try:
        mihomo_config, entries, _unsupported = _build_mihomo_batch_config(
            nodes,
            mixed_port,
            controller_port,
            logger,
            ipv6=coerce_bool(testing_config.get("ipv6", False), False),
            proxy_name_prefix="TEST-CONFIRM",
        )
        if not mihomo_config or not entries:
            return results
        index_by_node = {id(node): index for index, node in enumerate(nodes)}
        temp_dir_handle = _temporary_validation_dir("pproxy-mihomo-confirm-", testing_config)
        config_path = Path(temp_dir_handle.name) / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(mihomo_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        mixed_socket.close()
        controller_socket.close()
        process = subprocess.Popen(
            _mihomo_command(binary_path, str(config_path), temp_dir_handle.name),
            # Nobody reads this process's output; a full pipe must never
            # stall its URL tests.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
        _wait_for_tcp_port(
            "127.0.0.1",
            controller_port,
            startup_timeout,
            cancel_event=cancel_event,
        )
        client = MihomoAPIClient(
            base_url=f"http://127.0.0.1:{controller_port}",
            timeout=timeout_ms / 1000.0 + 2.0,
        )
        worker_count = max(1, min(len(entries), int(max_workers)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    _confirm_one_proxy_mainstream_https,
                    client,
                    proxy_name,
                    urls,
                    timeout_ms,
                    cancel_event,
                ): index_by_node[id(node)]
                for node, proxy_name in entries
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception as exc:
                    results[futures[future]] = (
                        TIER3_CONFIRM_FACILITY,
                        f"confirm_exception: {exc}"[:256],
                    )
        return results
    except Exception as exc:
        log_raw_diagnostic(
            logger,
            "warning",
            "Bastion Layer mainstream HTTPS confirm could not run (nodes=%s): %s",
            len(nodes),
            exc,
        )
        return [(TIER3_CONFIRM_FACILITY, f"confirm_facility: {exc}"[:256])] * len(nodes)
    finally:
        for reserved_socket in (mixed_socket, controller_socket):
            try:
                reserved_socket.close()
            except OSError:
                pass
        _stop_process(process)
        if temp_dir_handle is not None:
            temp_dir_handle.cleanup()


def validate_tier3_responsive_nodes_with_mihomo_batch(
    nodes: list[Node],
    testing_config: dict[str, Any],
    logger: logging.Logger,
    *,
    concurrency: int,
    result_observer: Callable[[Node], None] | None = None,
) -> list[Node]:
    if not nodes:
        return []
    cancel_event = testing_config.get("validation_cancel_event")
    result_observer = _cancellation_filtered_result_observer(
        result_observer,
        cancel_event,
    )
    slow_threshold_ms = slow_threshold_ms_from_testing(testing_config)
    session = testing_config.get("mihomo_validation_session")
    if session is not None and hasattr(session, "validate"):
        try:
            return session.validate(
                nodes,
                result_observer=result_observer,
                max_workers=max(1, int(concurrency)),
                cancel_event=cancel_event,
            )
        except TypeError:
            return session.validate(
                nodes,
                result_observer=result_observer,
                max_workers=max(1, int(concurrency)),
            )
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    if not (
        coerce_bool(helper_config.get("enabled", True), True)
        and coerce_bool(helper_config.get("batch_enabled", True), True)
    ):
        return _mihomo_direct_validate_many(
            nodes,
            testing_config,
            slow_threshold_ms,
            logger,
            result_observer=result_observer,
            max_workers=max(1, int(concurrency)),
            reason="batch helper disabled",
        )
    return _mihomo_batch_validate(
        nodes,
        testing_config,
        slow_threshold_ms,
        logger,
        result_observer=result_observer,
        max_workers=max(1, int(concurrency)),
        cancel_event=cancel_event,
    )


def _mihomo_direct_validate(
    node: Node,
    testing_config: dict[str, Any],
    slow_threshold_ms: int,
    logger: logging.Logger,
    *,
    _probe_urls_override: list[str] | tuple[str, ...] | None = None,
) -> Node:
    updated = node.clone()
    _clear_latency_policy_extra(updated)
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    binary_path = str(
        helper_config.get("binary_path")
        or testing_config.get("mihomo_binary_path")
        or testing_config.get("_mihomo_binary_path")
        or ""
    )
    if not binary_path:
        updated.status = NodeStatus.SUSPICIOUS
        updated.latency_ms = None
        updated.suspicious_reason = "Mihomo validation requires mihomo_binary_path"
        updated.last_checked_at = utc_now_iso()
        return updated
    if not Path(binary_path).exists():
        updated.status = NodeStatus.SUSPICIOUS
        updated.latency_ms = None
        updated.suspicious_reason = f"mihomo binary not found: {binary_path}"
        updated.last_checked_at = utc_now_iso()
        return updated

    probe_urls = (
        [str(item) for item in _probe_urls_override if str(item)]
        if _probe_urls_override is not None
        else _mihomo_probe_urls(testing_config)
    )
    expected_statuses = _mihomo_expected_statuses(testing_config)
    probe_timeout = _mihomo_probe_timeout(testing_config)
    startup_timeout = float(helper_config.get("startup_timeout") or 8)
    failures: list[str] = []
    port_attempts = max(1, int(helper_config.get("port_attempts", 3)))
    for _attempt in range(port_attempts):
        reserved_port = _reserve_free_port()
        inbound_port = int(reserved_port.getsockname()[1])
        mihomo_config = _build_mihomo_direct_config(
            updated,
            inbound_port,
            logger,
            ipv6=coerce_bool(testing_config.get("ipv6", False), False),
        )
        if not mihomo_config:
            reserved_port.close()
            failures.append("unsupported proxy")
            break
        process: subprocess.Popen[str] | None = None
        temp_dir_handle: tempfile.TemporaryDirectory[str] | None = None
        validation_started = time.perf_counter()
        try:
            temp_dir_handle = _temporary_validation_dir("pproxy-mihomo-direct-", testing_config)
            temp_dir = temp_dir_handle.name
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(yaml.safe_dump(mihomo_config, allow_unicode=True, sort_keys=False), encoding="utf-8")
            reserved_port.close()
            process = subprocess.Popen(
                _mihomo_command(binary_path, str(config_path), temp_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            )
            _wait_for_tcp_port("127.0.0.1", inbound_port, startup_timeout)
            helper_ready_at = time.perf_counter()
            proxy_url = f"http://127.0.0.1:{inbound_port}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            probe_failures: list[str] = []
            for probe_url in probe_urls:
                try:
                    request_started = time.perf_counter()
                    request = urllib.request.Request(probe_url, method="GET")
                    with opener.open(request, timeout=probe_timeout) as response:
                        status_code = int(getattr(response, "status", None) or response.getcode())
                        body = response.read(128)
                    request_finished = time.perf_counter()
                    latency_ms = (request_finished - request_started) * 1000
                    if status_code in expected_statuses and _probe_body_ok(status_code, body):
                        updated = apply_success_latency_policy(
                            updated,
                            latency_ms,
                            testing_config,
                            "mihomo_direct",
                            slow_threshold_ms=slow_threshold_ms,
                        )
                        updated.extra["validated_by"] = "mihomo"
                        updated.extra["validation_probe_url"] = probe_url
                        updated.extra["validation_helper_startup_elapsed_ms"] = round(
                            (helper_ready_at - validation_started) * 1000,
                            2,
                        )
                        updated.extra["validation_total_elapsed_ms"] = round(
                            (request_finished - validation_started) * 1000,
                            2,
                        )
                        updated.last_checked_at = utc_now_iso()
                        return updated
                    probe_failures.append(f"{probe_url}: unexpected probe status={status_code}")
                except Exception as exc:
                    probe_failures.append(f"{probe_url}: {exc}")
            failures.append("; ".join(probe_failures[:3]) or "all probe urls failed")
        except Exception as exc:
            log_raw_diagnostic(
                logger,
                "warning",
                "Mihomo validation failed for %s:%s: %s",
                node.address,
                node.port,
                exc,
            )
            failures.append(str(exc))
        finally:
            try:
                reserved_port.close()
            except OSError:
                pass
            _stop_process(process)
            if temp_dir_handle is not None:
                temp_dir_handle.cleanup()

    updated.status = NodeStatus.SUSPICIOUS
    updated.latency_ms = None
    updated.suspicious_reason = "mihomo_validation_failed: " + ("; ".join(failures[:3]) or "probe failed")
    updated.last_checked_at = utc_now_iso()
    return updated


def _gateway_chain_validate(
    node: Node,
    gateway_nodes: list[Node],
    testing_config: dict[str, Any],
    slow_threshold_ms: int,
    logger: logging.Logger,
) -> Node:
    updated = node.clone()
    _clear_latency_policy_extra(updated)
    helper_config = helper_block_from_testing(testing_config, "mihomo_helper")
    binary_path = str(
        helper_config.get("binary_path")
        or testing_config.get("mihomo_binary_path")
        or testing_config.get("_mihomo_binary_path")
        or ""
    )
    if not binary_path:
        updated.status = NodeStatus.SUSPICIOUS
        updated.latency_ms = None
        updated.suspicious_reason = "Gateway chain validation requires mihomo_binary_path"
        updated.last_checked_at = utc_now_iso()
        return updated
    if not Path(binary_path).exists():
        updated.status = NodeStatus.SUSPICIOUS
        updated.latency_ms = None
        updated.suspicious_reason = f"mihomo binary not found: {binary_path}"
        updated.last_checked_at = utc_now_iso()
        return updated

    probe_urls = _mihomo_probe_urls(testing_config)
    expected_statuses = _mihomo_expected_statuses(testing_config)
    probe_timeout = float(helper_config.get("probe_timeout") or testing_config.get("request_timeout") or 8)
    startup_timeout = float(helper_config.get("startup_timeout") or 8)
    gateways = [
        gateway
        for gateway in sort_nodes_for_use(gateway_nodes)
        if gateway.status not in {NodeStatus.DEAD, NodeStatus.SUSPICIOUS}
    ] or sort_nodes_for_use(gateway_nodes)
    if not gateways:
        updated.status = NodeStatus.SUSPICIOUS
        updated.latency_ms = None
        updated.suspicious_reason = "Gateway chain validation requires at least one Gateway node"
        updated.last_checked_at = utc_now_iso()
        return updated

    failures: list[str] = []
    port_attempts = max(1, int(helper_config.get("port_attempts", 3)))
    for gateway in gateways:
        gateway_failures: list[str] = []
        for _attempt in range(port_attempts):
            reserved_port = _reserve_free_port()
            inbound_port = int(reserved_port.getsockname()[1])
            mihomo_config = _build_mihomo_chain_config(
                updated,
                gateway,
                inbound_port,
                logger,
                ipv6=coerce_bool(testing_config.get("ipv6", False), False),
            )
            if not mihomo_config:
                reserved_port.close()
                gateway_failures.append("unsupported chain proxy")
                break
            process: subprocess.Popen[str] | None = None
            temp_dir_handle: tempfile.TemporaryDirectory[str] | None = None
            validation_started = time.perf_counter()
            try:
                temp_dir_handle = _temporary_validation_dir("pproxy-mihomo-chain-", testing_config)
                temp_dir = temp_dir_handle.name
                config_path = Path(temp_dir) / "config.yaml"
                config_path.write_text(yaml.safe_dump(mihomo_config, allow_unicode=True, sort_keys=False), encoding="utf-8")
                reserved_port.close()
                process = subprocess.Popen(
                    _mihomo_command(binary_path, str(config_path), temp_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **hidden_subprocess_kwargs(),
                )
                _wait_for_tcp_port("127.0.0.1", inbound_port, startup_timeout)
                helper_ready_at = time.perf_counter()
                proxy_url = f"http://127.0.0.1:{inbound_port}"
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                )
                probe_failures: list[str] = []
                for probe_url in probe_urls:
                    try:
                        request_started = time.perf_counter()
                        request = urllib.request.Request(str(probe_url), method="GET")
                        with opener.open(request, timeout=probe_timeout) as response:
                            status_code = int(getattr(response, "status", None) or response.getcode())
                            body = response.read(128)
                        request_finished = time.perf_counter()
                        latency_ms = (request_finished - request_started) * 1000
                        if status_code in expected_statuses and _probe_body_ok(status_code, body):
                            updated = apply_success_latency_policy(
                                updated,
                                latency_ms,
                                testing_config,
                                "gateway_chain",
                                slow_threshold_ms=slow_threshold_ms,
                            )
                            updated.extra["tested_via_gateway"] = gateway.name or f"{gateway.address}:{gateway.port}"
                            updated.extra["validation_probe_url"] = probe_url
                            updated.extra["validation_helper_startup_elapsed_ms"] = round(
                                (helper_ready_at - validation_started) * 1000,
                                2,
                            )
                            updated.extra["validation_total_elapsed_ms"] = round(
                                (request_finished - validation_started) * 1000,
                                2,
                            )
                            updated.last_checked_at = utc_now_iso()
                            return updated
                        probe_failures.append(f"{probe_url}: unexpected probe status={status_code}")
                    except Exception as exc:
                        probe_failures.append(f"{probe_url}: {exc}")
                gateway_failures.append("; ".join(probe_failures[:3]) or "all probe urls failed")
            except Exception as exc:
                log_raw_diagnostic(
                    logger,
                    "warning",
                    "Gateway chain validation failed for %s via %s: %s",
                    node.name or node.address,
                    gateway.name or gateway.address,
                    exc,
                )
                gateway_failures.append(str(exc))
            finally:
                try:
                    reserved_port.close()
                except OSError:
                    pass
                _stop_process(process)
                if temp_dir_handle is not None:
                    temp_dir_handle.cleanup()
        failures.append(f"{gateway.name or gateway.address}: {gateway_failures[-1] if gateway_failures else 'validation failed'}")

    updated.status = NodeStatus.SUSPICIOUS
    updated.latency_ms = None
    updated.suspicious_reason = "all_gateways_unavailable: " + ("; ".join(failures[:3]) or "Gateway chain validation failed")
    updated.last_checked_at = utc_now_iso()
    return updated


async def _probe_with_optional_validation(
    node: Node,
    semaphore: asyncio.Semaphore,
    timeout_seconds: float,
    slow_threshold_ms: int,
    full_validation_enabled: bool,
    helper_config: dict[str, Any],
    gateway_nodes: list[Node] | None,
    testing_config: dict[str, Any],
    logger: logging.Logger,
    cancel_event: Any | None = None,
) -> Node:
    async with semaphore:
        try:
            if _validation_cancel_requested(cancel_event):
                return _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
            if gateway_nodes and node.tier != "exit":
                updated = await asyncio.to_thread(
                    _gateway_chain_validate,
                    node,
                    gateway_nodes,
                    testing_config,
                    slow_threshold_ms,
                    logger,
                )
                if _validation_cancel_requested(cancel_event):
                    return _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
                return updated
            updated = await _tcp_probe(
                node,
                timeout_seconds=timeout_seconds,
                slow_threshold_ms=slow_threshold_ms,
                testing_config=testing_config,
            )
            if _validation_cancel_requested(cancel_event):
                return _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
            if full_validation_enabled and updated.status in {NodeStatus.ALIVE, NodeStatus.SLOW}:
                mihomo_helper = testing_config.get("mihomo_helper", {})
                mihomo_helper = mihomo_helper if isinstance(mihomo_helper, dict) else {}
                if node.tier == "tier3" and coerce_bool(mihomo_helper.get("enabled", True), True):
                    updated = await asyncio.to_thread(
                        _mihomo_direct_validate,
                        updated,
                        testing_config,
                        slow_threshold_ms,
                        logger,
                    )
                elif coerce_bool(helper_config.get("enabled", False), False):
                    status, reason, latency_ms = await asyncio.to_thread(
                        _full_validate,
                        updated,
                        helper_config,
                        logger,
                    )
                    if reason is None and latency_ms is not None:
                        updated = apply_success_latency_policy(
                            updated,
                            latency_ms,
                            testing_config,
                            "xray_direct",
                            slow_threshold_ms=slow_threshold_ms,
                        )
                        updated.last_checked_at = utc_now_iso()
                    else:
                        updated.status = status
                        updated.suspicious_reason = reason
            if _validation_cancel_requested(cancel_event):
                return _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
            return updated
        except Exception as exc:
            if _is_executor_shutdown_error(exc):
                logger.debug(
                    "Skipping node validation for %s:%s because the validation executor is shutting down",
                    node.address,
                    node.port,
                )
                updated = node.clone()
                updated.status = NodeStatus.SUSPICIOUS
                updated.latency_ms = None
                updated.suspicious_reason = (
                    "validation_cancelled: interpreter_shutdown"
                    if sys.is_finalizing()
                    else "validation_cancelled: executor_shutdown"
                )
                updated.last_checked_at = utc_now_iso()
                return updated
            log_raw_diagnostic(
                logger,
                "warning",
                "Node validation crashed for %s:%s: %s",
                node.address,
                node.port,
                exc,
                exc_info=True,
            )
            updated = node.clone()
            updated.status = NodeStatus.SUSPICIOUS
            updated.latency_ms = None
            updated.suspicious_reason = f"validation_exception: {exc}"
            updated.last_checked_at = utc_now_iso()
            return updated


def _should_use_tier3_mihomo_batch_validation(
    nodes: list[Node],
    *,
    full_validation_enabled: bool,
    gateway_nodes: list[Node] | None,
    testing_config: dict[str, Any],
) -> bool:
    if not nodes or not full_validation_enabled:
        return False
    if not all(node.tier == "tier3" for node in nodes):
        return False
    helper_config = testing_config.get("mihomo_helper", {})
    helper_config = helper_config if isinstance(helper_config, dict) else {}
    if coerce_bool(testing_config.get("tier3_responsive_candidates"), False):
        return bool(
            coerce_bool(helper_config.get("enabled", True), True)
            and coerce_bool(helper_config.get("batch_enabled", True), True)
        )
    if gateway_nodes:
        return False
    return bool(
        coerce_bool(helper_config.get("enabled", True), True)
        and coerce_bool(helper_config.get("batch_enabled", True), True)
    )


async def _test_tier3_nodes_with_mihomo_batch(
    nodes: list[Node],
    testing_config: dict[str, Any],
    logger: logging.Logger,
    *,
    concurrency: int,
    result_observer: Callable[[Node], None] | None = None,
) -> list[Node]:
    timeout_seconds = node_test_tcp_timeout_seconds(testing_config)
    slow_threshold_ms = slow_threshold_ms_from_testing(testing_config)
    cancel_event = testing_config.get("validation_cancel_event")
    result_observer = _cancellation_filtered_result_observer(
        result_observer,
        cancel_event,
    )
    tcp_wave_deadline_seconds = _config_float(
        testing_config.get("tcp_wave_deadline_seconds"),
        0.0,
        minimum=0.0,
    )
    tcp_wave_cleanup_slack_seconds = min(
        0.5,
        _config_float(
            testing_config.get("tcp_wave_cleanup_slack_seconds"),
            0.25,
            minimum=0.0,
        ),
    )
    if coerce_bool(testing_config.get("tier3_responsive_candidates"), False):
        session = testing_config.get("mihomo_validation_session")
        if session is not None and hasattr(session, "validate"):
            return await asyncio.to_thread(
                session.validate,
                nodes,
                result_observer=result_observer,
                max_workers=max(1, int(concurrency)),
                cancel_event=cancel_event,
            )
        return await asyncio.to_thread(
            _mihomo_batch_validate,
            nodes,
            testing_config,
            slow_threshold_ms,
            logger,
            result_observer=result_observer,
            max_workers=max(1, int(concurrency)),
            cancel_event=cancel_event,
        )
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def probe_tcp(node: Node) -> Node:
        async with semaphore:
            if _validation_cancel_requested(cancel_event):
                return _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
            return await _tcp_probe(
                node,
                timeout_seconds=timeout_seconds,
                slow_threshold_ms=slow_threshold_ms,
                testing_config=testing_config,
            )

    task_map = {asyncio.create_task(probe_tcp(node)): node for node in nodes}
    if not task_map:
        return []

    tested: list[Node] = []
    responsive: list[Node] = []
    tcp_outcome = await _collect_tcp_probe_tasks(
        task_map,
        deadline_seconds=tcp_wave_deadline_seconds,
        cleanup_slack_seconds=tcp_wave_cleanup_slack_seconds,
        logger=logger,
        cancel_event=cancel_event,
    )
    for updated in tcp_outcome.nodes:
        if updated.status in {NodeStatus.ALIVE, NodeStatus.SLOW}:
            responsive.append(updated)
            continue
        tested.append(updated)
        if not _validation_cancel_requested(cancel_event):
            _notify_result_observer(result_observer, updated, logger)

    if responsive:
        batch_tested = await asyncio.to_thread(
            _mihomo_batch_validate,
            responsive,
            testing_config,
            slow_threshold_ms,
            logger,
            result_observer=result_observer,
            max_workers=max(1, int(concurrency)),
            cancel_event=cancel_event,
        )
        tested.extend(batch_tested)
    return sort_nodes_for_use(tested)


async def scan_tier3_tcp_candidates(
    nodes: list[Node],
    testing_config: dict[str, Any],
    logger: logging.Logger,
    *,
    concurrency: int,
    result_observer: Callable[[Node], None] | None = None,
    cancel_event: Any | None = None,
    transport_start_observer: Callable[[Node, float], bool] | None = None,
    transport_entitlement_provider: (
        Callable[[Node, float], tuple[int, float]] | None
    ) = None,
    transport_began_observer: Callable[[Node, float], None] | None = None,
) -> Tier3TcpScanOutcome:
    if not nodes:
        return Tier3TcpScanOutcome(nodes=[], metadata={
            "scan_started_at": time.monotonic(),
            "scan_finished_at": time.monotonic(),
            "tcp_scan_elapsed_ms": 0,
            "deadline_seconds": 0.0,
            "cleanup_slack_ms": 250,
            "scheduled_count": 0,
            "completed_before_deadline": 0,
            "deferred_by_deadline": 0,
            "late_completed_ignored": 0,
            "deadline_hit": False,
            "deadline_violation": False,
        })
    if cancel_event is None:
        cancel_event = testing_config.get("validation_cancel_event")
    result_observer = _cancellation_filtered_result_observer(
        result_observer,
        cancel_event,
    )
    if _validation_cancel_requested(cancel_event):
        now = time.monotonic()
        return Tier3TcpScanOutcome(
            nodes=[
                _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
                for node in nodes
            ],
            metadata={
                "scan_started_at": now,
                "scan_finished_at": now,
                "tcp_scan_elapsed_ms": 0,
                "deadline_seconds": 0.0,
                "cleanup_slack_ms": 0,
                "scheduled_count": 0,
                "completed_before_deadline": 0,
                "deferred_by_deadline": len(nodes),
                "late_completed_ignored": 0,
                "deadline_hit": False,
                "deadline_violation": False,
                "cancelled": True,
            },
        )
    timeout_seconds = node_test_tcp_timeout_seconds(testing_config)
    slow_threshold_ms = slow_threshold_ms_from_testing(testing_config)
    tcp_wave_deadline_seconds = _config_float(
        testing_config.get("tcp_wave_deadline_seconds"),
        0.0,
        minimum=0.0,
    )
    tcp_wave_cleanup_slack_seconds = min(
        0.5,
        _config_float(
            testing_config.get("tcp_wave_cleanup_slack_seconds"),
            0.25,
            minimum=0.0,
        ),
    )
    tcp_probe_entitlement_seconds = _config_float(
        testing_config.get("tcp_probe_entitlement_seconds"),
        0.0,
        minimum=0.0,
    )
    if tcp_probe_entitlement_seconds <= 0.0:
        tcp_probe_entitlement_seconds = (
            min(timeout_seconds, tcp_wave_deadline_seconds)
            if tcp_wave_deadline_seconds > 0.0
            else timeout_seconds
        )
    tcp_dispatch_start_guard_seconds = _config_float(
        testing_config.get("tcp_dispatch_start_guard_seconds"),
        0.25,
        minimum=0.001,
    )
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    task_map: dict[asyncio.Task[Node], Tier3TcpProbeEntitlement] = {}
    for node in nodes:
        entitlement = Tier3TcpProbeEntitlement(node=node)
        task = asyncio.create_task(
            _run_tcp_probe_entitlement(
                entitlement,
                semaphore=semaphore,
                entitlement_seconds=tcp_probe_entitlement_seconds,
                cleanup_slack_seconds=tcp_wave_cleanup_slack_seconds,
                cancel_event=cancel_event,
                transport_start_observer=transport_start_observer,
                transport_entitlement_provider=transport_entitlement_provider,
                transport_began_observer=transport_began_observer,
                probe_factory=lambda effective_timeout, node=node: _tcp_probe(
                    node,
                    timeout_seconds=effective_timeout,
                    slow_threshold_ms=slow_threshold_ms,
                    testing_config=testing_config,
                ),
            )
        )
        task_map[task] = entitlement
    if not task_map:
        return []

    collection_outcome = await _collect_tcp_probe_entitlements(
        task_map,
        entitlement_seconds=tcp_probe_entitlement_seconds,
        dispatch_start_guard_seconds=tcp_dispatch_start_guard_seconds,
        cleanup_slack_seconds=tcp_wave_cleanup_slack_seconds,
        logger=logger,
        cancel_event=cancel_event,
        result_observer=result_observer,
    )
    return Tier3TcpScanOutcome(
        nodes=sort_nodes_for_use(collection_outcome.nodes),
        metadata=dict(collection_outcome.metadata),
    )


async def test_nodes(
    nodes: list[Node],
    testing_config: dict[str, Any],
    logger: logging.Logger,
    full_validation_enabled: bool = False,
    gateway_nodes: list[Node] | None = None,
    result_observer: Callable[[Node], None] | None = None,
    cancel_event: Any | None = None,
) -> list[Node]:
    if cancel_event is None:
        cancel_event = testing_config.get("validation_cancel_event")
    elif testing_config.get("validation_cancel_event") is not cancel_event:
        testing_config = dict(testing_config)
        testing_config["validation_cancel_event"] = cancel_event
    result_observer = _cancellation_filtered_result_observer(
        result_observer,
        cancel_event,
    )
    if _validation_cancel_requested(cancel_event):
        return [
            _tcp_wave_deferred_result(node, "tcp_wave_cancelled")
            for node in nodes
        ]
    full_validation = coerce_bool(full_validation_enabled, False)
    concurrency = node_test_concurrency_from_testing(
        testing_config,
        full_validation_enabled=full_validation,
        has_tier3_nodes=any(node.tier == "tier3" for node in nodes),
        has_gateway_nodes=bool(gateway_nodes),
    )
    timeout_seconds = node_test_tcp_timeout_seconds(testing_config)
    slow_threshold_ms = slow_threshold_ms_from_testing(testing_config)
    tcp_wave_deadline_seconds = _config_float(
        testing_config.get("tcp_wave_deadline_seconds"),
        0.0,
        minimum=0.0,
    )
    tcp_wave_cleanup_slack_seconds = min(
        0.5,
        _config_float(
            testing_config.get("tcp_wave_cleanup_slack_seconds"),
            0.25,
            minimum=0.0,
        ),
    )
    helper_config = helper_block_from_testing(testing_config, "xray_helper")
    validation_allowed = full_validation
    if _should_use_tier3_mihomo_batch_validation(
        nodes,
        full_validation_enabled=validation_allowed,
        gateway_nodes=gateway_nodes,
        testing_config=testing_config,
    ):
        return await _test_tier3_nodes_with_mihomo_batch(
            nodes,
            testing_config,
            logger,
            concurrency=concurrency,
            result_observer=result_observer,
        )
    semaphore = asyncio.Semaphore(concurrency)
    task_map = {
        asyncio.create_task(
            _probe_with_optional_validation(
                node=node,
                semaphore=semaphore,
                timeout_seconds=timeout_seconds,
                slow_threshold_ms=slow_threshold_ms,
                full_validation_enabled=validation_allowed,
                helper_config=helper_config,
                gateway_nodes=gateway_nodes,
                testing_config=testing_config,
                logger=logger,
                cancel_event=cancel_event,
            )
        ): node
        for node in nodes
    }
    if not task_map:
        return []

    tested: list[Node] = []
    tcp_outcome = await _collect_tcp_probe_tasks(
        task_map,
        deadline_seconds=(
            tcp_wave_deadline_seconds
            if not validation_allowed and not gateway_nodes
            else 0.0
        ),
        cleanup_slack_seconds=tcp_wave_cleanup_slack_seconds,
        logger=logger,
        cancel_event=cancel_event,
    )
    for updated in tcp_outcome.nodes:
        tested.append(updated)
        if not _validation_cancel_requested(cancel_event):
            _notify_result_observer(result_observer, updated, logger)
    return sort_nodes_for_use(tested)
