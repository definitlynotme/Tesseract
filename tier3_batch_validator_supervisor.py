"""Batch validator backend: one helper process carries K worker identities.

``Tier3ValidatorSupervisor`` models one worker identity as one helper process
with one Mihomo.  This subclass keeps every per-identity invariant the actor
and the domain rely on (one lease per identity, generation-bound capacity
leases, the dispatch/facility/probe/cancel deadline machine, parent-sealed
leases, clean confirmation) and changes only the physical layer:

* a *group* of K identities shares one process, one Job, one Pipe and one
  Mihomo; identity ``w`` of group ``g`` is ``(g - 1) * K + w``;
* each identity owns one listener port inside that Mihomo, so it still probes
  through a port bound to exactly its own candidate;
* a lease is dispatched exactly like the single-slot backend: the base
  supervisor reserves one identity, sends it the command, and the helper
  hot-installs that one candidate into the shared Mihomo (a reload that leaves
  every other slot byte-identical) before probing.  No staging, no rounds;
* a slot-level fault (unclean cleanup, protocol mismatch) quarantines that one
  identity, never the process.  A process-level fault retires the whole group
  and every staged or in-flight lease receives exactly one facility terminal;
* a group's identities become capacity at ``batch_ready`` (listener layout
  installed, helper reading its Pipe), not at the controller ``ready``;
* liveness arrives as one ``group_heartbeat`` per process and is fanned out to
  the per-identity heartbeat handling; per-slot ``heartbeat`` is still accepted.

The public interface is identical to the single-slot supervisor, so the
validation domain, the actor and the manager need no knowledge of groups: the
number of active identities *is* the validation concurrency.
"""
from __future__ import annotations

import multiprocessing.connection
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tier3_batch_pipe_io import (
    has_queued_message,
    hold_gil_for_sends,
    receive_queued_message,
)
from tier3_batch_validator_helper import batch_helper_process_bootstrap
from tier3_defaults import tier3_process_worker_ceiling
from tier3_validation_config import build_validator_prewarm_config
from tier3_validation_protocol import (
    GenerationGone,
    Tier3TerminalKind,
    Tier3WorkerPhase,
    parse_message,
)
from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE
from tier3_validator_supervisor import (
    Tier3ValidatorSupervisor,
    Tier3WorkerStarter,
    _WorkerGeneration,
    _close_stopped_process,
    _process_is_alive,
    _process_sentinel,
    _reserve_unique_loopback_port,
    _terminate_jobs_bounded,
)
from tier3_windows_job import WindowsJob, ports_released

_METRICS_LOG_INTERVAL_SECONDS = 15.0
# The owner's stop returns this long before the caller's deadline, so an owner
# thread that is descheduled for a moment still ends inside the caller's join.
_OWNER_STOP_MARGIN_SECONDS = 0.2
# A failed stop logs its recorded stages as one bounded line.
_STOP_TRACE_LIMIT = 64
_STOP_LOG_MAX_CHARS = 1800
# Startup phase between the helper's controller ``ready`` and its
# ``batch_ready`` (listener layout installed, Pipe read loop running).
_LAYOUT_PENDING = "layout_pending"

_ACTIVE_PHASES = {
    Tier3WorkerPhase.IDLE,
    Tier3WorkerPhase.RESERVED,
    Tier3WorkerPhase.VALIDATING,
    Tier3WorkerPhase.CANCELLING,
}
_LEASE_PHASES = {Tier3WorkerPhase.VALIDATING, Tier3WorkerPhase.CANCELLING}
_GONE_PHASES = {
    Tier3WorkerPhase.RETIRING,
    Tier3WorkerPhase.QUARANTINED,
    Tier3WorkerPhase.STOPPED,
}


@dataclass(slots=True)
class _BatchWorkerGeneration(_WorkerGeneration):
    group: Any = None
    listener_port: int = 0


class _HelperGroup:
    """The shared physical facility behind K worker identities."""

    __slots__ = (
        "index",
        "generation",
        "process",
        "connection",
        "job",
        "send_lock",
        "members",
        "lead",
        "mixed_port",
        "controller_port",
        "listener_ports",
        "retiring",
        "retirement_started_at",
        "retirement_terminal_kind",
        "retirement_replace",
        "retirement_record_failure",
        "retirement_was_ready",
        "replacement_reason",
        "recycle_pending",
    )

    def __init__(
        self,
        index: int,
        generation: int,
        process: Any,
        connection: Any,
        job: WindowsJob,
        send_lock: threading.Lock,
    ) -> None:
        self.index = int(index)
        self.generation = int(generation)
        self.process = process
        self.connection = connection
        self.job = job
        self.send_lock = send_lock
        self.members: list[_BatchWorkerGeneration] = []
        self.lead: _BatchWorkerGeneration | None = None
        self.mixed_port = 0
        self.controller_port = 0
        self.listener_ports: list[int] = []
        self.retiring = False
        self.retirement_started_at = 0.0
        self.retirement_terminal_kind: Tier3TerminalKind | None = None
        self.retirement_replace = True
        self.retirement_record_failure = True
        self.retirement_was_ready = False
        self.replacement_reason = ""
        self.recycle_pending = False


def _group_job_key(group: _HelperGroup) -> tuple[int, int]:
    """How a group's Job is keyed among the caller's stop duplicates."""

    lead_id = group.lead.worker_id if group.lead is not None else -group.index
    return (int(lead_id), int(group.generation))


def _count_reason(counts: dict[str, int], reason: str) -> None:
    """Count ``reason`` by its category (the text before the first colon)."""

    category = str(reason or "unknown").split(":", 1)[0][:96]
    counts[category] = int(counts.get(category, 0)) + 1


def _top_reasons(counts: dict[str, int], limit: int = 6) -> str:
    """Render the ``limit`` most frequent reasons as ``a=3,b=1`` for a log line."""

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return ",".join(f"{name}={count}" for name, count in ranked) or "-"


class Tier3BatchValidatorSupervisor(Tier3ValidatorSupervisor):
    """Supervisor whose helpers host ``slots_per_process`` identities each."""

    def __init__(
        self,
        *,
        slots_per_process: int,
        helper_target: Callable[[Any], None] = batch_helper_process_bootstrap,
        **kwargs: Any,
    ) -> None:
        requested_min = int(kwargs.get("min_workers", 4))
        requested_max = int(kwargs.get("max_workers", 16))
        requested_startup_bulkhead = int(kwargs.get("startup_bulkhead", 8))
        requested_mihomo_bulkhead = int(kwargs.get("mihomo_start_bulkhead", 8))
        super().__init__(helper_target=helper_target, **kwargs)
        self.slots_per_process = max(1, int(slots_per_process))
        # The audited process ceiling still bounds processes; identities scale
        # with the slots each process carries.
        slot_ceiling = tier3_process_worker_ceiling() * self.slots_per_process
        self.min_workers = max(1, min(slot_ceiling, requested_min))
        self.max_workers = max(self.min_workers, min(slot_ceiling, requested_max))
        self._desired_workers = self.min_workers
        self._shell_prewarm_workers = self.min_workers
        group_ceiling = self._groups_for(self.max_workers)
        self.startup_bulkhead = max(1, min(8, group_ceiling, requested_startup_bulkhead))
        self.mihomo_start_bulkhead = max(1, min(8, group_ceiling, requested_mihomo_bulkhead))
        self._starter = Tier3WorkerStarter(
            context=self._context,
            helper_target=self._helper_target,
            job_empty_wait_seconds=self.job_empty_wait_seconds,
            wake=self._wake.set,
            max_concurrent=self.startup_bulkhead,
        )
        self._groups: dict[int, _HelperGroup] = {}
        self._disabled_groups: dict[int, str] = {}
        self._group_failure_streak: dict[int, int] = {}
        self._group_spawn_retry_at: dict[int, float] = {}
        self._dispatch_total = 0
        self._slot_quarantine_count = 0
        self._group_retirement_count = 0
        self._quarantine_reasons: dict[str, int] = {}
        self._group_retirement_reasons: dict[str, int] = {}
        self._last_metrics_log_at = time.monotonic()
        self._stop_trace: list[tuple[float, str]] = []
        self._stop_failure_logged = False
        # Jobs the caller of stop() holds a duplicate of, and the groups whose
        # Job the owner could not confirm empty: the caller may only turn the
        # owner's failed verdict into STOPPED when it can see every such Job.
        self._caller_job_keys: frozenset[tuple[int, int]] = frozenset()
        self._stop_failed_job_keys: frozenset[tuple[int, int]] = frozenset()

    # ------------------------------------------------------------- helpers

    def _groups_for(self, identities: int) -> int:
        return max(1, (int(identities) + self.slots_per_process - 1) // self.slots_per_process)

    def _member_ids(self, group_index: int) -> list[int]:
        first = (int(group_index) - 1) * self.slots_per_process + 1
        return list(range(first, first + self.slots_per_process))

    def _member_for(self, group: _HelperGroup, payload: dict[str, Any]) -> _BatchWorkerGeneration | None:
        try:
            worker_id = int(payload.get("worker_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        with self._lock:
            member = self._workers.get(worker_id)
        if isinstance(member, _BatchWorkerGeneration) and member.group is group:
            return member
        return None

    def batch_metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "slots_per_process": self.slots_per_process,
                "groups": len(self._groups),
                "dispatches": self._dispatch_total,
                "slot_quarantines": self._slot_quarantine_count,
                "group_retirements": self._group_retirement_count,
                "quarantine_reasons": dict(sorted(self._quarantine_reasons.items())),
                "group_retirement_reasons": dict(
                    sorted(self._group_retirement_reasons.items())
                ),
            }

    def _maybe_log_metrics(self) -> None:
        now = time.monotonic()
        if now - self._last_metrics_log_at < _METRICS_LOG_INTERVAL_SECONDS:
            return
        self._last_metrics_log_at = now
        metrics = self.batch_metrics()
        self._log(
            "info",
            (
                "Tier3 batch validator (slots_per_process=%s groups=%s dispatches=%s "
                "slot_quarantines=%s group_retirements=%s "
                "quarantine_reasons=%s group_retirement_reasons=%s)"
            ),
            metrics["slots_per_process"],
            metrics["groups"],
            metrics["dispatches"],
            metrics["slot_quarantines"],
            metrics["group_retirements"],
            _top_reasons(metrics["quarantine_reasons"]),
            _top_reasons(metrics["group_retirement_reasons"]),
        )

    # ------------------------------------------------------------- lifecycle

    def _dispatch_reserved_command(self, worker, command, *, direct):  # type: ignore[override]
        accepted = super()._dispatch_reserved_command(worker, command, direct=direct)
        if accepted:
            with self._lock:
                self._dispatch_total += 1
        return accepted

    def start(self) -> None:
        if (
            self._helper_target is batch_helper_process_bootstrap
            and (not self.binary_path or not Path(self.binary_path).is_file())
        ):
            raise FileNotFoundError(
                f"Mihomo binary not found for batch validator supervisor: {self.binary_path}"
            )
        super().start()

    # ----------------------------------------------------------- spawning

    def _record_group_spawn_failure(self, group_index: int, exc: BaseException) -> None:
        failures = int(self._group_failure_streak.get(group_index, 0)) + 1
        self._group_failure_streak[group_index] = failures
        self._startup_failure_count += 1
        self._last_failure_reason = f"worker_spawn_failed:{type(exc).__name__}:{exc}"
        self._record_failure_reason(self._last_failure_reason)
        self._group_spawn_retry_at[group_index] = time.monotonic() + min(
            2.0, 0.25 * (2 ** max(0, failures - 1)),
        )
        self._log("error", "Tier3 batch validator group %s spawn failed: %s", group_index, exc)
        if failures >= 3:
            self._disabled_groups[group_index] = self._last_failure_reason
            for worker_id in self._member_ids(group_index):
                self._disabled_workers[worker_id] = self._last_failure_reason

    def _drain_spawn_results(self, *, discard: bool = False) -> None:
        for result in self._starter.drain_results():
            group_index = int(result.worker_id)
            with self._lock:
                expected_generation = self._pending_spawns.pop(group_index, None)
            shell = result.worker
            if result.error is not None:
                if not self._stopping.is_set() and not discard:
                    self._record_group_spawn_failure(group_index, result.error)
                continue
            if shell is None:
                continue
            if discard or self._stopping.is_set() or expected_generation != result.generation:
                self._starter._dispose_worker(shell)
                continue
            # One owner thread reads (and the actor writes) every frame of this
            # group's identities: keep the GIL across Pipe calls that cannot
            # block (tier3_batch_pipe_io).  The object is shared with the shell
            # and every member, so the switch is made once, here.
            hold_gil_for_sends(shell.connection)
            group = _HelperGroup(
                group_index, result.generation, shell.process, shell.connection, shell.job, shell.send_lock,
            )
            members = [
                _BatchWorkerGeneration(
                    worker_id=worker_id,
                    generation=result.generation,
                    process=shell.process,
                    connection=shell.connection,
                    job=shell.job,
                    helper_pid=shell.helper_pid,
                    send_lock=group.send_lock,
                    group=group,
                )
                for worker_id in self._member_ids(group_index)
            ]
            group.members = members
            group.lead = members[0]
            with self._lock:
                duplicate = group_index in self._groups or any(
                    member.worker_id in self._workers for member in members
                )
                if not duplicate:
                    self._groups[group_index] = group
                    for member in members:
                        self._workers[member.worker_id] = member
            if duplicate:
                self._starter._dispose_worker(shell)

    def _schedule_group_spawn(self, group_index: int) -> bool:
        generation = int(self._next_generation.get(group_index, 0)) + 1
        self._next_generation[group_index] = generation
        if not self._starter.submit(group_index, generation):
            return False
        with self._lock:
            self._pending_spawns[group_index] = generation
        return True

    def _group_hard_stall_check_unlocked(self) -> None:
        usable = any(
            not group.retiring
            and any(member.phase not in {Tier3WorkerPhase.QUARANTINED, Tier3WorkerPhase.STOPPED}
                    for member in group.members)
            for group in self._groups.values()
        )
        desired_groups = self._groups_for(self._desired_workers)

        def replaceable(index: int) -> bool:
            if index in self._disabled_groups:
                return False
            current = self._groups.get(index)
            if current is None:
                return True
            # A group on its way out will be replaced once its Job is empty:
            # that is progress, not a stall.
            return bool(current.retiring and current.retirement_replace)

        eligible_missing = any(replaceable(index) for index in range(1, desired_groups + 1))
        if not usable and not self._pending_spawns and not eligible_missing:
            self._hard_stall = True
            self._stop_status = "VALIDATOR_POOL_HARD_STALL"

    def _start_needed_workers(self) -> None:
        self._drain_spawn_results()
        self._activate_waiting_groups()
        with self._lock:
            existing = set(self._groups) | set(self._pending_spawns)
            desired_groups = self._groups_for(self._desired_workers)
            shell_groups = self._groups_for(
                max(self._desired_workers, self._shell_prewarm_workers)
            )
            shell_slots = max(0, self.startup_bulkhead - len(self._pending_spawns))
            # The initial cohort is counted in processes, like the single-slot
            # backend's four helpers: up to four groups spawn at once, bounded
            # by the startup bulkhead.
            initial_groups = min(4, self._groups_for(self._desired_workers))
            initial_facilities_started = all(
                index in self._groups
                and self._groups[index].lead is not None
                and self._groups[index].lead.prewarm_started
                and self._groups[index].lead.phase in {
                    Tier3WorkerPhase.STARTING, *_ACTIVE_PHASES,
                }
                for index in range(1, initial_groups + 1)
            )
            if not self._initial_cohort_ready_unlocked() and not initial_facilities_started:
                shell_slots = min(shell_slots, max(0, initial_groups - len(existing)))
        if self._resource_headroom_limited:
            with self._lock:
                usable = any(
                    member.phase not in {Tier3WorkerPhase.QUARANTINED, Tier3WorkerPhase.STOPPED}
                    for member in self._workers.values()
                )
                if not usable:
                    self._hard_stall = True
                    self._stop_status = "VALIDATOR_RESOURCE_HARD_STALL"
            return
        for index in range(1, desired_groups + 1):
            if shell_slots <= 0:
                break
            if index in existing or index in self._disabled_groups:
                continue
            if time.monotonic() < float(self._group_spawn_retry_at.get(index, 0.0)):
                continue
            if not self._schedule_group_spawn(index):
                continue
            existing.add(index)
            shell_slots -= 1
        prewarm_slots = min(1, shell_slots)
        for index in range(desired_groups + 1, shell_groups + 1):
            if prewarm_slots <= 0:
                break
            if index in existing or index in self._disabled_groups:
                continue
            if time.monotonic() < float(self._group_spawn_retry_at.get(index, 0.0)):
                continue
            if not self._schedule_group_spawn(index):
                continue
            existing.add(index)
            prewarm_slots -= 1
        with self._lock:
            self._group_hard_stall_check_unlocked()

    def _activate_waiting_groups(self) -> None:
        with self._lock:
            desired_groups = self._groups_for(self._desired_workers)
            starting = sum(
                1
                for group in self._groups.values()
                if group.lead is not None and group.lead.phase is Tier3WorkerPhase.STARTING
            )
            waiting = [
                group
                for group in sorted(self._groups.values(), key=lambda item: item.index)
                if group.lead is not None
                and group.lead.phase is Tier3WorkerPhase.WAITING_START
                and group.lead.bootstrap_ready
                and group.index <= desired_groups
            ]
            start_slots = max(0, self.mihomo_start_bulkhead - starting)
        for group in waiting[:start_slots]:
            lead = group.lead
            try:
                with self._lock:
                    if self._groups.get(group.index) is not group:
                        continue
                    if group.mixed_port <= 0 or group.controller_port <= 0 or not group.listener_ports:
                        leased: list[int] = []
                        for _ in range(2 + self.slots_per_process):
                            leased.append(
                                _reserve_unique_loopback_port(self._leased_ports | set(leased))
                            )
                        self._leased_ports.update(leased)
                        group.mixed_port = leased[0]
                        group.controller_port = leased[1]
                        group.listener_ports = leased[2:]
                        for member, port in zip(group.members, group.listener_ports):
                            member.mixed_port = group.mixed_port
                            member.controller_port = group.controller_port
                            member.listener_port = port
            except BaseException as exc:
                self._retire_group(
                    group,
                    f"helper_port_lease_failed:{type(exc).__name__}:{exc}",
                    terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                )
                continue
            payload = {
                "worker_id": lead.worker_id,
                "worker_generation": group.generation,
                "binary_path": self.binary_path,
                "probe_urls": list(self.probe_urls),
                "confirm_urls": list(self.confirm_urls),
                "expected_statuses": sorted(self.expected_statuses),
                "probe_timeout_seconds": self.probe_timeout_seconds,
                "slow_threshold_ms": self.slow_threshold_ms,
                "temp_parent": self.temp_parent,
                "ipv6": self.ipv6,
                "mixed_port": group.mixed_port,
                "controller_port": group.controller_port,
                "prewarm_config": build_validator_prewarm_config(
                    group.mixed_port, group.controller_port, ipv6=self.ipv6,
                ),
                "slot_count": self.slots_per_process,
                "member_worker_ids": [member.worker_id for member in group.members],
                "listener_ports": list(group.listener_ports),
            }
            if not self._send(lead, "start", payload):
                self._retire_group(
                    group,
                    "helper_start_latch_send_failed",
                    terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                )
                continue
            now = time.monotonic()
            with self._lock:
                if self._groups.get(group.index) is not group:
                    continue
                for member in group.members:
                    member.phase = Tier3WorkerPhase.STARTING
                    member.started_at = now
                    member.heartbeat_at = now
                    member.startup_phase = "start_sent"
                    member.startup_deadline_at = now + self.start_timeout_seconds
            starting += 1

    def _mirror_startup(self, group: _HelperGroup) -> None:
        lead = group.lead
        if lead is None:
            return
        with self._lock:
            if self._groups.get(group.index) is not group:
                return
            for member in group.members[1:]:
                if member.phase is not lead.phase or member.phase is not Tier3WorkerPhase.STARTING:
                    continue
                if member.startup_phase == "membership_pending":
                    continue
                member.startup_phase = lead.startup_phase
                member.heartbeat_at = lead.heartbeat_at
                member.startup_deadline_at = lead.startup_deadline_at
                member.prewarm_started = lead.prewarm_started
                member.prewarm_started_at = lead.prewarm_started_at
                member.mihomo_pid = lead.mihomo_pid

    # ------------------------------------------------------------ messages

    def _handle_group_ready(self, group: _HelperGroup, payload: dict[str, Any]) -> None:
        reported_mixed_port = int(payload.get("mixed_port", 0) or 0)
        reported_controller_port = int(payload.get("controller_port", 0) or 0)
        if (
            reported_mixed_port != group.mixed_port
            or reported_controller_port != group.controller_port
            or reported_mixed_port <= 0
            or reported_controller_port <= 0
            or reported_mixed_port == reported_controller_port
        ):
            self._retire_group(
                group,
                "helper_port_lease_mismatch",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
            return
        helper_pid = int(payload.get("helper_pid", 0) or 0)
        mihomo_pid = int(payload.get("mihomo_pid", 0) or 0)
        with self._lock:
            if group.lead is None or group.lead.phase is not Tier3WorkerPhase.STARTING:
                self._capacity_reservation_rejected_count += 1
                return
            # The controller is up, but the helper installs its listener
            # layout before it reads its Pipe again.  Capacity waits for
            # batch_ready; the startup deadline is re-armed to cover the layout.
            layout_deadline = time.monotonic() + self.start_timeout_seconds
            for member in group.members:
                member.helper_pid = helper_pid
                member.mihomo_pid = mihomo_pid
                member.startup_phase = _LAYOUT_PENDING
                member.startup_deadline_at = layout_deadline

    def _handle_group_batch_ready(self, group: _HelperGroup, payload: dict[str, Any]) -> None:
        """Grant the group's K identities once every listener is installed."""

        try:
            reported = [int(item) for item in payload.get("member_worker_ids", ())]
        except (TypeError, ValueError):
            reported = []
        with self._lock:
            if (
                self._groups.get(group.index) is not group
                or group.lead is None
                or group.lead.phase is not Tier3WorkerPhase.STARTING
                or group.lead.startup_phase != _LAYOUT_PENDING
            ):
                self._capacity_reservation_rejected_count += 1
                return
            mismatch = reported != [member.worker_id for member in group.members]
            if not mismatch:
                membership_deadline = time.monotonic() + 5.0
                for member in group.members:
                    if (
                        member.phase is Tier3WorkerPhase.STARTING
                        and member.startup_phase == _LAYOUT_PENDING
                    ):
                        member.startup_phase = "membership_pending"
                        member.startup_deadline_at = membership_deadline
        if mismatch:
            self._retire_group(
                group,
                "helper_batch_ready_member_mismatch",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
            return
        for member in group.members:
            self._confirm_ready_membership(member)

    def _handle_group_heartbeat(self, group: _HelperGroup, payload: dict[str, Any]) -> None:
        """Fan one per-process heartbeat out to the per-identity heartbeat handler.

        Each ``slots`` entry ``[worker_id, lease_id, active_probe_count]`` is
        handled exactly like a per-slot ``heartbeat`` carrying the frame's
        generation, pids and timestamp.  An entry naming no member of this
        group is the same protocol fault as a per-slot heartbeat would be.
        """

        entries = payload.get("slots")
        if not isinstance(entries, list):
            entries = [None]
        with self._lock:
            members = {
                member.worker_id: member
                for member in group.members
                if self._workers.get(member.worker_id) is member
            }
        shared = {
            key: payload[key]
            for key in ("worker_generation", "helper_pid", "mihomo_pid", "monotonic_ns")
            if key in payload
        }
        for entry in entries:
            member = None
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                try:
                    member = members.get(int(entry[0]))
                except (TypeError, ValueError):
                    member = None
            if member is None:
                self._retire_group(
                    group,
                    "helper_protocol_unknown_worker:heartbeat",
                    terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                )
                return
            self._handle_heartbeat(
                member,
                {
                    **shared,
                    "worker_id": entry[0],
                    "lease_id": entry[1],
                    "active_probe_count": entry[2],
                },
            )

    def _handle_pipe(self, worker: _WorkerGeneration) -> None:
        group = getattr(worker, "group", None)
        if group is None:
            super()._handle_pipe(worker)
            return
        try:
            self._pipe_wait.release(group.connection)
            raw = receive_queued_message(group.connection)
            if raw == BOOTSTRAP_READY_MESSAGE:
                now = time.monotonic()
                with self._lock:
                    if self._groups.get(group.index) is group:
                        for member in group.members:
                            if member.phase is Tier3WorkerPhase.WAITING_START:
                                member.bootstrap_ready = True
                                member.heartbeat_at = now
                self._wake.set()
                return
            kind, payload = parse_message(raw)
        except (EOFError, BrokenPipeError, OSError, ValueError) as exc:
            if self._stopping.is_set() or (group.lead is not None and group.lead.stop_requested):
                with self._lock:
                    if self._groups.get(group.index) is group:
                        for member in group.members:
                            member.phase = Tier3WorkerPhase.RETIRING
                            member.replacement_reason = "stop_pipe_closed"
                return
            self._retire_group(
                group,
                f"helper_pipe_lost:{type(exc).__name__}",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
            return
        if kind == "ready":
            self._handle_group_ready(group, payload)
        elif kind == "startup_progress":
            # Progress frames precede ready on the ordered Pipe; one arriving
            # during the layout must not rewind the startup phase.
            if group.lead is not None and group.lead.startup_phase != _LAYOUT_PENDING:
                self._handle_startup_progress(group.lead, payload)
                self._mirror_startup(group)
        elif kind == "group_heartbeat":
            self._handle_group_heartbeat(group, payload)
        elif kind in {"slice_phase", "endpoint_result", "clean_confirmed", "heartbeat"}:
            member = self._member_for(group, payload)
            if member is None:
                self._retire_group(
                    group,
                    f"helper_protocol_unknown_worker:{kind}",
                    terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                )
                return
            if kind == "slice_phase":
                self._handle_slice_phase(member, payload)
            elif kind == "endpoint_result":
                self._handle_endpoint_result(member, payload)
                with self._lock:
                    if member.phase is Tier3WorkerPhase.IDLE:
                        self._group_failure_streak[group.index] = 0
            elif kind == "clean_confirmed":
                self._handle_clean_confirmed(member, payload)
            else:
                self._handle_heartbeat(member, payload)
        elif kind == "batch_ready":
            # Every listener is installed and the helper reads its Pipe:
            # only now do the identities become capacity.
            self._handle_group_batch_ready(group, payload)
        elif kind == "slot_unusable":
            member = self._member_for(group, payload)
            if member is not None:
                # Same verdict shape as the base cleanup-timeout retirement:
                # once transport started the lease is censored, before that it
                # is a facility failure.
                terminal_kind = (
                    Tier3TerminalKind.CENSORED
                    if member.transport_started_at > 0.0
                    else Tier3TerminalKind.FACILITY_FAIL
                )
                self._quarantine_slot(
                    member,
                    f"slot_unusable:{payload.get('reason') or 'unknown'}",
                    terminal_kind=terminal_kind,
                    record_failure=True,
                )
        elif kind in {"bootstrap_error", "helper_error"}:
            # A bare TimeoutError has no message; name the type and the frames
            # that raised it so a failed group is diagnosable from the log.
            trace_tail = " | ".join(
                line.strip()
                for line in str(payload.get("traceback") or "").strip().splitlines()[-6:]
                if line.strip()
            )
            self._log(
                "error",
                "Tier3 batch validator helper failed: %s: %s (%s)",
                payload.get("error_type") or kind,
                payload.get("error") or "",
                trace_tail[:900] or "no traceback",
            )
            self._retire_group(
                group,
                f"helper_error:{payload.get('error_type') or 'unknown'}:{payload.get('error') or ''}",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
        else:
            self._retire_group(
                group,
                f"helper_protocol_unknown_message:{kind}",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )

    def _drain_worker_pipe(
        self,
        worker: _WorkerGeneration,
        *,
        max_messages: int = 32,
        first_message_ready: bool = False,
    ) -> int:
        group = getattr(worker, "group", None)
        if group is None:
            return super()._drain_worker_pipe(
                worker, max_messages=max_messages, first_message_ready=first_message_ready,
            )
        handled = 0
        while handled < max(1, int(max_messages)):
            if self._stopping.is_set():
                # One group Pipe carries K identities' frames: a burst can be
                # hundreds of messages, and the stop path reads what it needs
                # itself.  Never keep the owner away from _stop_all.
                break
            with self._lock:
                if self._groups.get(group.index) is not group:
                    break
            try:
                if handled == 0:
                    first_message_ready = (
                        self._pipe_wait.release(group.connection) or first_message_ready
                    )
                ready = (
                    (handled == 0 and first_message_ready)
                    or has_queued_message(group.connection)
                )
            except (EOFError, BrokenPipeError, OSError):
                self._handle_pipe(worker)
                handled += 1
                break
            if not ready:
                break
            self._handle_pipe(worker)
            handled += 1
        return handled


    # ---------------------------------------------------------- retirement

    @staticmethod
    def _is_slot_level_reason(reason: str) -> bool:
        # A cleanup timeout in the facility/probe phase means one identity's
        # transport would not unwind: retire that identity.  A timeout before
        # the helper even acknowledged the dispatch (dispatch_ack,
        # facility_start) means the process is unresponsive: retire the group.
        return (
            reason in {"helper_endpoint_result_unclean", "helper_endpoint_result_protocol_mismatch"}
            or reason in {"facility_deadline_cleanup_timeout", "probe_deadline_cleanup_timeout"}
        )

    def _retire_worker(
        self,
        worker: _WorkerGeneration,
        reason: str,
        *,
        terminal_kind: Tier3TerminalKind | None = None,
        replace_worker: bool = True,
        record_failure: bool = True,
    ) -> None:
        group = getattr(worker, "group", None)
        if group is None:
            super()._retire_worker(
                worker,
                reason,
                terminal_kind=terminal_kind,
                replace_worker=replace_worker,
                record_failure=record_failure,
            )
            return
        if self._is_slot_level_reason(reason):
            self._quarantine_slot(
                worker, reason, terminal_kind=terminal_kind, record_failure=record_failure,
            )
            return
        self._retire_group(
            group,
            reason,
            terminal_kind=terminal_kind,
            replace_worker=replace_worker,
            record_failure=record_failure,
        )

    def _quarantine_slot(
        self,
        worker: _WorkerGeneration,
        reason: str,
        *,
        terminal_kind: Tier3TerminalKind | None,
        record_failure: bool,
    ) -> None:
        group = worker.group
        with self._lock:
            if self._workers.get(worker.worker_id) is not worker:
                return
            if worker.phase in _GONE_PHASES:
                return
            was_idle = worker.phase is Tier3WorkerPhase.IDLE and worker.command is None
            worker.phase = Tier3WorkerPhase.QUARANTINED
            worker.reservation_id = 0
            worker.replacement_reason = reason
            worker.retirement_started_at = time.monotonic()
            self._disabled_workers[worker.worker_id] = reason
            self._slot_quarantine_count += 1
            _count_reason(self._quarantine_reasons, reason)
            if record_failure:
                self._last_failure_reason = str(reason)
                self._record_failure_reason(reason)
            command = worker.command
        if was_idle:
            self.capacity_sink(-1)
        if command is not None and terminal_kind is not None:
            # The identity may still own a socket; say so.  Its siblings keep
            # the process and their own transports.
            self._emit_lost_result(worker, terminal_kind, reason, clean_confirmed=False)
        with self._lock:
            worker.command = None
            worker.deadline_at = 0.0
            worker.lease_hard_deadline_at = 0.0
            worker.deadline_phase = ""
            worker.transport_started_at = 0.0
            worker.cancel_deadline_at = 0.0
            worker.sealed_lease_id = ""
            worker.cancel_reason = ""
            worker.cancel_requested_monotonic_ns = 0
            gone = sum(1 for member in group.members if member.phase in _GONE_PHASES)
            all_gone = gone == len(group.members)
            # Half the identities gone: the process is worth replacing.  Do it
            # once no sibling holds a lease, so nothing in flight is cut.
            if gone * 2 >= len(group.members):
                group.recycle_pending = True
        if all_gone:
            self._retire_group(
                group,
                "all_slots_quarantined",
                terminal_kind=None,
                replace_worker=True,
                record_failure=False,
            )
        else:
            self._recycle_groups()

    def _recycle_groups(self) -> None:
        with self._lock:
            due = [
                group
                for group in self._groups.values()
                if group.recycle_pending
                and not group.retiring
                and not any(
                    member.phase in {
                        Tier3WorkerPhase.RESERVED,
                        Tier3WorkerPhase.VALIDATING,
                        Tier3WorkerPhase.CANCELLING,
                    }
                    for member in group.members
                )
            ]
        for group in due:
            self._retire_group(
                group,
                "slot_quarantine_recycle",
                terminal_kind=None,
                replace_worker=True,
                record_failure=False,
            )

    def _retire_group(
        self,
        group: _HelperGroup,
        reason: str,
        *,
        terminal_kind: Tier3TerminalKind | None,
        replace_worker: bool = True,
        record_failure: bool = True,
    ) -> None:
        idle_released = 0
        with self._lock:
            if self._groups.get(group.index) is not group or group.retiring:
                return
            group.retiring = True
            group.retirement_started_at = time.monotonic()
            group.replacement_reason = reason
            group.retirement_terminal_kind = terminal_kind
            group.retirement_replace = bool(replace_worker)
            group.retirement_record_failure = bool(record_failure)
            group.retirement_was_ready = bool(
                group.lead is not None and group.lead.ready_at > 0.0
            )
            for member in group.members:
                if member.phase in _GONE_PHASES:
                    if member.phase is Tier3WorkerPhase.QUARANTINED:
                        member.phase = Tier3WorkerPhase.RETIRING
                    continue
                if member.phase is Tier3WorkerPhase.IDLE and member.command is None:
                    idle_released += 1
                member.phase = Tier3WorkerPhase.RETIRING
                member.reservation_id = 0
                member.replacement_reason = reason
                member.retirement_started_at = group.retirement_started_at
                member.retirement_terminal_kind = terminal_kind
            if record_failure:
                self._last_failure_reason = str(reason)
                self._record_failure_reason(reason)
            self._group_retirement_count += 1
            _count_reason(self._group_retirement_reasons, reason)
        for _ in range(idle_released):
            self.capacity_sink(-1)
        try:
            termination_job = group.job.duplicate()
        except OSError as exc:
            termination_job = None
            self._log("error", "Tier3 validator Job handle duplication failed: %s", exc)
        if termination_job is not None:
            self._forced_job_termination_count += 1
            _terminate_jobs_bounded(
                [termination_job], exit_code=74, timeout_seconds=0.05, close_after=True,
            )
        self._reap_retiring_workers()

    def _reap_retiring_workers(self) -> None:
        now = time.monotonic()
        with self._lock:
            retiring = [
                group
                for group in self._groups.values()
                if group.retiring and group.retirement_started_at > 0.0
            ]
        for group in retiring:
            try:
                snapshot = group.job.snapshot()
                empty = snapshot.active_processes <= 0 and not snapshot.process_ids
            except OSError:
                empty = False
            if empty:
                self._finalize_group(group, job_empty=True)
                continue
            if now - group.retirement_started_at >= self.job_empty_wait_seconds:
                self._finalize_group(group, job_empty=False)

    def _remove_group(self, group: _HelperGroup) -> None:
        self._pipe_wait.release(group.connection)
        with self._lock:
            for member in group.members:
                if self._workers.get(member.worker_id) is member:
                    self._workers.pop(member.worker_id, None)
            if self._groups.get(group.index) is group:
                self._groups.pop(group.index, None)
            self._leased_ports.discard(int(group.mixed_port or 0))
            self._leased_ports.discard(int(group.controller_port or 0))
            for port in group.listener_ports:
                self._leased_ports.discard(int(port or 0))
        try:
            group.connection.close()
        except OSError:
            pass
        try:
            group.process.join(timeout=0.1)
        except (AssertionError, ValueError):
            pass
        group.job.close()
        _close_stopped_process(group.process)

    def _emit_group_gone(self, group: _HelperGroup, *, job_empty: bool, released: bool) -> None:
        if self.generation_sink is None or group.lead is None:
            return
        try:
            self.generation_sink(
                GenerationGone(
                    worker_id=group.lead.worker_id,
                    worker_generation=group.generation,
                    helper_pid=group.lead.helper_pid,
                    mihomo_pid=group.lead.mihomo_pid,
                    reason=group.replacement_reason or "worker_retired",
                    job_empty=job_empty,
                    ports_released=released,
                )
            )
        except Exception:
            self._log("exception", "Tier3 generation sink failed")

    def _finalize_group(self, group: _HelperGroup, *, job_empty: bool) -> None:
        with self._lock:
            if self._groups.get(group.index) is not group or not group.retiring:
                return
            terminal_kind = group.retirement_terminal_kind
            replace_group = group.retirement_replace
            record_failure = group.retirement_record_failure
            was_ready = group.retirement_was_ready
            reason = group.replacement_reason or "worker_retired"
            members = list(group.members)
        released = ports_released(
            (group.mixed_port, group.controller_port, *group.listener_ports)
        )
        if not job_empty:
            for member in members:
                if terminal_kind is not None and member.command is not None:
                    self._emit_lost_result(member, terminal_kind, reason, clean_confirmed=False)
            self._emit_group_gone(group, job_empty=False, released=released)
            with self._lock:
                if self._groups.get(group.index) is not group:
                    return
                for member in members:
                    member.phase = Tier3WorkerPhase.QUARANTINED
                    member.command = None
                    self._disabled_workers[member.worker_id] = str(reason)
                self._disabled_groups[group.index] = str(reason)
                # Finalized: never reap this group again.
                group.retirement_started_at = 0.0
                self._group_hard_stall_check_unlocked()
            return
        try:
            group.process.join(timeout=0.0)
        except (AssertionError, ValueError):
            pass
        self._remove_group(group)
        for member in members:
            if terminal_kind is not None and member.command is not None:
                self._emit_lost_result(member, terminal_kind, reason, clean_confirmed=True)
        self._emit_group_gone(group, job_empty=True, released=released)
        failure_streak = int(self._group_failure_streak.get(group.index, 0))
        if record_failure:
            failure_streak += 1
            self._group_failure_streak[group.index] = failure_streak
            if not was_ready:
                self._startup_failure_count += 1
        else:
            self._group_failure_streak[group.index] = 0
        if replace_group and failure_streak >= 3:
            self._disabled_groups[group.index] = str(reason)
            for member in members:
                self._disabled_workers[member.worker_id] = str(reason)
            replace_group = False
        if replace_group and not self._stopping.is_set():
            self._replacement_count += 1
            for member in members:
                self._disabled_workers.pop(member.worker_id, None)
        with self._lock:
            self._group_hard_stall_check_unlocked()

    def duplicate_job_handles(
        self, timeout_seconds: float | None = None,
    ) -> dict[tuple[int, int], WindowsJob]:
        # Every group still registered is duplicated, whatever its members'
        # phase: a group stays registered only while its Job may hold a
        # process (a quarantined group's Job did not empty), and that is the
        # Job the caller must be able to terminate and read back.
        duplicates: dict[tuple[int, int], WindowsJob] = {}
        self._caller_job_keys = frozenset()
        if timeout_seconds is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=max(0.0, float(timeout_seconds)))
        if not acquired:
            return duplicates
        try:
            for group in self._groups.values():
                try:
                    duplicates[_group_job_key(group)] = group.job.duplicate()
                except OSError:
                    continue
        finally:
            self._lock.release()
        self._caller_job_keys = frozenset(duplicates)
        return duplicates

    # ------------------------------------------------------------ reactor

    def _check_deadlines(self) -> None:
        # Once stop is signalled every deadline belongs to _stop_all: no
        # cancel, no retirement and no per-member drain of a shared Pipe.
        if self._stopping.is_set():
            return
        super()._check_deadlines()

    def _run(self) -> None:
        self._running.set()
        self._stop_status = "RUNNING"
        try:
            while not self._stopping.is_set():
                self._wake.clear()
                self._process_controls()
                self._reap_retiring_workers()
                self._assign_pending()
                self._start_needed_workers()
                self._recycle_groups()
                self._maybe_log_metrics()
                with self._lock:
                    groups = list(self._groups.values())
                waitables: list[Any] = []
                owner: dict[Any, _HelperGroup] = {}
                for group in groups:
                    if group.lead is None:
                        continue
                    if not group.connection.closed:
                        waitables.append(group.connection)
                        owner[group.connection] = group
                    sentinel = _process_sentinel(group.process)
                    if sentinel is not None:
                        waitables.append(sentinel)
                        owner[sentinel] = group
                if waitables:
                    ready = self._pipe_wait.wait(waitables, timeout=0.05)
                else:
                    self._wake.wait(0.05)
                    ready = []
                # A caller's stop can arrive at any point of an iteration; the
                # owner is the only thread that can reap the groups inside the
                # caller's deadline, so it leaves the loop at the next step
                # instead of finishing a sweep of K identities per group.
                for item in ready:
                    if self._stopping.is_set():
                        break
                    group = owner.get(item)
                    if group is None or item is not group.connection or group.lead is None:
                        continue
                    self._drain_worker_pipe(group.lead, first_message_ready=True)
                for item in ready:
                    if self._stopping.is_set():
                        break
                    group = owner.get(item)
                    if group is None or item is group.connection:
                        continue
                    with self._lock:
                        current = self._groups.get(group.index)
                    if current is group and not _process_is_alive(group.process):
                        self._retire_group(
                            group,
                            f"helper_exit:{group.process.exitcode}",
                            terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                        )
                self._check_deadlines()
        finally:
            try:
                self._pipe_wait.close()
            finally:
                self._stop_all()
                self._running.clear()

    # --------------------------------------------------------------- stop

    def _publish_stop_stage(self, stage: str) -> None:
        trace = getattr(self, "_stop_trace", None)
        if trace is not None and len(trace) < _STOP_TRACE_LIMIT:
            trace.append((time.monotonic(), str(stage)))
        super()._publish_stop_stage(stage)

    def stop(self, timeout_seconds: float = 3.0) -> str:
        started = time.monotonic()
        self._stop_trace = []
        status = super().stop(timeout_seconds)
        uncovered = sorted(self._stop_failed_job_keys - self._caller_job_keys)
        if status == "STOPPED" and uncovered:
            # The inherited caller turned the owner's failed verdict into
            # STOPPED from the Jobs it duplicated at entry.  A Job the owner
            # found holding a process that the caller never held (a group
            # registered after the duplicate, or one whose duplicate failed)
            # was not part of that readback: the owner's verdict stands.
            status = "STOP_FAILED_QUARANTINED"
            self._stop_status = status
            self._publish_stop_stage(
                "caller_conversion_refused:uncovered="
                + ",".join(f"w{worker}.{generation}" for worker, generation in uncovered)
            )
        if status == "STOP_FAILED_QUARANTINED" and not self._stop_failure_logged:
            self._stop_failure_logged = True
            self._log_stop_failure(started, status)
        return status

    def _log_stop_failure(self, started: float, status: str) -> None:
        """One bounded line with every recorded stop stage, for a failed stop.

        The stages otherwise live only in the domain snapshot and the close
        report, and a failed stop is exactly when neither is persisted.
        ``owner_alive=1`` means the owner had not returned by the caller's
        deadline; the stages it had reached show where it was.
        """

        thread = self._thread
        owner_alive = bool(thread is not None and thread.is_alive())
        stages = " ".join(
            f"{stage}@{int(round((stamp - started) * 1000))}ms"
            for stamp, stage in list(self._stop_trace)
        )
        if len(stages) > _STOP_LOG_MAX_CHARS:
            stages = stages[:_STOP_LOG_MAX_CHARS] + "..."
        self._log(
            "warning",
            "Tier3 batch validator stop %s (slots_per_process=%s owner_alive=%s): %s",
            status,
            self.slots_per_process,
            int(owner_alive),
            stages or "no stages recorded",
        )

    def _stop_all(self) -> None:  # noqa: C901
        """Reap every group inside the caller's deadline and judge it physically.

        Same boundary as the base owner stop (stop frame, a short cooperative
        window, Job termination, Job and process evidence), with the verdict
        made robust for groups of K identities:

        * every wait ends ``_OWNER_STOP_MARGIN_SECONDS`` before the caller's
          deadline, so the owner has returned when the caller's join ends and a
          late owner does as much as its budget allows instead of overrunning;
        * each group's own Job handle stays open until the verdict and is
          sampled together with the process wait, the last sample taken after
          the last wait; a group is never judged on a sample it did not get
          (at least one is taken even with no budget left), and a Job that
          empties during the wait counts as empty.  A Job still holding a
          process at the last sample is a failed stop;
        * Jobs are terminated inline.  TerminateJobObject does not wait for the
          processes, while a terminator thread per Job costs a thread start
          each, which a GIL-starved parent pays in hundreds of milliseconds.
        """

        self._publish_stop_stage("owner_entered")
        self._pipe_wait.close()
        caller_deadline = self._stop_deadline_at or (
            time.monotonic() + self.job_empty_wait_seconds + 1.0
        )
        deadline = caller_deadline - _OWNER_STOP_MARGIN_SECONDS

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        # Running helpers hear "stop" before anything waits on the starter; its
        # threads are joined (bounded) before the verdict.
        self._starter.stop(0.0)
        self._drain_spawn_results(discard=True)
        with self._lock:
            self._pending_spawns.clear()
            groups = list(self._groups.values())
        for group in groups:
            for member in group.members:
                member.stop_requested = True
            if group.lead is not None and group.lead.phase is not Tier3WorkerPhase.WAITING_START:
                self._send(group.lead, "stop", {})
        self._publish_stop_stage(f"owner_stop_sent:{len(groups)}")
        cooperative_deadline = time.monotonic() + min(0.5, remaining())
        # Helpers still running.  A process is dropped when its sentinel fires
        # (never polled per pass), and the deadline is checked between frames:
        # in a GIL-starved parent every native call costs a thread switch, and
        # a pass over K groups would otherwise overrun the window many times.
        running = {
            group.index: group for group in groups if _process_is_alive(group.process)
        }
        pipe_closed: set[int] = set()
        while running and time.monotonic() < cooperative_deadline:
            waitables: list[Any] = []
            waitable_owner: dict[Any, tuple[_HelperGroup, str]] = {}
            for group in list(running.values()):
                sentinel = _process_sentinel(group.process)
                if sentinel is None:
                    running.pop(group.index, None)
                    continue
                waitables.append(sentinel)
                waitable_owner[sentinel] = (group, "sentinel")
                if group.index not in pipe_closed and not group.connection.closed:
                    waitables.append(group.connection)
                    waitable_owner[group.connection] = (group, "connection")
            if not waitables:
                break
            ready = multiprocessing.connection.wait(
                waitables,
                timeout=min(0.05, max(0.0, cooperative_deadline - time.monotonic())),
            )
            owners = [waitable_owner[item] for item in ready if item in waitable_owner]
            for group, kind in owners:
                if kind == "sentinel":
                    running.pop(group.index, None)
            for group, kind in owners:
                if kind != "connection" or group.lead is None or group.index not in running:
                    continue
                if time.monotonic() >= cooperative_deadline:
                    break
                self._handle_pipe(group.lead)
                if group.lead.replacement_reason == "stop_pipe_closed":
                    # EOF stays readable: waiting on it again would spin
                    # until the helper process is gone.
                    pipe_closed.add(group.index)

        with self._lock:
            remaining_groups = list(self._groups.values())
            for group in remaining_groups:
                group.retiring = True
                group.replacement_reason = group.replacement_reason or "supervisor_stop"
                for member in group.members:
                    member.phase = Tier3WorkerPhase.RETIRING
                    member.replacement_reason = member.replacement_reason or "supervisor_stop"
        self._publish_stop_stage(f"owner_cooperative_complete:{len(remaining_groups)}")

        jobs_to_terminate: list[_HelperGroup] = []
        for group in remaining_groups:
            try:
                snapshot = group.job.snapshot()
            except OSError:
                snapshot = None
            if (
                _process_is_alive(group.process)
                or snapshot is None
                or snapshot.active_processes > 0
                or bool(snapshot.process_ids)
            ):
                jobs_to_terminate.append(group)
        self._forced_job_termination_count += len(jobs_to_terminate)
        self._publish_stop_stage(f"owner_job_termination_started:{len(jobs_to_terminate)}")
        terminated = 0
        for group in jobs_to_terminate:
            try:
                group.job.terminate(74)
                terminated += 1
            except OSError:
                pass
        self._publish_stop_stage(
            "owner_job_termination_completed"
            if terminated == len(jobs_to_terminate)
            else f"owner_job_termination_incomplete:{terminated}/{len(jobs_to_terminate)}"
        )

        job_empty: dict[int, bool] = {}

        def job_is_empty(group: _HelperGroup) -> bool:
            # Empty is final: nothing can join a Job that holds no process
            # once the starter is stopped.
            if job_empty.get(group.index):
                return True
            try:
                snapshot = group.job.snapshot()
                empty = snapshot.active_processes <= 0 and not snapshot.process_ids
            except OSError:
                empty = False
            job_empty[group.index] = empty
            return empty

        self._publish_stop_stage(f"owner_confirm_budget_ms:{int(remaining() * 1000)}")
        direct_killed: set[int] = set()
        helper_gone: set[int] = set()
        while True:
            unconfirmed = False
            sentinels: dict[Any, _HelperGroup] = {}
            for group in remaining_groups:
                empty = job_is_empty(group)
                alive = group.index not in helper_gone and _process_is_alive(group.process)
                if not alive:
                    helper_gone.add(group.index)
                if empty and alive and group.index not in direct_killed:
                    # The Job is empty but the helper's handle still reads
                    # alive: terminate that exact process as a second,
                    # independent boundary (as the base owner stop does).
                    direct_killed.add(group.index)
                    try:
                        group.process.kill()
                    except (OSError, AssertionError, ValueError):
                        pass
                if alive:
                    sentinel = _process_sentinel(group.process)
                    if sentinel is not None:
                        sentinels[sentinel] = group
                unconfirmed = unconfirmed or alive or not empty
            if not unconfirmed:
                break
            wait_seconds = remaining()
            if wait_seconds <= 0.0:
                break
            if sentinels:
                for sentinel in multiprocessing.connection.wait(
                    list(sentinels), timeout=min(0.025, wait_seconds),
                ):
                    try:
                        sentinels[sentinel].process.join(timeout=0.0)
                    except (AssertionError, ValueError):
                        pass
            else:
                time.sleep(min(0.01, wait_seconds))
        if direct_killed:
            self._forced_job_termination_count += len(direct_killed)
        self._publish_stop_stage(
            "owner_job_verification:"
            f"{sum(1 for group in remaining_groups if job_empty.get(group.index))}/"
            f"{len(remaining_groups)}"
        )
        self._publish_stop_stage(f"owner_direct_process_kill:{len(direct_killed)}")

        for group in remaining_groups:
            for member in group.members:
                member.command = None
            self._pipe_wait.release(group.connection)
            with self._lock:
                for member in group.members:
                    if self._workers.get(member.worker_id) is member:
                        self._workers.pop(member.worker_id, None)
                if self._groups.get(group.index) is group:
                    self._groups.pop(group.index, None)
                self._leased_ports.discard(int(group.mixed_port or 0))
                self._leased_ports.discard(int(group.controller_port or 0))
                for port in group.listener_ports:
                    self._leased_ports.discard(int(port or 0))
            try:
                group.connection.close()
            except OSError:
                pass
            try:
                group.process.join(timeout=0.0)
            except (AssertionError, ValueError):
                pass
            # Only now: closing the last handle of a KILL_ON_JOB_CLOSE Job is
            # itself a kill, and would leave nothing to sample.
            group.job.close()
        self._publish_stop_stage(f"owner_registry_cleared:{len(remaining_groups)}")

        job_failed = False
        process_handle_lag_count = 0
        alive_process_evidence: list[str] = []
        live_jobs: list[str] = []
        failed_job_keys: set[tuple[int, int]] = set()
        for group in remaining_groups:
            alive = _process_is_alive(group.process)
            if alive:
                process_handle_lag_count += 1
                alive_process_evidence.append(
                    f"g{group.index}.{group.generation}:"
                    f"{int(group.process.pid or 0)}:{group.process.exitcode}"
                )
            if not job_empty.get(group.index, False):
                job_failed = True
                live_jobs.append(f"g{group.index}.{group.generation}")
                failed_job_keys.add(_group_job_key(group))
            if not alive:
                _close_stopped_process(group.process)
        self._stop_failed_job_keys = frozenset(failed_job_keys)
        self._publish_stop_stage(
            "owner_final_evidence:"
            f"job_failed={int(job_failed)}:"
            f"process_handle_lag={process_handle_lag_count}:"
            f"alive={','.join(alive_process_evidence) or 'none'}:"
            f"live_jobs={','.join(live_jobs) or 'none'}"
        )
        # The starter was told to stop at entry; its idle threads only need to
        # be scheduled once, so even an owner with no budget left gives them a
        # moment (inside the margin before the caller's deadline).
        self._starter.join(max(0.05, remaining()))
        self._drain_spawn_results(discard=True)
        starter_stopped = self._starter.is_quiescent()
        self._stop_unconfirmed_processes = tuple(
            group.process for group in remaining_groups if _process_is_alive(group.process)
        )
        self._publish_stop_stage(f"owner_starter_stopped:{int(starter_stopped)}")
        self._stop_status = (
            "STOP_FAILED_QUARANTINED"
            if job_failed or self._stop_unconfirmed_processes or not starter_stopped
            else "STOPPED"
        )
        self._publish_stop_stage(f"owner_return:{self._stop_status}")


__all__ = [
    "Tier3BatchValidatorSupervisor",
]
