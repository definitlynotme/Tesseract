from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import yaml

from models import Node
from tier3_validation_config import build_single_proxy_validation_config, build_validator_prewarm_config
from tier3_validation_protocol import (
    CONFIRM_HTTPS_ATTEMPTS_PER_URL,
    CONFIRM_HTTPS_BUDGET_SECONDS,
    CONFIRM_HTTPS_CENSORED_REASON,
    CONFIRM_HTTPS_FAILED_REASON,
    CONFIRM_HTTPS_STAGE,
    CancelCmd,
    EndpointAttemptKind,
    EndpointAttemptResult,
    ProbeAttemptFact,
    TIER3_HELPER_PREWARM_READY_TIMEOUT_SECONDS,
    ValidateEndpointCmd,
    ValidationStage,
    cancel_cmd_from_payload,
    endpoint_result_wire_payload,
    message_bytes,
    parse_message,
    validate_endpoint_cmd_from_payload,
)

# The supervisor's parent guard adds this delivery slack to the lease hard
# cap; the confirm never plans to run into it.
_CONFIRM_DELIVERY_SLACK_SECONDS = 0.75
# A confirm URL is a real site: only an empty 204 counts (Google's
# generate_204), never a 200 page that a captive portal could also serve.
_CONFIRM_EXPECTED_STATUSES = frozenset({204})
# A repeat sample needs room for a CONNECT plus a TLS handshake; with less of
# the hard cap left, one transport failure stays inconclusive (censored).
_CONFIRM_RETRY_MIN_BUDGET_SECONDS = 0.5


class _NoDnsResolver(aiohttp.abc.AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        raise RuntimeError(f"local DNS is forbidden in validator helper: {host}:{port}")

    async def close(self) -> None:
        return None


def _reserve_port(*, excluded: frozenset[int] = frozenset()) -> int:
    for _attempt in range(128):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        finally:
            sock.close()
        if port > 0 and port not in excluded:
            return port
    raise RuntimeError("unable to reserve distinct validator ports")


def _expected_statuses(raw: Any) -> set[int]:
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    statuses: set[int] = set()
    for value in values:
        try:
            statuses.add(int(value))
        except (TypeError, ValueError):
            continue
    return statuses or {200, 204}


def _body_is_valid(status: int, body: bytes) -> bool:
    if int(status) == 204:
        return body in {b"", b"\n"}
    return True


def _is_transient_http_status(status: int) -> bool:
    return 500 <= int(status) <= 599


def _exception_detail(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, asyncio.TimeoutError):
        return "transient", "timeout"
    if isinstance(exc, aiohttp.ClientProxyConnectionError):
        # The configured HTTP proxy is the helper's own loopback mixed-port.
        # Refusal here is validator facility loss, not candidate evidence.
        return "facility", f"mixed_port_connect:{exc.os_error or exc}"
    if isinstance(exc, aiohttp.ClientConnectorError):
        # A reset/EOF inside a CONNECT tunnel often has an empty message;
        # keep its class so a confirm_https_failed reason stays legible.
        cause = exc.os_error or exc
        return "transient", f"connect:{str(cause) or type(cause).__name__}"
    if isinstance(exc, aiohttp.ClientSSLError):
        return "transient", f"tls:{exc}"
    if isinstance(exc, aiohttp.ClientPayloadError):
        return "transient", f"payload:{exc}"
    if isinstance(exc, aiohttp.ClientError):
        return "transient", f"http_transport:{exc}"
    if isinstance(exc, OSError):
        return "transient", f"socket:{exc}"
    return "facility", f"{type(exc).__name__}:{exc}"


class ValidatorHelperRuntime:
    def __init__(self, connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None) -> None:
        self.connection = connection
        self.worker_id = int(start_payload["worker_id"])
        self.generation = int(start_payload["worker_generation"])
        self.binary_path = str(start_payload["binary_path"])
        self.probe_urls = tuple(str(item) for item in start_payload.get("probe_urls", ()) if str(item))
        # Mainstream HTTPS confirm URLs; absent or empty disables the gate.
        self.confirm_urls = tuple(
            str(item) for item in start_payload.get("confirm_urls", ()) or () if str(item)
        )
        self.expected_statuses = _expected_statuses(start_payload.get("expected_statuses"))
        self.probe_timeout_seconds = max(0.25, float(start_payload.get("probe_timeout_seconds", 5.0)))
        self.slow_threshold_ms = max(1, int(start_payload.get("slow_threshold_ms", 3000)))
        self.ipv6 = bool(start_payload.get("ipv6", False))
        prewarm_config = start_payload.get("prewarm_config")
        dns_block = prewarm_config.get("dns") if isinstance(prewarm_config, dict) else None
        self._dns_block = copy.deepcopy(dns_block) if isinstance(dns_block, dict) else None
        self.temp_parent = str(start_payload.get("temp_parent") or "")
        if self.temp_parent:
            Path(self.temp_parent).mkdir(parents=True, exist_ok=True)
        supplied_mixed_port = int(start_payload.get("mixed_port", 0) or 0)
        supplied_controller_port = int(start_payload.get("controller_port", 0) or 0)
        if supplied_mixed_port > 0 or supplied_controller_port > 0:
            if (
                supplied_mixed_port <= 0
                or supplied_controller_port <= 0
                or supplied_mixed_port == supplied_controller_port
            ):
                raise ValueError("validator START ports must be positive and distinct")
            self.mixed_port = supplied_mixed_port
            self.controller_port = supplied_controller_port
        else:
            # Offline oracle/unit-test compatibility. Production helpers always
            # receive generation-owned ports from the supervisor.
            self.mixed_port = _reserve_port()
            self.controller_port = _reserve_port(
                excluded=frozenset({self.mixed_port})
            )
        self.work_dir = prewarmed.work_dir if prewarmed is not None else Path(
            tempfile.mkdtemp(
                prefix=f"tier3-validator-{self.worker_id}-{self.generation}-",
                dir=self.temp_parent or None,
            )
        )
        self.process = prewarmed.process if prewarmed is not None else None
        self.controller_session: aiohttp.ClientSession | None = None
        self.probe_connector: aiohttp.TCPConnector | None = None
        self.probe_session: aiohttp.ClientSession | None = None
        self.active_task: asyncio.Task[EndpointAttemptResult] | None = None
        self.active_cmd: ValidateEndpointCmd | None = None
        self._detached_probe_tasks: set[asyncio.Task[Any]] = set()
        self._detached_cleanup_tasks: set[asyncio.Future[Any]] = set()
        self.loaded_config_digest = ""
        self.stopping = False
        self.last_heartbeat = 0.0

    def _send(self, kind: str, payload: dict[str, Any]) -> None:
        self.connection.send_bytes(message_bytes(kind, payload))

    async def _mixed_port_is_ready(self) -> bool:
        writer: asyncio.StreamWriter | None = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.mixed_port),
                timeout=0.25,
            )
            return True
        except (asyncio.TimeoutError, OSError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    def _base_config(self) -> dict[str, Any]:
        config = build_validator_prewarm_config(
            self.mixed_port, self.controller_port, ipv6=self.ipv6,
        )
        if self._dns_block is not None:
            config["dns"] = copy.deepcopy(self._dns_block)
        return config

    def _candidate_config(self, node: Node) -> dict[str, Any] | None:
        config = build_single_proxy_validation_config(
            node,
            self.mixed_port,
            controller_port=self.controller_port,
            ipv6=self.ipv6,
        )
        if config is not None and self._dns_block is not None:
            config["dns"] = copy.deepcopy(self._dns_block)
        return config

    def _write_config(self, config: dict[str, Any], suffix: str) -> Path:
        path = self.work_dir / f"validator-{suffix}.yaml"
        path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    async def start(self) -> None:
        if not self.binary_path or not Path(self.binary_path).exists():
            raise FileNotFoundError(f"Mihomo binary not found: {self.binary_path}")
        self._send(
            "startup_progress",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "phase": "mihomo_starting",
                "helper_pid": os.getpid(),
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        if self.process is None:
            initial_path = self._write_config(self._base_config(), "prewarm")
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            self.process = await asyncio.create_subprocess_exec(
                self.binary_path,
                "-f",
                str(initial_path),
                "-d",
                str(self.work_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
        self._send(
            "startup_progress",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "phase": "mihomo_started",
                "helper_pid": os.getpid(),
                "mihomo_pid": int(self.process.pid),
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        timeout = aiohttp.ClientTimeout(total=1.0, connect=0.5, sock_connect=0.5, sock_read=0.5)
        self.controller_session = aiohttp.ClientSession(
            base_url=f"http://127.0.0.1:{self.controller_port}",
            timeout=timeout,
            trust_env=False,
            connector=aiohttp.TCPConnector(
                resolver=_NoDnsResolver(),
                force_close=True,
                use_dns_cache=False,
                limit=2,
            ),
            auto_decompress=False,
            max_line_size=8192,
            max_field_size=8192,
        )
        deadline = (
            time.monotonic() + TIER3_HELPER_PREWARM_READY_TIMEOUT_SECONDS
        )
        last_error = "controller not ready"
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError(f"Mihomo exited during helper prewarm: {self.process.returncode}")
            try:
                async with self.controller_session.get("/version") as response:
                    if response.status == 200:
                        if not await self._mixed_port_is_ready():
                            last_error = "mixed-port not ready"
                            await asyncio.sleep(0.04)
                            continue
                        self._send(
                            "ready",
                            {
                                "worker_id": self.worker_id,
                                "worker_generation": self.generation,
                                "helper_pid": os.getpid(),
                                "mihomo_pid": int(self.process.pid),
                                "mixed_port": self.mixed_port,
                                "controller_port": self.controller_port,
                            },
                        )
                        return
                    last_error = f"controller status {response.status}"
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = str(exc)
            await asyncio.sleep(0.04)
        raise TimeoutError(last_error)

    async def _reload_candidate(self, node: Node, cmd: ValidateEndpointCmd) -> tuple[bool, str, int]:
        started = time.monotonic()
        if self.loaded_config_digest == cmd.config_digest and self.controller_session is not None:
            try:
                async with asyncio.timeout(min(0.25, max(0.1, cmd.facility_budget_seconds))):
                    async with self.controller_session.get("/proxies") as response:
                        if response.status == 200:
                            payload = await response.json(content_type=None)
                            proxies = payload.get("proxies") if isinstance(payload, dict) else None
                            if isinstance(proxies, dict) and "TEST-PROXY" in proxies:
                                return True, "config_reused", int(
                                    (time.monotonic() - started) * 1000
                                )
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError):
                # A failed reuse check falls through to the ordinary reload.
                pass
        config = self._candidate_config(node)
        if config is None:
            return False, "unsupported_proxy", 0
        path = self._write_config(config, f"{cmd.sequence}-{cmd.candidate_digest[:12]}")
        if self.controller_session is None:
            return False, "controller_unavailable", 0
        try:
            async with asyncio.timeout(max(0.1, cmd.facility_budget_seconds)):
                async with self.controller_session.put(
                    "/configs",
                    params={"force": "true"},
                    json={"path": str(path), "payload": ""},
                ) as response:
                    body = await response.content.read(4097)
                    if response.status >= 300:
                        return False, f"reload_status_{response.status}:{body[:256]!r}", int(
                            (time.monotonic() - started) * 1000
                        )
                while True:
                    async with self.controller_session.get("/proxies") as response:
                        if response.status == 200:
                            payload = await response.json(content_type=None)
                            proxies = payload.get("proxies") if isinstance(payload, dict) else None
                            if isinstance(proxies, dict) and "TEST-PROXY" in proxies:
                                self.loaded_config_digest = cmd.config_digest
                                return True, "", int((time.monotonic() - started) * 1000)
                    await asyncio.sleep(0.025)
        except asyncio.TimeoutError:
            return False, "facility_deadline", int((time.monotonic() - started) * 1000)
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            return False, f"facility_error:{type(exc).__name__}:{exc}", int(
                (time.monotonic() - started) * 1000
            )

    async def _probe_once(
        self,
        endpoint: str,
        budget_seconds: float,
        cmd: ValidateEndpointCmd,
        attempt_index: int,
        *,
        expected_statuses: frozenset[int] | None = None,
        stage: str = "",
    ) -> tuple[ProbeAttemptFact, bool, float | None]:
        started = time.monotonic()
        started_ns = time.monotonic_ns()
        status_code: int | None = None
        accepted_statuses = (
            expected_statuses
            if expected_statuses is not None
            else self.expected_statuses
        )
        fact_identity = {
            "attempt_id": f"{cmd.lease_id}:{attempt_index}",
            "generation_id": f"{self.worker_id}:{self.generation}",
            "candidate_fingerprint": cmd.candidate_digest,
            "session_lease_id": cmd.lease_id,
            "stage": stage or cmd.stage.value,
            "transport_started_monotonic_ns": started_ns,
            "granted_budget_ms": int(round(budget_seconds * 1000)),
            "lifecycle": "completed",
        }
        try:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("invalid probe URL")
            timeout = aiohttp.ClientTimeout(
                total=budget_seconds,
                connect=budget_seconds,
                sock_connect=budget_seconds,
                sock_read=budget_seconds,
            )
            session = self._ensure_probe_session()
            async with session.get(
                endpoint,
                proxy=f"http://127.0.0.1:{self.mixed_port}",
                allow_redirects=False,
                headers={"Accept": "*/*", "Connection": "close"},
                timeout=timeout,
            ) as response:
                status_code = int(response.status)
                body = await response.content.read(129)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                passed = status_code in accepted_statuses and _body_is_valid(
                    status_code,
                    body,
                )
                if passed:
                    return (
                        ProbeAttemptFact(
                            endpoint=endpoint,
                            elapsed_ms=elapsed_ms,
                            full_budget=True,
                            completed=True,
                            status_code=status_code,
                            outcome="pass",
                            failure_domain="none",
                            **fact_identity,
                        ),
                        True,
                        float(elapsed_ms),
                    )
                transient = _is_transient_http_status(status_code)
                return (
                    ProbeAttemptFact(
                        endpoint=endpoint,
                        elapsed_ms=elapsed_ms,
                        full_budget=True,
                        completed=True,
                        status_code=status_code,
                        outcome="transient" if transient else "candidate_fail",
                        failure_domain="candidate_or_endpoint" if transient else "candidate",
                        detail=f"unexpected_status:{status_code}",
                        **fact_identity,
                    ),
                    False,
                    None,
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                censored_identity = dict(fact_identity)
                censored_identity["lifecycle"] = "censored"
                return (
                    ProbeAttemptFact(
                        endpoint=endpoint,
                        elapsed_ms=elapsed_ms,
                        full_budget=False,
                        completed=False,
                        entitlement_exhausted=(
                            elapsed_ms + 25 >= int(round(budget_seconds * 1000))
                        ),
                        status_code=status_code,
                        outcome="right_censored",
                        failure_domain="endpoint_deadline",
                        detail="endpoint_entitlement_expired",
                        **censored_identity,
                    ),
                    False,
                    None,
                )
            domain, detail = _exception_detail(exc)
            full_budget = (
                budget_seconds + 0.025 >= self.probe_timeout_seconds
                and elapsed_ms + 25 >= int(budget_seconds * 1000)
            )
            return (
                ProbeAttemptFact(
                    endpoint=endpoint,
                    elapsed_ms=elapsed_ms,
                    full_budget=full_budget,
                    completed=True,
                    entitlement_exhausted=(
                        elapsed_ms + 25 >= int(round(budget_seconds * 1000))
                    ),
                    status_code=status_code,
                    outcome="transient" if domain != "facility" else "facility_fail",
                    failure_domain=domain,
                    detail=detail[:512],
                    **fact_identity,
                ),
                False,
                None,
            )

    def _ensure_probe_session(self) -> aiohttp.ClientSession:
        session = self.probe_session
        if session is not None and not session.closed:
            return session
        connector = aiohttp.TCPConnector(
            resolver=_NoDnsResolver(),
            force_close=True,
            use_dns_cache=False,
            limit=1,
            enable_cleanup_closed=True,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            connector_owner=True,
            trust_env=False,
            auto_decompress=False,
            max_line_size=8192,
            max_field_size=8192,
        )
        self.probe_connector = connector
        self.probe_session = session
        return session

    @staticmethod
    def _consume_detached_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    def _detach_probe_task(self, task: asyncio.Task[Any]) -> None:
        task.cancel()
        self._detached_probe_tasks.add(task)

        def discard(completed: asyncio.Task[Any]) -> None:
            self._detached_probe_tasks.discard(completed)
            self._consume_detached_task(completed)

        task.add_done_callback(discard)

    def _detach_cleanup_task(self, task: asyncio.Future[Any]) -> None:
        self._detached_cleanup_tasks.add(task)

        def discard(completed: asyncio.Future[Any]) -> None:
            self._detached_cleanup_tasks.discard(completed)
            try:
                completed.result()
            except BaseException:
                pass

        task.add_done_callback(discard)

    async def _invalidate_probe_session(self) -> bool:
        """Abort the current probe transport before the helper is reused."""

        session = self.probe_session
        connector = self.probe_connector
        self.probe_session = None
        self.probe_connector = None
        if session is None and connector is None:
            return False

        close_awaitable: Any = None
        if connector is not None:
            detach = getattr(session, "detach", None)
            if callable(detach):
                detach()
            close_awaitable = connector.close(abort_ssl=True)
        elif session is not None:
            close_awaitable = session.close()
        if close_awaitable is None:
            return True

        cleanup = asyncio.ensure_future(close_awaitable)
        done, _pending = await asyncio.wait((cleanup,), timeout=0.05)
        if cleanup in done:
            self._consume_detached_task(cleanup)  # type: ignore[arg-type]
        else:
            self._detach_cleanup_task(cleanup)
        # aiohttp closes/aborts and removes acquired transports synchronously
        # when close() is called; its awaitable only drains protocol cleanup.
        # A new validation therefore cannot reuse the old socket or connector.
        return True

    async def _cancel_probe_task(
        self,
        task: asyncio.Task[Any],
        *,
        timeout: float = 0.1,
    ) -> bool:
        task.cancel()
        transport_aborted = await self._invalidate_probe_session()
        done, _pending = await asyncio.wait((task,), timeout=max(0.0, timeout))
        if task in done:
            self._consume_detached_task(task)
            return True
        self._detach_probe_task(task)
        return transport_aborted

    async def _settle_detached_probe_tasks(self, *, timeout: float) -> bool:
        detached = tuple(self._detached_probe_tasks)
        if not detached:
            return True
        for task in detached:
            task.cancel()
        done, pending = await asyncio.wait(detached, timeout=max(0.0, timeout))
        for task in done:
            self._consume_detached_task(task)
        for task in pending:
            task.cancel()
        return not pending

    async def _confirm_mainstream_https(
        self,
        cmd: ValidateEndpointCmd,
        *,
        passed_fact: ProbeAttemptFact,
        total_started: float,
    ) -> tuple[
        tuple[ProbeAttemptFact, ...],
        EndpointAttemptKind | None,
        str,
        bool,
    ]:
        """Load each confirm URL through the node that just PASSED.

        Runs only after the scheduled portfolio endpoint passed, so the
        overwhelming majority of candidates (which fail that endpoint) pay
        nothing.  Returns ``(facts, failing kind or None, reason, clean)``:

        * an explicit answer other than an empty 204 (4xx, a 200 page, a 204
          with a body) is a verdict about the node -> CANDIDATE_FAIL;
        * a transport-class failure (TLS EOF/reset, CONNECT refused, a 5xx) is
          sampled once more, and only a repeat failure is the verdict; one
          failure with no hard-cap room left for the repeat stays censored;
        * a timeout proves nothing -> RIGHT_CENSORED;
        * loss of the helper's own mixed port -> FACILITY_FAIL.

        The default aiohttp ``ssl=True`` verifies the certificate chain, so a
        node that intercepts TLS fails here too.
        """

        facts: list[ProbeAttemptFact] = []
        clean = True
        endpoint = str(cmd.endpoint or "")
        attempt_index = 1
        for url in getattr(self, "confirm_urls", ()) or ():
            if (
                url == endpoint
                and passed_fact.status_code == 204
                and urlsplit(url).scheme == "https"
            ):
                # The portfolio slice already loaded this exact URL with an
                # empty 204 over verified TLS; do not pay for it twice.
                continue
            for sample in range(CONFIRM_HTTPS_ATTEMPTS_PER_URL):
                remaining = (
                    float(cmd.candidate_hard_cap_seconds)
                    - _CONFIRM_DELIVERY_SLACK_SECONDS
                    - (time.monotonic() - total_started)
                )
                if sample and remaining < _CONFIRM_RETRY_MIN_BUDGET_SECONDS:
                    # One transport failure is not a verdict, and the lease
                    # has no room to repeat it: defer, never negative-cache.
                    return (
                        tuple(facts),
                        EndpointAttemptKind.RIGHT_CENSORED,
                        CONFIRM_HTTPS_CENSORED_REASON,
                        clean,
                    )
                budget = max(
                    0.001,
                    min(
                        CONFIRM_HTTPS_BUDGET_SECONDS,
                        self.probe_timeout_seconds,
                        remaining,
                    ),
                )
                attempt_index += 1
                fact, attempt_clean = await self._confirm_attempt(
                    cmd, url, budget, attempt_index,
                )
                clean = clean and attempt_clean
                if fact.outcome == "pass":
                    facts.append(fact)
                    break
                if fact.outcome == "facility_fail":
                    facts.append(fact)
                    return (
                        tuple(facts),
                        EndpointAttemptKind.FACILITY_FAIL,
                        fact.detail or "confirm_https_facility_failed",
                        clean,
                    )
                if fact.outcome == "right_censored" or not fact.completed:
                    facts.append(fact)
                    return (
                        tuple(facts),
                        EndpointAttemptKind.RIGHT_CENSORED,
                        CONFIRM_HTTPS_CENSORED_REASON,
                        clean,
                    )
                if (
                    fact.outcome == "transient"
                    and sample + 1 < CONFIRM_HTTPS_ATTEMPTS_PER_URL
                ):
                    # Reset/EOF or a 5xx: keep the fact as transient evidence
                    # and sample the same URL once more.
                    facts.append(fact)
                    continue
                # An explicit non-204 answer, or a transport failure that
                # repeated: the node relays connectivity checks yet cannot
                # load a real site.
                detail = str(fact.detail or f"outcome:{fact.outcome}")
                facts.append(
                    replace(fact, outcome="candidate_fail", failure_domain="candidate")
                )
                return (
                    tuple(facts),
                    EndpointAttemptKind.CANDIDATE_FAIL,
                    f"{CONFIRM_HTTPS_FAILED_REASON}:{detail}"[:512],
                    clean,
                )
        return tuple(facts), None, "", clean

    async def _confirm_attempt(
        self,
        cmd: ValidateEndpointCmd,
        url: str,
        budget: float,
        attempt_index: int,
    ) -> tuple[ProbeAttemptFact, bool]:
        """One confirm request bounded by ``budget``; returns (fact, clean)."""

        attempt_started = time.monotonic()
        attempt_started_ns = time.monotonic_ns()
        probe_task = asyncio.create_task(
            self._probe_once(
                url,
                budget,
                cmd,
                attempt_index,
                expected_statuses=_CONFIRM_EXPECTED_STATUSES,
                stage=CONFIRM_HTTPS_STAGE,
            )
        )
        try:
            completed, _pending = await asyncio.wait(
                (probe_task,),
                timeout=max(0.001, budget + 0.1),
            )
        except asyncio.CancelledError:
            await self._cancel_probe_task(probe_task)
            raise
        if probe_task in completed:
            fact, _passed, _latency = probe_task.result()
            return fact, True
        clean = await self._cancel_probe_task(probe_task)
        elapsed_ms = int((time.monotonic() - attempt_started) * 1000)
        return (
            ProbeAttemptFact(
                endpoint=url,
                elapsed_ms=elapsed_ms,
                full_budget=False,
                completed=False,
                entitlement_exhausted=(
                    elapsed_ms + 25 >= int(round(budget * 1000))
                ),
                outcome="right_censored",
                failure_domain="endpoint_deadline",
                detail="endpoint_entitlement_expired",
                attempt_id=f"{cmd.lease_id}:{attempt_index}",
                generation_id=f"{self.worker_id}:{self.generation}",
                candidate_fingerprint=cmd.candidate_digest,
                session_lease_id=cmd.lease_id,
                stage=CONFIRM_HTTPS_STAGE,
                transport_started_monotonic_ns=attempt_started_ns,
                granted_budget_ms=int(round(budget * 1000)),
                lifecycle="censored",
            ),
            clean,
        )

    async def validate_endpoint(
        self,
        cmd: ValidateEndpointCmd,
    ) -> EndpointAttemptResult:
        total_started = time.monotonic()
        helper_started_monotonic_ns = time.monotonic_ns()
        self._send(
            "slice_phase",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "lease_id": cmd.lease_id,
                "slice_id": cmd.slice_id,
                "stage": cmd.stage.value,
                "phase": "facility_started",
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        node = Node.from_dict(json.loads(cmd.node_json))
        facility_ok, facility_reason, facility_elapsed_ms = await self._reload_candidate(
            node,
            cmd,
        )
        if not facility_ok:
            kind = (
                EndpointAttemptKind.CANDIDATE_FAIL
                if facility_reason == "unsupported_proxy"
                else EndpointAttemptKind.FACILITY_FAIL
            )
            return EndpointAttemptResult(
                refresh_epoch=cmd.refresh_epoch,
                candidate_digest=cmd.candidate_digest,
                config_digest=cmd.config_digest,
                lease_id=cmd.lease_id,
                sequence=cmd.sequence,
                worker_id=self.worker_id,
                worker_generation=self.generation,
                node_json=cmd.node_json,
                kind=kind,
                transaction_id=cmd.transaction_id,
                slice_id=cmd.slice_id,
                stage=cmd.stage,
                purpose=cmd.purpose,
                report_json=cmd.report_json,
                reason=facility_reason,
                # The actor owns the candidate-wide endpoint ledger.  A
                # facility failure before transport start has no new attempt
                # fact; echoing the command's historical facts would make the
                # endpoint controller observe them a second time.
                attempt_facts=(),
                facility_elapsed_ms=facility_elapsed_ms,
                total_elapsed_ms=int((time.monotonic() - total_started) * 1000),
                clean_confirmed=True,
                helper_pid=os.getpid(),
                mihomo_pid=int(self.process.pid if self.process else 0),
                probe_policy_revision=cmd.probe_policy_revision,
                portfolio_digest=cmd.portfolio_digest,
                submitted_monotonic_ns=cmd.submitted_monotonic_ns,
                dispatch_monotonic_ns=cmd.dispatch_monotonic_ns,
                supervisor_assigned_monotonic_ns=(
                    cmd.supervisor_assigned_monotonic_ns
                ),
                supervisor_sent_monotonic_ns=cmd.supervisor_sent_monotonic_ns,
                helper_started_monotonic_ns=helper_started_monotonic_ns,
            )

        probe_started = time.monotonic()
        probe_started_monotonic_ns = time.monotonic_ns()
        self._send(
            "slice_phase",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "lease_id": cmd.lease_id,
                "slice_id": cmd.slice_id,
                "stage": cmd.stage.value,
                "phase": "transport_started",
                "monotonic_ns": probe_started_monotonic_ns,
            },
        )
        endpoint = str(cmd.endpoint or "")
        budget = min(
            self.probe_timeout_seconds,
            max(0.001, float(cmd.probe_budget_seconds)),
        )
        attempt_started = time.monotonic()
        attempt_started_ns = time.monotonic_ns()
        probe_cleanup_clean = True
        if not endpoint:
            fact = ProbeAttemptFact(
                endpoint="",
                elapsed_ms=0,
                full_budget=False,
                completed=True,
                outcome="facility_fail",
                failure_domain="validator_contract",
                detail="missing_probe_endpoint",
                attempt_id=f"{cmd.lease_id}:1",
                generation_id=f"{self.worker_id}:{self.generation}",
                candidate_fingerprint=cmd.candidate_digest,
                session_lease_id=cmd.lease_id,
                stage=cmd.stage.value,
                granted_budget_ms=int(round(budget * 1000)),
            )
            passed = False
            latency_ms = None
        else:
            probe_task = asyncio.create_task(
                self._probe_once(endpoint, budget, cmd, 1)
            )
            try:
                completed, _pending = await asyncio.wait(
                    (probe_task,),
                    timeout=max(0.001, budget + 0.1),
                )
            except asyncio.CancelledError:
                await self._cancel_probe_task(probe_task)
                raise
            if probe_task in completed:
                fact, passed, latency_ms = probe_task.result()
            else:
                probe_cleanup_clean = await self._cancel_probe_task(probe_task)
                elapsed_ms = int((time.monotonic() - attempt_started) * 1000)
                fact = ProbeAttemptFact(
                    endpoint=endpoint,
                    elapsed_ms=elapsed_ms,
                    full_budget=False,
                    completed=False,
                    entitlement_exhausted=(
                        elapsed_ms + 25 >= int(round(budget * 1000))
                    ),
                    outcome="right_censored",
                    failure_domain="endpoint_deadline",
                    detail="endpoint_entitlement_expired",
                    attempt_id=f"{cmd.lease_id}:1",
                    generation_id=f"{self.worker_id}:{self.generation}",
                    candidate_fingerprint=cmd.candidate_digest,
                    session_lease_id=cmd.lease_id,
                    stage=cmd.stage.value,
                    transport_started_monotonic_ns=attempt_started_ns,
                    granted_budget_ms=int(round(budget * 1000)),
                    lifecycle="censored",
                )
                passed = False
                latency_ms = None

        confirm_facts: tuple[ProbeAttemptFact, ...] = ()
        confirm_kind: EndpointAttemptKind | None = None
        confirm_reason = ""
        if passed and getattr(self, "confirm_urls", ()):
            (
                confirm_facts,
                confirm_kind,
                confirm_reason,
                confirm_clean,
            ) = await self._confirm_mainstream_https(
                cmd,
                passed_fact=fact,
                total_started=total_started,
            )
            probe_cleanup_clean = probe_cleanup_clean and confirm_clean
            if confirm_kind is not None:
                # The portfolio endpoint passed but the node cannot load a
                # mainstream HTTPS site: this slice does not PASS.
                passed = False
                latency_ms = None

        if confirm_kind is not None:
            kind = confirm_kind
            reason = confirm_reason
        elif passed:
            kind = EndpointAttemptKind.PASS
            reason = "endpoint_probe_passed"
        elif fact.outcome == "candidate_fail":
            kind = EndpointAttemptKind.CANDIDATE_FAIL
            reason = fact.detail or "endpoint_candidate_failed"
        elif fact.outcome == "facility_fail":
            kind = EndpointAttemptKind.FACILITY_FAIL
            reason = fact.detail or "endpoint_facility_failed"
        elif fact.outcome == "right_censored" or not fact.completed:
            kind = EndpointAttemptKind.RIGHT_CENSORED
            reason = fact.detail or "endpoint_right_censored"
        else:
            kind = EndpointAttemptKind.TRANSIENT
            reason = fact.detail or "endpoint_transient"

        probe_elapsed_ms = int((time.monotonic() - probe_started) * 1000)
        return EndpointAttemptResult(
            refresh_epoch=cmd.refresh_epoch,
            transaction_id=cmd.transaction_id,
            candidate_digest=cmd.candidate_digest,
            config_digest=cmd.config_digest,
            lease_id=cmd.lease_id,
            slice_id=cmd.slice_id,
            sequence=cmd.sequence,
            stage=cmd.stage,
            purpose=cmd.purpose,
            worker_id=self.worker_id,
            worker_generation=self.generation,
            kind=kind,
            node_json=cmd.node_json,
            report_json=cmd.report_json,
            reason=reason,
            latency_ms=latency_ms,
            slow=bool(
                latency_ms is not None and latency_ms > self.slow_threshold_ms
            ),
            probe_url=endpoint if passed else "",
            # The portfolio fact stays first; confirm facts trail it.
            attempt_facts=(fact, *confirm_facts),
            facility_elapsed_ms=facility_elapsed_ms,
            portfolio_elapsed_ms=probe_elapsed_ms,
            total_elapsed_ms=int((time.monotonic() - total_started) * 1000),
            clean_confirmed=probe_cleanup_clean,
            helper_pid=os.getpid(),
            mihomo_pid=int(self.process.pid if self.process else 0),
            probe_policy_revision=cmd.probe_policy_revision,
            portfolio_digest=cmd.portfolio_digest,
            submitted_monotonic_ns=cmd.submitted_monotonic_ns,
            dispatch_monotonic_ns=cmd.dispatch_monotonic_ns,
            supervisor_assigned_monotonic_ns=(
                cmd.supervisor_assigned_monotonic_ns
            ),
            supervisor_sent_monotonic_ns=cmd.supervisor_sent_monotonic_ns,
            helper_started_monotonic_ns=helper_started_monotonic_ns,
            portfolio_started_monotonic_ns=probe_started_monotonic_ns,
        )

    async def _cancel_active(self, command: CancelCmd) -> None:
        task = self.active_task
        active = self.active_cmd
        if task is None or active is None or active.lease_id != command.lease_id:
            self._send(
                "clean_confirmed",
                {
                    "worker_id": self.worker_id,
                    "worker_generation": self.generation,
                    "lease_id": command.lease_id,
                    "sequence": command.sequence,
                    "reason": command.reason,
                    "active_probe_count": 0,
                },
            )
            return
        task.cancel()
        transport_aborted = await self._invalidate_probe_session()
        # The supervisor grants a separate 1.5s physical-cleanup entitlement.
        # Give an already-aborted Windows transport time to unwind inside the
        # same generation. Replacing the whole helper before this bounded
        # cleanup finishes discards otherwise healthy capacity under load.
        # The total remains below the parent's 1.5s physical cleanup bound.
        done, _pending = await asyncio.wait((task,), timeout=0.75)
        if task not in done:
            task.cancel()
            done, _pending = await asyncio.wait((task,), timeout=0.25)
        task_clean = task in done
        if task_clean:
            self._consume_detached_task(task)
        probes_clean = await self._settle_detached_probe_tasks(timeout=0.15)
        if not task_clean or (not probes_clean and not transport_aborted):
            # The parent owns the 1.5s physical cleanup bound.  Never claim a
            # reusable helper while a transport task can still own a socket.
            self.stopping = True
            return
        self.active_task = None
        self.active_cmd = None
        self._send(
            "clean_confirmed",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "lease_id": command.lease_id,
                "sequence": command.sequence,
                "reason": command.reason,
                "active_probe_count": 0,
            },
        )

    async def run(self) -> None:
        await self.start()
        while not self.stopping:
            now = time.monotonic()
            if now - self.last_heartbeat >= 0.5:
                self.last_heartbeat = now
                self._send(
                    "heartbeat",
                    {
                        "worker_id": self.worker_id,
                        "worker_generation": self.generation,
                        "helper_pid": os.getpid(),
                        "mihomo_pid": int(self.process.pid if self.process else 0),
                        "lease_id": self.active_cmd.lease_id if self.active_cmd else "",
                        "active_probe_count": int(self.active_task is not None),
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
            while self.connection.poll(0):
                kind, payload = parse_message(self.connection.recv_bytes())
                if kind == "validate_endpoint":
                    if self.active_task is not None:
                        raise RuntimeError("helper received overlapping validation lease")
                    cmd = validate_endpoint_cmd_from_payload(payload)
                    if cmd.worker_id != self.worker_id or cmd.worker_generation != self.generation:
                        raise RuntimeError("helper validation generation mismatch")
                    self.active_cmd = cmd
                    # Acknowledge Pipe consumption synchronously.  Facility
                    # work starts in the task below and has its own budget; it
                    # must not be conflated with event-loop scheduling delay.
                    self._send(
                        "slice_phase",
                        {
                            "worker_id": self.worker_id,
                            "worker_generation": self.generation,
                            "lease_id": cmd.lease_id,
                            "slice_id": cmd.slice_id,
                            "stage": cmd.stage.value,
                            "phase": "dispatch_received",
                            "monotonic_ns": time.monotonic_ns(),
                        },
                    )
                    self.active_task = asyncio.create_task(self.validate_endpoint(cmd))
                elif kind == "cancel":
                    await self._cancel_active(cancel_cmd_from_payload(payload))
                elif kind == "stop":
                    self.stopping = True
                    if self.active_cmd is not None:
                        await self._cancel_active(
                            CancelCmd(
                                refresh_epoch=self.active_cmd.refresh_epoch,
                                lease_id=self.active_cmd.lease_id,
                                worker_id=self.worker_id,
                                worker_generation=self.generation,
                                sequence=self.active_cmd.sequence,
                                reason="helper_stop",
                            )
                        )
                    break
                else:
                    raise RuntimeError(f"unsupported validator command: {kind}")
            if self.active_task is not None and self.active_task.done():
                task = self.active_task
                active = self.active_cmd
                self.active_task = None
                self.active_cmd = None
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    result = None
                except BaseException as exc:
                    if active is None:
                        raise
                    result = EndpointAttemptResult(
                        refresh_epoch=active.refresh_epoch,
                        candidate_digest=active.candidate_digest,
                        config_digest=active.config_digest,
                        lease_id=active.lease_id,
                        sequence=active.sequence,
                        worker_id=self.worker_id,
                        worker_generation=self.generation,
                        node_json=active.node_json,
                        kind=EndpointAttemptKind.FACILITY_FAIL,
                        transaction_id=active.transaction_id,
                        slice_id=active.slice_id,
                        stage=active.stage,
                        purpose=active.purpose,
                        report_json=active.report_json,
                        reason=f"helper_validation_exception:{type(exc).__name__}:{exc}",
                        clean_confirmed=True,
                        helper_pid=os.getpid(),
                        mihomo_pid=int(self.process.pid if self.process else 0),
                        probe_policy_revision=active.probe_policy_revision,
                        portfolio_digest=active.portfolio_digest,
                        submitted_monotonic_ns=active.submitted_monotonic_ns,
                        dispatch_monotonic_ns=active.dispatch_monotonic_ns,
                        supervisor_assigned_monotonic_ns=(
                            active.supervisor_assigned_monotonic_ns
                        ),
                        supervisor_sent_monotonic_ns=(
                            active.supervisor_sent_monotonic_ns
                        ),
                        helper_started_monotonic_ns=time.monotonic_ns(),
                    )
                if result is not None:
                    self._send("endpoint_result", endpoint_result_wire_payload(result))
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        if self.active_task is not None:
            self.active_task.cancel()
            await self._invalidate_probe_session()
            done, pending = await asyncio.wait((self.active_task,), timeout=0.4)
            for task in done:
                self._consume_detached_task(task)
            for task in pending:
                task.cancel()
            self.active_task = None
            self.active_cmd = None
        await self._settle_detached_probe_tasks(timeout=0.25)
        if self.probe_session is not None:
            await self.probe_session.close()
            self.probe_session = None
            self.probe_connector = None
        cleanup_tasks = tuple(self._detached_cleanup_tasks)
        if cleanup_tasks:
            done, pending = await asyncio.wait(cleanup_tasks, timeout=0.25)
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass
            for task in pending:
                task.cancel()
        if self.controller_session is not None:
            await self.controller_session.close()
            self.controller_session = None
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=0.25)
            except ProcessLookupError:
                pass
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.25)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
        shutil.rmtree(self.work_dir, ignore_errors=True)


async def run_validator_helper(connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None) -> None:
    runtime = ValidatorHelperRuntime(connection, start_payload, prewarmed=prewarmed)
    try:
        await runtime.run()
    finally:
        await runtime.close()
