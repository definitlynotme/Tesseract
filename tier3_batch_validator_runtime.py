"""Batch validator helper: one Mihomo process hosts several worker identities.

The single-slot helper (``tier3_validator_runtime``) reloads one candidate into
its Mihomo per lease.  Here the parent assigns one candidate to each of K worker
identities (slots); every slot owns one listener bound to its own proxy, and
each slot runs the *unmodified* ``ValidatorHelperRuntime.validate_endpoint``
against its own listener with its own probe session and task.

**Per-slot provider install (no rounds, no reloads).**  The Mihomo layout is
fixed once at group start: listener W -> select group W -> file provider W.
When slot W receives a lease, the helper writes W's provider file with the
candidate as its only proxy and issues ``PUT /providers/proxies/TEST-PROVIDER-W``.
Mihomo parses that one proxy and touches nothing else: no listener rebinds, no
sibling proxy is re-created, no shared lock is needed, and a candidate Mihomo
rejects is answered with a non-2xx status while the previous proxy stays -
so a poison pill fails alone by construction.  Measured with the bundled
binary and local identity sinks (``tests/test_tier3_batch_provider_install.py``):
an in-flight probe on listener A is untouched by an install on B, 16 concurrent
installs complete in ~160 ms wall at 20-80 ms each, and every listener routes
to its own candidate.  (The earlier whole-config hot-install, kept as the
experiment ``tests/test_tier3_batch_hot_install.py``, paid 271-664 ms per
reload at K=16 under a per-process lock.)

Invariants kept from the single-slot design: one lease per identity at a time;
every message a slot emits carries that slot's ``worker_id`` so the parent
routes results, phases, heartbeats and clean confirmations per identity;
cancelling one slot only touches that slot's transport; a slot whose transport
cannot be proven clean is poisoned and never reused.  An idle slot's provider
carries a REJECT placeholder, so a listener never has egress until its lease
installs a candidate.

**The event loop never writes the Pipe.**  A Windows Pipe buffers 8 KiB and a
write blocks until the parent reads.  Every frame the runtime emits is handed
to one sender thread in FIFO order, so a parent that falls behind delays
delivery but never freezes the K probes, their deadlines or command intake.
Liveness is one ``group_heartbeat`` per process carrying every slot's
``(worker_id, lease_id, active_probe_count)``.
"""
from __future__ import annotations

import asyncio
import copy
import errno
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from models import Node
from tier3_validation_config import (
    BATCH_PROVIDER_FILE_PREFIX,
    batch_provider_name,
    batch_provider_placeholder,
    build_batch_provider_validation_config,
    render_batch_provider_proxy,
)
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
from tier3_validator_runtime import ValidatorHelperRuntime, _NoDnsResolver

_HEARTBEAT_INTERVAL_SECONDS = 0.5
# Stop bounds.  After "stop" the parent gives the whole process one 0.5 s
# cooperative window and then kills its Job.  The K leases therefore unwind
# concurrently under ONE bound (a cancel normally completes in 20-50 ms), and
# close() cleans the K slots concurrently under one more, never one bound per
# slot.  A slot that misses a bound gets no stop-time clean_confirmed; the
# parent never needs one at stop (it reaps the Job either way).
_STOP_CANCEL_SECONDS = 0.3
_CLOSE_SETTLE_SECONDS = 0.2
# How long close() waits for the shared Mihomo it asked to exit before the
# inherited close() escalates (terminate, then kill).
_FACILITY_EXIT_WAIT_SECONDS = 0.25


class _PipeSender:
    """The only writer of a batch helper's Pipe once the runtime exists.

    ``send`` enqueues an encoded frame and returns at once; one thread writes
    frames in enqueue order, so the per-process message order is exactly the
    order the event loop produced.  A write failure is kept and raised to the
    next caller of ``send`` or ``raise_if_failed``, which ends the runtime the
    same way a failed synchronous write did.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._queue: "queue.SimpleQueue[bytes | None]" = queue.SimpleQueue()
        self._condition = threading.Condition()
        self._enqueued = 0
        self._written = 0
        self._failure: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="tier3-batch-helper-sender", daemon=True,
        )
        self._thread.start()

    def send(self, data: bytes) -> None:
        failure = self._failure
        if failure is not None:
            raise failure
        if self._closed:
            raise OSError(errno.EPIPE, "batch helper sender is closed")
        with self._condition:
            self._enqueued += 1
        self._queue.put(data)

    def raise_if_failed(self) -> None:
        failure = self._failure
        if failure is not None:
            raise failure

    def _run(self) -> None:
        while True:
            data = self._queue.get()
            if data is None:
                return
            try:
                self._connection.send_bytes(data)
            except BaseException as exc:  # noqa: BLE001
                with self._condition:
                    self._failure = exc
                    self._condition.notify_all()
                return
            with self._condition:
                self._written += 1
                self._condition.notify_all()

    def flush(self, timeout: float) -> bool:
        """Wait until every frame enqueued so far is written; False on failure or timeout."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._failure is None and self._written < self._enqueued:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return self._failure is None

    def close(self, timeout: float | None = None) -> bool:
        """Write every queued frame, then stop the thread; True once it has exited.

        With no ``timeout`` (what the runtime uses) this waits exactly as long
        as the synchronous writes it replaces would have blocked: a parent that
        is gone fails the pending write at once, which ends the thread, and a
        parent that is alive but not reading ends the process through its Job
        on its own deadlines.  A bounded wait would drop verdicts the helper
        already earned, and leave the thread writing while the bootstrap sends
        ``bootstrap_error`` on the same Pipe.
        """

        if not self._closed:
            self._closed = True
            self._queue.put(None)
        if timeout is None:
            while self._thread.is_alive():
                self._thread.join(0.5)
        else:
            self._thread.join(max(0.0, float(timeout)))
        return not self._thread.is_alive()


class _BatchProbeSlot(ValidatorHelperRuntime):
    """One worker identity.  Deliberately does not call super().__init__.

    Every attribute below is one the real validate_endpoint path reads; the
    slot borrows the shared facility from its owner and owns its listener port,
    probe session and task, so the probe behaves exactly as a single-slot
    helper's would.
    """

    def __init__(
        self,
        owner: "BatchValidatorHelperRuntime",
        worker_id: int,
        listener_port: int,
    ) -> None:
        self.owner = owner
        self.worker_id = int(worker_id)
        self.generation = owner.generation
        self.connection = owner.connection
        self.process = owner.process
        self.probe_urls = owner.probe_urls
        # The mainstream HTTPS confirm runs inside validate_endpoint; every
        # slot inherits the group's START setting.
        self.confirm_urls = owner.confirm_urls
        self.probe_timeout_seconds = owner.probe_timeout_seconds
        self.slow_threshold_ms = owner.slow_threshold_ms
        self.expected_statuses = owner.expected_statuses
        # The slot probes through its own listener; that port plays the role
        # the mixed-port plays for a single-slot helper.
        self.mixed_port = int(listener_port)
        self.listener_port = int(listener_port)
        self.proxy_name = f"TEST-PROXY-{int(worker_id)}"
        self.provider_name = batch_provider_name(worker_id)
        self.probe_session: aiohttp.ClientSession | None = None
        self.probe_connector: aiohttp.TCPConnector | None = None
        self._detached_probe_tasks: set[asyncio.Task[Any]] = set()
        self._detached_cleanup_tasks: set[asyncio.Future[Any]] = set()
        self.active_task: asyncio.Task[EndpointAttemptResult] | None = None
        self.active_cmd: ValidateEndpointCmd | None = None
        # bound_node/installed_digest: what this slot's provider currently
        # carries in the running Mihomo (empty while unknown, e.g. after an
        # interrupted update).
        self.bound_node: Node | None = None
        self.installed_digest = ""
        self.poisoned = False

    def _send(self, kind: str, payload: dict[str, Any]) -> None:
        # Slots share the owner's Pipe and its single ordered writer.
        self.owner._send(kind, payload)

    async def _reload_candidate(  # type: ignore[override]
        self, node: Node, cmd: ValidateEndpointCmd,
    ) -> tuple[bool, str, int]:
        """The only stubbed seam: install this slot's candidate in the facility phase.

        Running here (after ``facility_started`` was sent) keeps the parent's
        dispatch/facility/probe deadline phases identical to the single-slot
        helper: the provider update is facility work bounded by
        ``cmd.facility_budget_seconds``.
        """

        return await self.owner._install_candidate(
            self, node, cmd.candidate_digest, cmd.facility_budget_seconds,
        )

    def active_probe_count(self) -> int:
        # A poisoned slot must never look idle: the parent's cleanup deadline is
        # what retires the identity, and only a busy heartbeat lets it fire.
        if self.poisoned:
            return 1
        return int(self.active_task is not None or bool(self._detached_probe_tasks))


class BatchValidatorHelperRuntime(ValidatorHelperRuntime):
    """A helper whose one Mihomo serves K worker identities at once."""

    def __init__(self, connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None) -> None:
        super().__init__(connection, start_payload, prewarmed=prewarmed)
        member_ids = [int(item) for item in start_payload.get("member_worker_ids", ())]
        listener_ports = [int(item) for item in start_payload.get("listener_ports", ())]
        if not member_ids or len(member_ids) != len(listener_ports):
            raise ValueError("batch validator START must name one listener port per worker identity")
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("batch validator worker identities must be distinct")
        reserved = {self.mixed_port, self.controller_port}
        for port in listener_ports:
            if port <= 0 or port in reserved:
                raise ValueError("batch validator listener ports must be positive and distinct")
            reserved.add(port)
        if self.worker_id not in member_ids:
            raise ValueError("batch validator lead identity must be one of its members")
        self.member_ids = tuple(member_ids)
        self.listener_ports = tuple(listener_ports)
        self.slots: dict[int, _BatchProbeSlot] = {}
        self._slot_heartbeat_at = 0.0
        self._install_count = 0
        self._cancel_tasks: set[asyncio.Task[Any]] = set()
        self._facility_terminate_requested = False
        # Provider files must live under Mihomo's home directory (-d).
        self.provider_dir = self.work_dir / "batch-providers"
        # Last: every frame from here on (including start()'s startup_progress
        # and ready) goes through the ordered sender thread.
        self._sender = _PipeSender(connection)

    def _send(self, kind: str, payload: dict[str, Any]) -> None:
        self._sender.send(message_bytes(kind, payload))

    # -------------------------------------------------------------- install

    def _provider_path(self, slot: _BatchProbeSlot) -> Path:
        return self.provider_dir / f"{BATCH_PROVIDER_FILE_PREFIX}{slot.worker_id}.yaml"

    def _write_provider(self, slot: _BatchProbeSlot, proxy: dict[str, Any]) -> Path:
        """Atomically publish ``proxy`` as the sole entry of the slot's provider file."""

        path = self._provider_path(slot)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            yaml.safe_dump({"proxies": [proxy]}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        # Mihomo watches provider files and re-reads them on change (measured:
        # a bare write is live within 200 ms).  The explicit PUT below is the
        # synchronous acknowledgement; the watcher may already hold the file
        # open when we swap it in, which on Windows surfaces as a transient
        # PermissionError - retry briefly instead of failing the lease.
        deadline = time.monotonic() + 0.25
        while True:
            try:
                os.replace(tmp, path)
                return path
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    async def _install_layout(self) -> None:
        """Install the fixed listener/group/provider layout once, after prewarm.

        Every slot starts on a REJECT placeholder.  All K listeners must accept
        before the group announces itself; a listener that never comes up is a
        group-level failure (the parent retires and replaces the process).
        """

        if self.controller_session is None:
            raise RuntimeError("controller session unavailable for batch layout")
        self.provider_dir.mkdir(parents=True, exist_ok=True)
        for slot in self.slots.values():
            self._write_provider(slot, batch_provider_placeholder(slot.worker_id))
        config = build_batch_provider_validation_config(
            [(slot.worker_id, slot.listener_port) for slot in self.slots.values()],
            self.mixed_port,
            provider_dir=str(self.provider_dir),
            controller_port=self.controller_port,
            ipv6=self.ipv6,
        )
        if self._dns_block is not None:
            config["dns"] = copy.deepcopy(self._dns_block)
        path = self._write_config(config, "batch-layout")
        async with asyncio.timeout(10.0):
            try:
                async with self.controller_session.put(
                    "/configs",
                    params={"force": "true"},
                    json={"path": str(path), "payload": ""},
                    timeout=aiohttp.ClientTimeout(total=8.0, connect=0.5, sock_connect=0.5, sock_read=8.0),
                ) as response:
                    body = await response.content.read(4097)
                    if response.status >= 300:
                        raise RuntimeError(
                            f"batch layout rejected: status {response.status} {body[:256]!r}"
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                # The answer was lost, not the request: Mihomo may well have
                # applied the layout.  The listener wait below is the proof
                # that matters; if the listeners never come up, the outer
                # deadline raises and the parent replaces the group.
                pass
            pending = {slot.worker_id: slot.listener_port for slot in self.slots.values()}
            while pending:
                for worker_id, port in list(pending.items()):
                    writer = None
                    try:
                        _reader, writer = await asyncio.wait_for(
                            asyncio.open_connection("127.0.0.1", port), timeout=0.25,
                        )
                        del pending[worker_id]
                    except (asyncio.TimeoutError, OSError):
                        pass
                    finally:
                        if writer is not None:
                            writer.close()
                if pending:
                    await asyncio.sleep(0.02)

    async def _install_candidate(
        self,
        slot: _BatchProbeSlot,
        node: Node,
        candidate_digest: str,
        facility_budget: float,
    ) -> tuple[bool, str, int]:
        """Make ``node`` the sole proxy behind ``slot``'s listener.

        One provider update per lease, independent of every sibling: no lock,
        no shared file, no listener change.  A non-2xx answer means Mihomo
        rejected this candidate and kept the previous proxy - the lease fails
        alone with a facility verdict shaped like the single-slot
        ``reload_status_*``.  Any interruption between the request and its
        answer leaves the binding unknown, so the "already installed" shortcut
        is disabled until a later update commits.
        """

        started = time.monotonic()
        requested = str(candidate_digest)
        if slot.installed_digest == requested and slot.bound_node is not None:
            return True, "provider_reused", int((time.monotonic() - started) * 1000)
        proxy = render_batch_provider_proxy(node, slot.worker_id)
        if proxy is None:
            return False, "unsupported_proxy", 0
        if self.controller_session is None:
            return False, "controller_unavailable", 0
        slot.installed_digest = ""
        budget = max(0.1, float(facility_budget))
        try:
            async with asyncio.timeout(budget):
                # File I/O (and its short watcher-contention retry) runs off
                # the event loop, so K siblings' probes and heartbeats never
                # stall behind one slot's install.
                await asyncio.to_thread(self._write_provider, slot, proxy)
                # The controller session's default timeout (1.0 s total,
                # 0.5 s read) is sized for status polls; an install is
                # facility work and gets the whole facility budget.
                async with self.controller_session.put(
                    f"/providers/proxies/{slot.provider_name}",
                    timeout=aiohttp.ClientTimeout(
                        total=budget, connect=0.5, sock_connect=0.5, sock_read=budget,
                    ),
                ) as response:
                    body = await response.content.read(4097)
                    if response.status >= 300:
                        # Mihomo kept the previous proxy; only this lease fails.
                        return False, f"provider_status_{response.status}:{body[:256]!r}", int(
                            (time.monotonic() - started) * 1000
                        )
        except asyncio.TimeoutError:
            return False, "facility_deadline", int((time.monotonic() - started) * 1000)
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            return False, f"facility_error:{type(exc).__name__}:{exc}", int(
                (time.monotonic() - started) * 1000
            )
        slot.bound_node = node
        slot.installed_digest = requested
        self._install_count += 1
        return True, "", int((time.monotonic() - started) * 1000)

    # ------------------------------------------------------------------ run

    def _mark_unusable(self, slot: _BatchProbeSlot, reason: str) -> None:
        """Poison a slot and tell the parent to retire that one identity."""

        slot.poisoned = True
        slot._send(
            "slot_unusable",
            {
                "worker_id": slot.worker_id,
                "worker_generation": self.generation,
                "reason": reason,
                "monotonic_ns": time.monotonic_ns(),
            },
        )

    def _dispatch(self, cmd: ValidateEndpointCmd) -> None:
        slot = self.slots.get(int(cmd.worker_id))
        if slot is None or cmd.worker_generation != self.generation or slot.active_task is not None:
            # A single-slot helper raises here and loses one lease.  In a
            # shared process that would cost every sibling, so answer the
            # offending lease with a facility failure and keep the process.
            reason = (
                "batch_protocol_unknown_identity" if slot is None
                else "batch_protocol_generation_mismatch" if cmd.worker_generation != self.generation
                else "batch_protocol_overlapping_lease"
            )
            probe = slot if slot is not None else _BatchProbeSlot(self, int(cmd.worker_id), 0)
            self._send(
                "endpoint_result",
                endpoint_result_wire_payload(self._facility_result(probe, cmd, reason)),
            )
            return
        if slot.poisoned:
            self._send(
                "endpoint_result",
                endpoint_result_wire_payload(self._facility_result(slot, cmd, "batch_slot_poisoned")),
            )
            self._mark_unusable(slot, "batch_slot_poisoned")
            return
        slot.active_cmd = cmd
        slot._send(
            "slice_phase",
            {
                "worker_id": slot.worker_id,
                "worker_generation": self.generation,
                "lease_id": cmd.lease_id,
                "slice_id": cmd.slice_id,
                "stage": cmd.stage.value,
                "phase": "dispatch_received",
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        slot.active_task = asyncio.create_task(
            ValidatorHelperRuntime.validate_endpoint(slot, cmd)
        )

    def _facility_result(
        self, slot: _BatchProbeSlot, cmd: ValidateEndpointCmd, reason: str,
    ) -> EndpointAttemptResult:
        return EndpointAttemptResult(
            refresh_epoch=cmd.refresh_epoch,
            candidate_digest=cmd.candidate_digest,
            config_digest=cmd.config_digest,
            lease_id=cmd.lease_id,
            sequence=cmd.sequence,
            worker_id=slot.worker_id,
            worker_generation=self.generation,
            node_json=cmd.node_json,
            kind=EndpointAttemptKind.FACILITY_FAIL,
            transaction_id=cmd.transaction_id,
            slice_id=cmd.slice_id,
            stage=cmd.stage,
            purpose=cmd.purpose,
            report_json=cmd.report_json,
            reason=reason,
            clean_confirmed=True,
            helper_pid=os.getpid(),
            mihomo_pid=int(self.process.pid if self.process else 0),
            probe_policy_revision=cmd.probe_policy_revision,
            portfolio_digest=cmd.portfolio_digest,
            submitted_monotonic_ns=cmd.submitted_monotonic_ns,
            dispatch_monotonic_ns=cmd.dispatch_monotonic_ns,
            supervisor_assigned_monotonic_ns=cmd.supervisor_assigned_monotonic_ns,
            supervisor_sent_monotonic_ns=cmd.supervisor_sent_monotonic_ns,
            helper_started_monotonic_ns=time.monotonic_ns(),
        )

    def _sweep(self) -> None:
        for slot in self.slots.values():
            task = slot.active_task
            if task is None or not task.done():
                continue
            active = slot.active_cmd
            slot.active_task = None
            slot.active_cmd = None
            try:
                result = task.result()
            except asyncio.CancelledError:
                result = None
            except BaseException as exc:  # noqa: BLE001
                result = self._facility_result(
                    slot, active, f"helper_validation_exception:{type(exc).__name__}:{exc}"
                ) if active is not None else None
            if result is not None:
                self._send("endpoint_result", endpoint_result_wire_payload(result))

    async def _cancel_slot(self, command: CancelCmd) -> None:
        """Cancel one identity's lease without touching its siblings."""

        slot = self.slots.get(int(command.worker_id))
        if slot is None:
            return
        task = slot.active_task
        active = slot.active_cmd
        if task is None or active is None or active.lease_id != command.lease_id:
            slot._send(
                "clean_confirmed",
                {
                    "worker_id": slot.worker_id,
                    "worker_generation": self.generation,
                    "lease_id": command.lease_id,
                    "sequence": command.sequence,
                    "reason": command.reason,
                    "active_probe_count": slot.active_probe_count(),
                },
            )
            return
        task.cancel()
        transport_aborted = await slot._invalidate_probe_session()
        done, _pending = await asyncio.wait((task,), timeout=0.75)
        if task not in done:
            task.cancel()
            done, _pending = await asyncio.wait((task,), timeout=0.25)
        task_clean = task in done
        if task_clean:
            slot._consume_detached_task(task)
        probes_clean = await slot._settle_detached_probe_tasks(timeout=0.15)
        if not task_clean or (not probes_clean and not transport_aborted):
            # Only this identity can still own a socket: retire it, keep the
            # process and the siblings.
            self._mark_unusable(slot, "batch_slot_unclean_cleanup")
            return
        slot.active_task = None
        slot.active_cmd = None
        slot._send(
            "clean_confirmed",
            {
                "worker_id": slot.worker_id,
                "worker_generation": self.generation,
                "lease_id": command.lease_id,
                "sequence": command.sequence,
                "reason": command.reason,
                "active_probe_count": 0,
            },
        )

    def _send_group_heartbeat(self) -> None:
        """One frame for the whole process; the parent fans it out per identity.

        Each entry keeps the per-slot heartbeat's fields, so a poisoned slot
        still reports ``active_probe_count`` 1 and a slot's lease id stays its
        own.
        """

        self._send(
            "group_heartbeat",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "helper_pid": os.getpid(),
                "mihomo_pid": int(self.process.pid if self.process else 0),
                "monotonic_ns": time.monotonic_ns(),
                "slots": [
                    [
                        slot.worker_id,
                        slot.active_cmd.lease_id if slot.active_cmd else "",
                        slot.active_probe_count(),
                    ]
                    for slot in self.slots.values()
                ],
            },
        )

    async def _widen_controller_session(self) -> None:
        """K identities update K providers at once: give the controller client K connections."""

        old = self.controller_session
        if old is None:
            return
        timeout = aiohttp.ClientTimeout(total=1.0, connect=0.5, sock_connect=0.5, sock_read=0.5)
        self.controller_session = aiohttp.ClientSession(
            base_url=f"http://127.0.0.1:{self.controller_port}",
            timeout=timeout,
            trust_env=False,
            connector=aiohttp.TCPConnector(
                resolver=_NoDnsResolver(),
                force_close=True,
                use_dns_cache=False,
                limit=max(2, len(self.slots) + 1),
            ),
            auto_decompress=False,
            max_line_size=8192,
            max_field_size=8192,
        )
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- stop

    def _terminate_facility(self) -> None:
        """Ask the shared Mihomo to exit now, without waiting for it.

        Nothing is validated once the helper stops, so the facility goes first:
        its teardown (~100 ms in the kernel) then overlaps the slot cleanup
        instead of following it, and no lease can linger on a live upstream.
        """

        process = self.process
        if process is None or self._facility_terminate_requested:
            return
        try:
            if process.returncode is not None:
                return
            # Once only: on Windows a second TerminateProcess on an exiting
            # process records its exit code before the process is gone, which
            # would end the exit wait early (and remove files it still holds).
            self._facility_terminate_requested = True
            process.terminate()
        except (AttributeError, OSError):
            # ProcessLookupError (already gone) and PermissionError (exiting)
            # are OSErrors; close() still waits and escalates.
            pass

    async def _await_facility_exit(self, timeout: float) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.returncode is not None:
                return
            await asyncio.wait_for(process.wait(), timeout=max(0.0, timeout))
        except (asyncio.TimeoutError, AttributeError, OSError):
            pass

    async def _stop_slots(self) -> None:
        """Unwind every in-flight lease at once, under one bound.

        Each active slot gets the ordinary per-slot cancel (clean_confirmed
        with reason ``helper_stop`` once its transport is proven clean); cancels
        already running for deadlines share the same bound.  Whatever is still
        running when the bound expires is left to close().
        """

        self._terminate_facility()
        for slot in list(self.slots.values()):
            if slot.active_cmd is None:
                continue
            task = asyncio.create_task(
                self._cancel_slot(
                    CancelCmd(
                        refresh_epoch=slot.active_cmd.refresh_epoch,
                        lease_id=slot.active_cmd.lease_id,
                        worker_id=slot.worker_id,
                        worker_generation=self.generation,
                        sequence=slot.active_cmd.sequence,
                        reason="helper_stop",
                    )
                )
            )
            self._cancel_tasks.add(task)
            task.add_done_callback(self._cancel_tasks.discard)
        if self._cancel_tasks:
            await asyncio.wait(list(self._cancel_tasks), timeout=_STOP_CANCEL_SECONDS)

    async def _settle_slots(self, timeout: float) -> None:
        """Release every slot's transport concurrently under one bound (close only)."""

        async def settle(slot: _BatchProbeSlot) -> None:
            task = slot.active_task
            if task is not None and not task.done():
                task.cancel()
            await slot._invalidate_probe_session()
            await slot._settle_detached_probe_tasks(timeout=timeout)

        work: list[asyncio.Future[Any]] = [
            task for task in self._cancel_tasks if not task.done()
        ]
        work.extend(asyncio.ensure_future(settle(slot)) for slot in list(self.slots.values()))
        if work:
            done, pending = await asyncio.wait(work, timeout=max(0.0, timeout))
            for item in done:
                if not item.cancelled():
                    item.exception()  # retrieved: close() is best effort
            for item in pending:
                item.cancel()
        for slot in self.slots.values():
            slot.active_task = None
            slot.active_cmd = None

    async def run(self) -> None:  # noqa: C901
        await self.start()
        self.slots = {
            worker_id: _BatchProbeSlot(self, worker_id, port)
            for worker_id, port in zip(self.member_ids, self.listener_ports)
        }
        await self._widen_controller_session()
        await self._install_layout()
        # Announce that every identity is ready to carry a lease.
        self._send(
            "batch_ready",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "member_worker_ids": list(self.member_ids),
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        while not self.stopping:
            # A failed Pipe write ends the helper exactly as a failed
            # synchronous write did: the exception leaves run().
            self._sender.raise_if_failed()
            now = time.monotonic()
            if self.process is not None and self.process.returncode is not None:
                # The shared facility is gone: every identity in this process
                # is lost.  Deliver verdicts already earned, then raise so the
                # parent sees a helper_error, retires the group and gives each
                # still-in-flight lease exactly one facility terminal.  close()
                # drains the sender before the bootstrap reports the error.
                self._sweep()
                raise RuntimeError(
                    f"shared Mihomo exited during validation: {self.process.returncode}"
                )
            if now - self._slot_heartbeat_at >= _HEARTBEAT_INTERVAL_SECONDS:
                self._slot_heartbeat_at = now
                self._send_group_heartbeat()
            while self.connection.poll(0):
                kind, payload = parse_message(self.connection.recv_bytes())
                if kind == "validate_endpoint":
                    self._dispatch(validate_endpoint_cmd_from_payload(payload))
                elif kind == "cancel":
                    # Never block the loop on one slot's cleanup: siblings'
                    # results, heartbeats and dispatch acks must keep flowing.
                    task = asyncio.create_task(self._cancel_slot(cancel_cmd_from_payload(payload)))
                    self._cancel_tasks.add(task)
                    task.add_done_callback(self._cancel_tasks.discard)
                elif kind == "stop":
                    self.stopping = True
                    # All slots at once: the parent's cooperative window is
                    # one bound for the whole process, not one per slot.
                    await self._stop_slots()
                    self._sweep()
                    return
                else:
                    raise RuntimeError(f"unsupported batch validator command: {kind}")
            self._sweep()
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        try:
            # Whatever ended run() (stop, a lost Pipe, a dead facility),
            # nothing validates after close: the facility's exit overlaps the
            # slot cleanup, and each step has one bound for all K slots.
            self.stopping = True
            self._terminate_facility()
            try:
                await self._settle_slots(_CLOSE_SETTLE_SECONDS)
            except Exception:  # noqa: BLE001
                pass
            await self._await_facility_exit(_FACILITY_EXIT_WAIT_SECONDS)
            # Controller session, a still-running facility (terminate, then
            # kill) and the work directory.
            await super().close()
        finally:
            # Every frame already produced (results, clean confirmations,
            # slot_unusable) is written before this returns, so it precedes
            # anything the bootstrap sends afterwards on the same Pipe, and
            # the sender thread is gone before the bootstrap writes.  No
            # deadline: see _PipeSender.close.
            self._sender.close()


async def run_batch_validator_helper(
    connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None,
) -> None:
    runtime = BatchValidatorHelperRuntime(connection, start_payload, prewarmed=prewarmed)
    try:
        await runtime.run()
    finally:
        await runtime.close()
