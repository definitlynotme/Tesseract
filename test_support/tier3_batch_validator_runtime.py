"""Prototype helper that validates several candidates from one shared Mihomo.

This exists to answer one question codex posed: at the SAME number of
validation slots, is a shared process faster than a process per candidate?  It
is a measurement instrument, not a production path, and nothing the app imports
may import it.

The design keeps the measured code identical between arms.  Each logical slot is
a ``_BatchProbeSlot`` that subclasses the real ``ValidatorHelperRuntime`` with a
bypassed ``__init__`` and is then handed to the *unmodified*
``ValidatorHelperRuntime.validate_endpoint``.  So the probe, the session
handling, the budget clamp, the censoring ladder and the result assembly are the
same bytecode in both arms.  Exactly one seam is stubbed, and it is named:
``_reload_candidate`` returns the verdict the batch already decided when it
loaded its config, because in a batch the candidate is pre-loaded rather than
hot-reloaded per lease.

Per codex's rule, an ACTIVE batch is never hot-updated: a generation's config is
fixed while its leases run, results return per candidate as they finish, and the
config is only swapped between generations.  Each slot owns its own probe
session, so cancelling one slot cannot close a sibling's transport.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp

from models import Node
from tier3_validation_config import build_batch_proxy_validation_config
from tier3_validation_protocol import (
    CancelCmd,
    EndpointAttemptKind,
    EndpointAttemptResult,
    ValidateEndpointCmd,
    cancel_cmd_from_payload,
    endpoint_result_wire_payload,
    message_bytes,
    parse_message,
    validate_endpoint_cmd_from_payload,
)
from tier3_validator_runtime import ValidatorHelperRuntime


class _BatchProbeSlot(ValidatorHelperRuntime):
    """One logical validation slot. Deliberately does not call super().__init__.

    Every attribute below is one the real validate_endpoint path reads; the slot
    borrows them from its owner so the probe behaves exactly as a production
    helper's would, but with its own port, its own session and its own task.
    """

    def __init__(self, owner: "BatchValidatorHelperRuntime", slot_index: int) -> None:
        self.owner = owner
        self.slot_index = int(slot_index)
        self.connection = owner.connection
        self.worker_id = owner.worker_id
        self.generation = owner.generation
        self.process = owner.process
        self.probe_urls = owner.probe_urls
        self.probe_timeout_seconds = owner.probe_timeout_seconds
        self.slow_threshold_ms = owner.slow_threshold_ms
        self.expected_statuses = owner.expected_statuses
        # Rebound per lease to the candidate's own listener port, only while the
        # slot is idle; force_close sessions mean no socket outlives a request.
        self.mixed_port = 0
        self.listener_port = 0
        self.probe_session: aiohttp.ClientSession | None = None
        self.probe_connector: aiohttp.TCPConnector | None = None
        self._detached_probe_tasks: set[asyncio.Task[Any]] = set()
        self._detached_cleanup_tasks: set[asyncio.Future[Any]] = set()
        self.active_task: asyncio.Task[EndpointAttemptResult] | None = None
        self.active_cmd: ValidateEndpointCmd | None = None
        self.facility_verdict: tuple[bool, str, int] = (True, "batch_preloaded", 0)
        self.batch_generation = 0
        self.poisoned = False

    async def _reload_candidate(  # type: ignore[override]
        self, node: Node, cmd: ValidateEndpointCmd,
    ) -> tuple[bool, str, int]:
        """The only stubbed seam: a batch candidate is already loaded."""

        return self.facility_verdict

    def _send(self, kind: str, payload: dict[str, Any]) -> None:
        enriched = dict(payload)
        enriched["slot_index"] = self.slot_index
        self.connection.send_bytes(message_bytes(kind, enriched))


class _BatchEntry:
    __slots__ = ("index", "listener_port", "proxy_name", "verdict", "generation")

    def __init__(self, index: int, listener_port: int, proxy_name: str,
                 verdict: tuple[bool, str, int]) -> None:
        self.index = index
        self.listener_port = listener_port
        self.proxy_name = proxy_name
        self.verdict = verdict
        self.generation = 0


class BatchValidatorHelperRuntime(ValidatorHelperRuntime):
    """A helper whose one Mihomo serves several slots at once."""

    def __init__(self, connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None) -> None:
        super().__init__(connection, start_payload, prewarmed=prewarmed)
        self.slot_count = max(1, int(start_payload.get("slot_count", 1)))
        self.slots: list[_BatchProbeSlot] = []
        self.free_slots: list[int] = []
        self.batch_generation = 0
        self.batch_entries: dict[str, _BatchEntry] = {}
        # A batch may only be probed once its config is proven installed and its
        # bindings read back. Without this gate a failed load would leave the
        # previous generation still serving the reused listener ports, and a new
        # candidate could be credited with the old candidate's answer.
        self.batch_loaded = False
        self.batch_ready_ports: set[int] = set()

    # ---------------------------------------------------------------- batches

    async def _load_generation(self, payload: dict[str, Any]) -> None:
        """Install one fixed batch config. Never called while a lease runs."""

        if any(slot.active_task is not None for slot in self.slots):
            self._send("batch_error", {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "reason": "hot_update_refused",
            })
            return
        started = time.monotonic()
        # Retire the previous generation before anything else: until the new one
        # proves itself, no candidate may be probed at all.
        self.batch_loaded = False
        self.batch_ready_ports = set()
        nodes = [Node.from_dict(item) for item in payload["nodes"]]
        digests = [str(item) for item in payload["candidate_digests"]]
        listener_ports = [int(item) for item in payload["listener_ports"]]
        facility_budget = max(0.5, float(payload.get("facility_budget_seconds", 10.0)))
        self.batch_generation += 1
        config, names = build_batch_proxy_validation_config(
            list(zip(nodes, listener_ports)),
            self.mixed_port,
            controller_port=self.controller_port,
            ipv6=self.ipv6,
        )
        if isinstance(self._dns_block, dict):
            config["dns"] = json.loads(json.dumps(self._dns_block))
        render_ms = int((time.monotonic() - started) * 1000)

        self.batch_entries = {}
        unrenderable: list[int] = []
        for index, digest in enumerate(digests):
            if not names[index]:
                unrenderable.append(index)
                self.batch_entries[digest] = _BatchEntry(
                    index, 0, "", (False, "unsupported_proxy", 0),
                )
                continue
            # Not "preloaded" until the load is proven; see the gate in _dispatch.
            self.batch_entries[digest] = _BatchEntry(
                index, listener_ports[index], names[index], (True, "batch_preloaded", 0),
            )
            self.batch_entries[digest].generation = self.batch_generation

        reload_started = time.monotonic()
        path = self._write_config(config, f"batch-{self.batch_generation}")
        expected = {name for name in names if name}
        loaded = False
        if self.controller_session is not None and expected:
            try:
                async with asyncio.timeout(facility_budget):
                    async with self.controller_session.put(
                        "/configs", params={"force": "true"},
                        json={"path": str(path), "payload": ""},
                    ) as response:
                        await response.content.read(4097)
                        if response.status >= 300:
                            raise RuntimeError(f"reload_status_{response.status}")
                    while True:
                        async with self.controller_session.get("/proxies") as response:
                            if response.status == 200:
                                body = await response.json(content_type=None)
                                proxies = body.get("proxies") if isinstance(body, dict) else None
                                if isinstance(proxies, dict) and expected <= set(proxies):
                                    loaded = True
                                    break
                        await asyncio.sleep(0.02)
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError, RuntimeError):
                loaded = False
        reload_ms = int((time.monotonic() - reload_started) * 1000)

        ready_started = time.monotonic()
        pending = [port for index, port in enumerate(listener_ports) if names[index]]
        deadline = time.monotonic() + facility_budget
        while loaded and pending and time.monotonic() < deadline:
            remaining = []
            for port in pending:
                writer = None
                try:
                    _reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", port), timeout=0.25,
                    )
                except (asyncio.TimeoutError, OSError):
                    remaining.append(port)
                finally:
                    if writer is not None:
                        writer.close()
            pending = remaining
            if pending:
                await asyncio.sleep(0.02)
        listeners_ready_ms = int((time.monotonic() - ready_started) * 1000)
        self.batch_loaded = bool(loaded and not pending)
        self.batch_ready_ports = (
            {port for index, port in enumerate(listener_ports) if names[index]}
            if self.batch_loaded else set()
        )

        self._send("batch_generation_ready", {
            "worker_id": self.worker_id,
            "worker_generation": self.generation,
            "batch_generation": self.batch_generation,
            "candidate_digests": digests,
            "listener_ports": listener_ports,
            "unrenderable_indexes": unrenderable,
            "loaded": bool(loaded and not pending),
            "config_render_ms": render_ms,
            "reload_ms": reload_ms,
            "listeners_ready_ms": listeners_ready_ms,
            "monotonic_ns": time.monotonic_ns(),
        })

    # ------------------------------------------------------------------- run

    def _facility_failure(self, cmd: ValidateEndpointCmd, reason: str) -> EndpointAttemptResult:
        return EndpointAttemptResult(
            refresh_epoch=cmd.refresh_epoch,
            transaction_id=cmd.transaction_id,
            candidate_digest=cmd.candidate_digest,
            config_digest=cmd.config_digest,
            lease_id=cmd.lease_id,
            slice_id=cmd.slice_id,
            sequence=cmd.sequence,
            stage=cmd.stage,
            worker_id=self.worker_id,
            worker_generation=self.generation,
            node_json=cmd.node_json,
            purpose=cmd.purpose,
            kind=EndpointAttemptKind.FACILITY_FAIL,
            reason=reason,
        )

    def _dispatch(self, cmd: ValidateEndpointCmd) -> None:
        entry = self.batch_entries.get(cmd.candidate_digest)
        if entry is not None and entry.proxy_name and not self.batch_loaded:
            # The generation never proved itself. Probing now could reach a
            # previous generation still bound to this reused listener port and
            # credit its answer to this candidate, so refuse rather than risk
            # a false pass.
            self.connection.send_bytes(message_bytes(
                "endpoint_result",
                endpoint_result_wire_payload(
                    self._facility_failure(cmd, "batch_generation_not_loaded")
                ),
            ))
            return
        if entry is not None and entry.proxy_name and (
            entry.generation != self.batch_generation
            or entry.listener_port not in self.batch_ready_ports
        ):
            self.connection.send_bytes(message_bytes(
                "endpoint_result",
                endpoint_result_wire_payload(
                    self._facility_failure(cmd, "batch_generation_mismatch")
                ),
            ))
            return
        if entry is None:
            self.connection.send_bytes(message_bytes(
                "endpoint_result",
                endpoint_result_wire_payload(
                    self._facility_failure(cmd, "batch_entry_missing")
                ),
            ))
            return
        if not self.free_slots:
            self.connection.send_bytes(message_bytes(
                "endpoint_result",
                endpoint_result_wire_payload(
                    self._facility_failure(cmd, "batch_no_free_slot")
                ),
            ))
            return
        slot_index = self.free_slots.pop(0)
        slot = self.slots[slot_index]
        if slot.active_task is not None:
            raise RuntimeError(f"batch helper received overlapping lease on slot {slot_index}")
        slot.mixed_port = entry.listener_port
        slot.listener_port = entry.listener_port
        slot.batch_generation = self.batch_generation
        slot.facility_verdict = entry.verdict
        slot.active_cmd = cmd
        slot._send("slice_phase", {
            "worker_id": self.worker_id,
            "worker_generation": self.generation,
            "lease_id": cmd.lease_id,
            "slice_id": cmd.slice_id,
            "stage": cmd.stage.value,
            "phase": "dispatch_received",
            "monotonic_ns": time.monotonic_ns(),
        })
        slot.active_task = asyncio.create_task(
            ValidatorHelperRuntime.validate_endpoint(slot, cmd)
        )

    async def _sweep(self) -> None:
        for index, slot in enumerate(self.slots):
            task = slot.active_task
            if task is None or not task.done():
                continue
            cmd = slot.active_cmd
            slot.active_task = None
            slot.active_cmd = None
            try:
                result = task.result()
            except asyncio.CancelledError:
                result = None
            except BaseException as exc:  # noqa: BLE001
                result = self._facility_failure(
                    cmd, f"batch_slot_error:{type(exc).__name__}",
                ) if cmd is not None else None
            if result is not None:
                payload = dict(endpoint_result_wire_payload(result))
                payload["slot_index"] = index
                payload["batch_generation"] = slot.batch_generation
                payload["listener_port"] = slot.listener_port
                self.connection.send_bytes(message_bytes("endpoint_result", payload))
            if not slot.poisoned:
                self.free_slots.append(index)

    async def _cancel_lease(self, command: CancelCmd) -> None:
        """Cancel one slot without touching its siblings' transports."""

        for slot in self.slots:
            if slot.active_cmd is None or slot.active_cmd.lease_id != command.lease_id:
                continue
            task = slot.active_task
            if task is not None:
                task.cancel()
            await slot._invalidate_probe_session()
            if task is not None:
                _done, pending = await asyncio.wait((task,), timeout=0.75)
                if pending:
                    _done, pending = await asyncio.wait((task,), timeout=0.25)
                    if pending:
                        slot.poisoned = True
            await slot._settle_detached_probe_tasks(timeout=0.15)
            slot.active_task = None
            slot.active_cmd = None
            if not slot.poisoned and slot.slot_index not in self.free_slots:
                self.free_slots.append(slot.slot_index)
            self._send("clean_confirmed", {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "lease_id": command.lease_id,
                "slot_index": slot.slot_index,
                "poisoned": bool(slot.poisoned),
                "monotonic_ns": time.monotonic_ns(),
            })
            return

    async def run(self) -> None:  # noqa: C901
        await self.start()
        self.slots = [_BatchProbeSlot(self, index) for index in range(self.slot_count)]
        for slot in self.slots:
            slot.process = self.process
        self.free_slots = list(range(self.slot_count))
        while not self.stopping:
            now = time.monotonic()
            if now - self.last_heartbeat >= 0.5:
                self.last_heartbeat = now
                self._send("heartbeat", {
                    "worker_id": self.worker_id,
                    "worker_generation": self.generation,
                    "helper_pid": __import__("os").getpid(),
                    "mihomo_pid": int(self.process.pid if self.process else 0),
                    "lease_id": "",
                    "active_probe_count": sum(
                        1 for slot in self.slots if slot.active_task is not None
                    ),
                    "monotonic_ns": time.monotonic_ns(),
                })
            while self.connection.poll(0):
                kind, payload = parse_message(self.connection.recv_bytes())
                if kind == "load_batch_generation":
                    await self._load_generation(payload)
                elif kind == "validate_endpoint":
                    self._dispatch(validate_endpoint_cmd_from_payload(payload))
                elif kind == "cancel":
                    await self._cancel_lease(cancel_cmd_from_payload(payload))
                elif kind == "stop":
                    self.stopping = True
                    break
                else:
                    raise RuntimeError(f"unsupported batch validator command: {kind}")
            await self._sweep()
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        for slot in self.slots:
            task = slot.active_task
            if task is not None:
                task.cancel()
            try:
                await slot._invalidate_probe_session()
                await slot._settle_detached_probe_tasks(timeout=0.25)
            except BaseException:  # noqa: BLE001
                pass
        await super().close()


async def run_batch_validator_helper(
    connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None,
) -> None:
    runtime = BatchValidatorHelperRuntime(connection, start_payload, prewarmed=prewarmed)
    try:
        await runtime.run()
    finally:
        await runtime.close()
