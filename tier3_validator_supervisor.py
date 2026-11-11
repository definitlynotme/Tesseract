from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from tier3_defaults import tier3_process_worker_ceiling
from tier3_pipe_wait import Tier3PipeWaitSet, receive_ready_pipe_message
from tier3_validation_config import build_validator_prewarm_config
from tier3_validation_protocol import (
    CONFIRM_HTTPS_ATTEMPTS_PER_URL,
    CancelCmd,
    EndpointAttemptKind,
    EndpointAttemptResult,
    GenerationGone,
    ProbeAttemptFact,
    TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS,
    TIER3_HELPER_START_TIMEOUT_SECONDS,
    Tier3TerminalKind,
    Tier3ValidatorCapacityLease,
    Tier3WorkerPhase,
    ValidateEndpointCmd,
    dataclass_payload,
    endpoint_attempt_result_from_payload,
    is_confirm_https_fact,
    message_bytes,
    parse_message,
)
from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE, helper_process_bootstrap
from tier3_windows_job import (
    WindowsJob,
    ports_released,
    process_resource_snapshot,
    system_resource_snapshot,
)

if os.name == "nt":
    import _winapi


def _pipe_has_queued_message(connection: Any) -> bool:
    if os.name == "nt" and isinstance(
        connection, multiprocessing.connection.PipeConnection
    ):
        # poll(0) on an empty Windows Pipe creates and cancels an overlapped
        # read. The owner already waits on all Pipes once; this sweep only
        # checks buffered messages and must not repeat that wait per helper.
        if connection.closed or not connection.readable:
            raise OSError("validator Pipe is not readable")
        return bool(
            connection._got_empty_message
            or _winapi.PeekNamedPipe(connection.fileno())[0]
        )
    return bool(connection.poll(0.0))


@dataclass(slots=True)
class _WorkerGeneration:
    worker_id: int
    generation: int
    process: multiprocessing.Process
    connection: multiprocessing.connection.Connection
    job: WindowsJob
    phase: Tier3WorkerPhase = Tier3WorkerPhase.WAITING_START
    helper_pid: int = 0
    mihomo_pid: int = 0
    mixed_port: int = 0
    controller_port: int = 0
    started_at: float = field(default_factory=time.monotonic)
    ready_at: float = 0.0
    heartbeat_at: float = field(default_factory=time.monotonic)
    command: ValidateEndpointCmd | None = None
    deadline_at: float = 0.0
    lease_hard_deadline_at: float = 0.0
    deadline_phase: str = ""
    transport_started_at: float = 0.0
    cancel_deadline_at: float = 0.0
    sealed_lease_id: str = ""
    cancel_reason: str = ""
    cancel_requested_monotonic_ns: int = 0
    stop_requested: bool = False
    replacement_reason: str = ""
    retirement_started_at: float = 0.0
    retirement_terminal_kind: Tier3TerminalKind | None = None
    retirement_replace_worker: bool = True
    retirement_record_failure: bool = True
    retirement_was_ready: bool = False
    reservation_id: int = 0
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    bootstrap_ready: bool = False
    prewarm_started: bool = False
    prewarm_started_at: float = 0.0
    prewarm_pending_pid: int = 0
    prewarm_check_at: float = 0.0
    startup_phase: str = ""
    startup_deadline_at: float = 0.0


@dataclass(frozen=True, slots=True)
class _WorkerSpawnResult:
    worker_id: int
    generation: int
    worker: _WorkerGeneration | None = None
    error: BaseException | None = None


def _close_stopped_process(process: multiprocessing.Process) -> bool:
    """Release the parent's process handle once the child is confirmed gone."""

    try:
        if process.is_alive():
            return False
    except ValueError:
        return True
    try:
        if process.pid is not None:
            process.join(timeout=0.0)
        process.close()
    except (AssertionError, OSError, ValueError):
        return False
    return True


def _process_is_alive(process: multiprocessing.Process) -> bool:
    """Read child liveness without reopening an already closed handle."""

    try:
        return bool(process.is_alive())
    except (AssertionError, OSError, ValueError):
        return False


def _process_sentinel(process: multiprocessing.Process) -> Any | None:
    """Return one stable wait handle, or none after the Process was closed."""

    try:
        return process.sentinel
    except (AssertionError, OSError, ValueError):
        return None


def _reserve_unique_loopback_port(excluded: set[int]) -> int:
    """Choose a currently free port that is not leased to another generation."""

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
    raise RuntimeError("unable to reserve a unique Tier3 helper port")


class Tier3WorkerStarter:
    """Starts helper shells without blocking the supervisor Pipe reactor."""

    def __init__(
        self,
        *,
        context: multiprocessing.context.BaseContext,
        helper_target: Callable[[Any], None],
        job_empty_wait_seconds: float,
        wake: Callable[[], None],
        max_concurrent: int = 8,
    ) -> None:
        self._context = context
        self._helper_target = helper_target
        self._job_empty_wait_seconds = max(0.1, float(job_empty_wait_seconds))
        self._wake = wake
        self._requests: queue.Queue[tuple[int, int] | None] = queue.Queue()
        self._results: queue.Queue[_WorkerSpawnResult] = queue.Queue()
        self._pending: set[tuple[int, int]] = set()
        self._unreaped_processes: list[multiprocessing.Process] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"tier3-validator-starter-{index + 1}",
                daemon=True,
            )
            for index in range(max(1, min(8, int(max_concurrent))))
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def submit(self, worker_id: int, generation: int) -> bool:
        key = (int(worker_id), int(generation))
        with self._lock:
            if self._stopping.is_set() or key in self._pending:
                return False
            self._pending.add(key)
        self._requests.put_nowait(key)
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def drain_results(self) -> list[_WorkerSpawnResult]:
        results: list[_WorkerSpawnResult] = []
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._pending.discard((result.worker_id, result.generation))
            results.append(result)
        return results

    def stop(self, timeout_seconds: float) -> None:
        self._stopping.set()
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            if request is not None:
                with self._lock:
                    self._pending.discard(request)
        for _ in self._threads:
            self._requests.put_nowait(None)
        self.join(timeout_seconds)

    def join(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def is_quiescent(self) -> bool:
        if any(thread.is_alive() for thread in self._threads):
            return False
        with self._lock:
            self._unreaped_processes = [
                process for process in self._unreaped_processes
                if not _close_stopped_process(process)
            ]
            return not self._pending and not self._unreaped_processes

    def _dispose_worker(self, worker: _WorkerGeneration) -> None:
        try:
            worker.job.terminate(74)
        except OSError:
            pass
        try:
            worker.job.wait_empty(self._job_empty_wait_seconds)
        except OSError:
            pass
        try:
            worker.connection.close()
        except OSError:
            pass
        try:
            worker.process.join(timeout=0.1)
        except (AssertionError, ValueError):
            pass
        if _process_is_alive(worker.process):
            try:
                worker.process.kill()
                worker.process.join(timeout=0.25)
            except (OSError, AssertionError, ValueError):
                pass
        worker.job.close()
        if not _close_stopped_process(worker.process):
            with self._lock:
                self._unreaped_processes.append(worker.process)

    def _spawn(self, worker_id: int, generation: int) -> _WorkerGeneration:
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self._helper_target,
            args=(child,),
            name=f"tier3-validator-helper-{worker_id}-{generation}",
            daemon=False,
        )
        job = WindowsJob()
        try:
            process.start()
            child.close()
            job.assign_pid(int(process.pid))
            return _WorkerGeneration(
                worker_id=worker_id,
                generation=generation,
                process=process,
                connection=parent,
                job=job,
                helper_pid=int(process.pid or 0),
            )
        except BaseException:
            try:
                child.close()
            except OSError:
                pass
            try:
                parent.close()
            except OSError:
                pass
            try:
                job.terminate(74)
            except OSError:
                pass
            if process.pid and _process_is_alive(process):
                try:
                    process.kill()
                    process.join(timeout=0.25)
                except (OSError, AssertionError, ValueError):
                    pass
            job.close()
            if not _close_stopped_process(process):
                with self._lock:
                    self._unreaped_processes.append(process)
            raise

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            worker_id, generation = request
            worker: _WorkerGeneration | None = None
            error: BaseException | None = None
            try:
                if self._stopping.is_set():
                    continue
                worker = self._spawn(worker_id, generation)
                if self._stopping.is_set():
                    self._dispose_worker(worker)
                    worker = None
            except BaseException as exc:
                error = exc
            finally:
                if worker is not None or error is not None:
                    self._results.put_nowait(
                        _WorkerSpawnResult(
                            worker_id=worker_id,
                            generation=generation,
                            worker=worker,
                            error=error,
                        )
                    )
                    self._wake()
                elif self._stopping.is_set():
                    with self._lock:
                        self._pending.discard((worker_id, generation))


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    desired_workers: int
    shell_prewarm_workers: int
    prewarmed_waiting_workers: int
    startup_bulkhead: int
    mihomo_start_bulkhead: int
    pending_spawn_workers: int
    startup_pipeline_workers: int
    active_workers: int
    idle_workers: int
    reserved_workers: int
    waiting_start_workers: int
    validating_workers: int
    starting_workers: int
    retiring_workers: int
    quarantined_workers: int
    pending_commands: int
    capacity_reservation_rejected_count: int
    direct_dispatch_count: int
    assignment_to_send_sample_count: int
    assignment_to_send_total_ms: float
    assignment_to_send_max_ms: float
    send_to_helper_sample_count: int
    send_to_helper_total_ms: float
    send_to_helper_max_ms: float
    completion_count: int
    endpoint_result_count: int
    facility_failure_count: int
    replacement_count: int
    forced_job_termination_count: int
    last_completion_monotonic: float
    stop_status: str
    hard_stall: bool
    disabled_worker_count: int
    startup_failure_count: int
    last_failure_reason: str
    failure_reason_counts: tuple[tuple[str, int], ...]
    private_bytes: int
    handle_count: int
    system_commit_headroom_bytes: int
    resource_headroom_limited: bool
    workers: tuple[dict[str, Any], ...]


def _terminate_jobs_bounded(
    jobs: list[WindowsJob],
    *,
    exit_code: int,
    timeout_seconds: float,
    close_after: bool,
) -> bool:
    """Issue TerminateJobObject concurrently under one shared deadline."""

    if not jobs:
        return True
    completed = [threading.Event() for _ in jobs]

    def terminate(index: int, job: WindowsJob) -> None:
        try:
            job.terminate(exit_code)
        except OSError:
            pass
        finally:
            if close_after:
                job.close()
            completed[index].set()

    threads = [
        threading.Thread(
            target=terminate,
            args=(index, job),
            name=f"tier3-job-terminator-{index + 1}",
            daemon=True,
        )
        for index, job in enumerate(jobs)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return all(event.is_set() for event in completed)


class Tier3ValidatorSupervisor:
    """Owns helper processes, generation Jobs, Pipes and physical retirement."""

    def __init__(
        self,
        *,
        binary_path: str,
        probe_urls: list[str],
        expected_statuses: set[int],
        probe_timeout_seconds: float,
        slow_threshold_ms: int,
        capacity_sink: Callable[[int], None],
        attempt_sink: Callable[[EndpointAttemptResult], None],
        generation_sink: Callable[[GenerationGone], None] | None = None,
        logger: Any | None = None,
        min_workers: int = 4,
        max_workers: int = 16,
        temp_parent: str = "",
        ipv6: bool = False,
        cleanup_seconds: float = 1.5,
        job_empty_wait_seconds: float = 1.0,
        start_timeout_seconds: float = TIER3_HELPER_START_TIMEOUT_SECONDS,
        startup_bulkhead: int = 8,
        mihomo_start_bulkhead: int = 8,
        helper_target: Callable[[Any], None] = helper_process_bootstrap,
        confirm_urls: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.binary_path = str(binary_path)
        self.probe_urls = tuple(str(item) for item in probe_urls if str(item))
        # Mainstream HTTPS confirm URLs carried to every helper in START.
        self.confirm_urls = tuple(str(item) for item in confirm_urls if str(item))
        self.expected_statuses = frozenset(int(item) for item in expected_statuses)
        self.probe_timeout_seconds = max(0.25, float(probe_timeout_seconds))
        self.slow_threshold_ms = max(1, int(slow_threshold_ms))
        self.attempt_sink = attempt_sink
        self.capacity_sink = capacity_sink
        self.generation_sink = generation_sink
        self.logger = logger
        self.min_workers = max(1, min(tier3_process_worker_ceiling(), int(min_workers)))
        self.max_workers = max(
            self.min_workers,
            min(tier3_process_worker_ceiling(), int(max_workers)),
        )
        self.temp_parent = str(temp_parent or "")
        self.ipv6 = bool(ipv6)
        self.cleanup_seconds = max(0.1, float(cleanup_seconds))
        self.job_empty_wait_seconds = max(0.1, float(job_empty_wait_seconds))
        self.start_timeout_seconds = max(1.0, float(start_timeout_seconds))
        self.startup_bulkhead = max(
            1,
            min(8, self.max_workers, int(startup_bulkhead)),
        )
        # Process.start()/Job assignment and Mihomo prewarm are separate
        # facilities with independent bounded concurrency. The supervisor
        # remains the only Pipe reactor for both cohorts.
        self.mihomo_start_bulkhead = max(
            1,
            min(8, self.max_workers, int(mihomo_start_bulkhead)),
        )
        self._helper_target = helper_target

        self._context = multiprocessing.get_context("spawn")
        self._commands: queue.Queue[ValidateEndpointCmd] = queue.Queue()
        self._control: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._workers: dict[int, _WorkerGeneration] = {}
        self._stop_unconfirmed_processes: tuple[multiprocessing.Process, ...] = ()
        # A helper used to reserve and immediately release its own two ports.
        # Concurrent prewarm could then assign the same endpoint to two
        # generations and route a mixed-port probe to another controller.
        # The supervisor owns uniqueness for the complete generation lifetime.
        self._leased_ports: set[int] = set()
        self._next_generation: dict[int, int] = {}
        self._desired_workers = self.min_workers
        self._shell_prewarm_workers = self.min_workers
        self._startup_cohort_ready = False
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._pipe_wait = Tier3PipeWaitSet()
        self._pending_spawns: dict[int, int] = {}
        self._starter = Tier3WorkerStarter(
            context=self._context,
            helper_target=self._helper_target,
            job_empty_wait_seconds=self.job_empty_wait_seconds,
            wake=self._wake.set,
            max_concurrent=self.startup_bulkhead,
        )
        self._completion_count = 0
        self._endpoint_result_count = 0
        self._facility_failure_count = 0
        self._replacement_count = 0
        self._forced_job_termination_count = 0
        self._last_completion_at = time.monotonic()
        self._stop_status = "not_started"
        self._hard_stall = False
        self._disabled_workers: dict[int, str] = {}
        self._worker_failure_streak: dict[int, int] = {}
        self._spawn_retry_at: dict[int, float] = {}
        self._startup_failure_count = 0
        self._last_failure_reason = ""
        self._failure_reason_counts: dict[str, int] = {}
        self._capacity_reservation_sequence = 0
        self._capacity_reservation_rejected_count = 0
        self._direct_dispatch_count = 0
        self._assignment_to_send_sample_count = 0
        self._assignment_to_send_total_ms = 0.0
        self._assignment_to_send_max_ms = 0.0
        self._send_to_helper_sample_count = 0
        self._send_to_helper_total_ms = 0.0
        self._send_to_helper_max_ms = 0.0
        self._private_bytes = 0
        self._handle_count = 0
        self._system_commit_headroom_bytes = 0
        self._resource_headroom_limited = False
        self._last_resource_sample_at = 0.0
        self._stop_deadline_at = 0.0
        self.stop_stage_observer: Callable[[str], None] | None = None

    def _publish_stop_stage(self, stage: str) -> None:
        observer = self.stop_stage_observer
        if observer is None:
            return
        try:
            observer(str(stage))
        except Exception:
            # Lifecycle diagnostics must never become a shutdown dependency.
            pass

    def start(self) -> None:
        if self._thread is not None:
            return
        if (
            self._helper_target is helper_process_bootstrap
            and (not self.binary_path or not Path(self.binary_path).is_file())
        ):
            raise FileNotFoundError(
                f"Mihomo binary not found for validator supervisor: {self.binary_path}"
            )
        # Establish headroom before workers exist. Ongoing per-process sampling
        # belongs to the domain resource reporter, not the Pipe owner thread.
        self.refresh_resource_metrics()
        self._starter.start()
        self._thread = threading.Thread(
            target=self._run,
            name="tier3-validator-supervisor",
            daemon=False,
        )
        self._thread.start()
        if not self._running.wait(2.0):
            raise RuntimeError("validator supervisor did not start")

    def reserve_capacity(self) -> Tier3ValidatorCapacityLease | None:
        if self._stopping.is_set():
            return None
        with self._lock:
            worker = next(
                (
                    item
                    for item in sorted(
                        self._workers.values(), key=lambda current: current.worker_id
                    )
                    if item.phase is Tier3WorkerPhase.IDLE
                    and item.command is None
                    and item.reservation_id <= 0
                ),
                None,
            )
            if worker is None:
                self._capacity_reservation_rejected_count += 1
                return None
            self._capacity_reservation_sequence += 1
            worker.reservation_id = self._capacity_reservation_sequence
            worker.phase = Tier3WorkerPhase.RESERVED
            return Tier3ValidatorCapacityLease(
                worker_id=worker.worker_id,
                worker_generation=worker.generation,
                reservation_id=worker.reservation_id,
            )

    def release_capacity(self, lease: Tier3ValidatorCapacityLease) -> None:
        released = False
        with self._lock:
            worker = self._workers.get(int(lease.worker_id))
            if (
                worker is not None
                and worker.generation == int(lease.worker_generation)
                and worker.phase is Tier3WorkerPhase.RESERVED
                and worker.command is None
                and worker.reservation_id == int(lease.reservation_id)
            ):
                worker.reservation_id = 0
                worker.phase = Tier3WorkerPhase.IDLE
                released = True
        if released:
            self.capacity_sink(1)

    def submit(self, command: ValidateEndpointCmd) -> bool:
        if self._stopping.is_set():
            raise RuntimeError("validator supervisor is stopping")
        if int(command.worker_id) <= 0 or int(command.worker_generation) <= 0:
            # Offline parity and focused supervisor diagnostics deliberately
            # submit an unbound batch. Production Actor commands always carry
            # a generation-bound capacity lease.
            self._commands.put_nowait(command)
            self._wake.set()
            return True
        assigned_at = time.monotonic_ns()
        assigned: ValidateEndpointCmd | None = None
        with self._lock:
            worker = self._workers.get(int(command.worker_id))
            valid = not (
                worker is None
                or worker.generation != int(command.worker_generation)
                or worker.phase is not Tier3WorkerPhase.RESERVED
                or worker.command is not None
                or worker.reservation_id <= 0
                or worker.reservation_id
                != int(command.capacity_reservation_id)
            )
            if not valid:
                self._capacity_reservation_rejected_count += 1
            else:
                assigned = replace(
                    command,
                    supervisor_assigned_monotonic_ns=assigned_at,
                )
                worker.command = assigned
        if assigned is None:
            return False
        # Production commands already own an exact generation-bound idle
        # helper.  Sending them through the global fallback queue makes the
        # actor's lease depend on when the sole Pipe reader next gets CPU.  A
        # large source parse/report burst can delay that hand-off by seconds
        # even though the selected helper is idle.  Deliver the command now;
        # unbound parity/diagnostic commands continue to use _commands.
        accepted = self._dispatch_reserved_command(
            worker,
            assigned,
            direct=True,
        )
        self._wake.set()
        return accepted

    def set_desired_workers(self, count: int) -> None:
        desired = max(self.min_workers, min(self.max_workers, int(count)))
        self._control.put_nowait(("desired", desired))
        self._wake.set()

    def set_shell_prewarm_workers(self, count: int) -> None:
        """Prepare helper shells without granting Mihomo or validation capacity."""

        target = max(self.min_workers, min(self.max_workers, int(count)))
        self._control.put_nowait(("shell_prewarm", target))
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def request_abort(self, worker_id: int, generation: int, reason: str) -> None:
        self._control.put_nowait(("abort", (int(worker_id), int(generation), str(reason))))
        self._wake.set()

    def pending_count(self) -> int:
        return max(0, int(self._commands.qsize()))

    def stop(self, timeout_seconds: float = 3.0) -> str:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        self._stop_deadline_at = deadline
        self._publish_stop_stage("caller_entered")
        watchdog_jobs = self.duplicate_job_handles(timeout_seconds=0.1)
        self._publish_stop_stage(f"caller_jobs_duplicated:{len(watchdog_jobs)}")
        self._stopping.set()
        self._wake.set()
        self._publish_stop_stage("caller_stop_signalled")
        thread = self._thread
        termination_completed = True
        if thread is not None:
            # A caller-owned duplicate Job handle is the bounded physical stop
            # boundary. Give helpers a short cooperative window, then clear all
            # Jobs in parallel while enough of the global deadline remains for
            # the supervisor to reap Pipes, process handles and registry state.
            cooperative = min(0.5, max(0.0, deadline - time.monotonic()))
            thread.join(timeout=cooperative)
            self._publish_stop_stage(
                f"caller_cooperative_join:{'alive' if thread.is_alive() else 'stopped'}"
            )
            if thread.is_alive():
                self._publish_stop_stage("caller_job_termination_started")
                termination_completed = _terminate_jobs_bounded(
                    list(watchdog_jobs.values()),
                    exit_code=74,
                    timeout_seconds=min(
                        0.35,
                        max(0.0, deadline - time.monotonic()),
                    ),
                    close_after=True,
                )
                watchdog_jobs = {}
                self._publish_stop_stage(
                    "caller_job_termination_completed"
                    if termination_completed
                    else "caller_job_termination_incomplete"
                )
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                self._publish_stop_stage(
                    f"caller_owner_join:{'alive' if thread.is_alive() else 'stopped'}"
                )
        caller_jobs_empty = False
        if (
            watchdog_jobs
            and thread is not None
            and not thread.is_alive()
            and self._stop_status == "STOP_FAILED_QUARANTINED"
        ):
            # The owner may observe a non-empty Job immediately before closing
            # its handle, then reap every process while the caller-owned
            # duplicate still provides authoritative physical readback.  Do
            # not preserve that transient sample as a permanent quarantine.
            # Conversely, never convert the failure without proving every Job
            # empty inside the original shared stop deadline.
            while time.monotonic() < deadline:
                caller_jobs_empty = True
                for job in watchdog_jobs.values():
                    try:
                        snapshot = job.snapshot()
                    except OSError:
                        caller_jobs_empty = False
                        break
                    if (
                        snapshot.active_processes > 0
                        or bool(snapshot.process_ids)
                    ):
                        caller_jobs_empty = False
                        break
                if caller_jobs_empty:
                    break
                time.sleep(0.01)
            self._publish_stop_stage(
                "caller_jobs_empty_confirmed"
                if caller_jobs_empty
                else "caller_jobs_empty_unconfirmed"
            )
        for job in watchdog_jobs.values():
            job.close()
        # A bounded termination helper can miss its 350ms scheduling window
        # under system load even though the owner subsequently observes every
        # Job and process as empty.  Quarantine is a physical-state verdict,
        # not a timing verdict for this auxiliary caller thread.
        if thread is not None and thread.is_alive():
            self._publish_stop_stage("caller_return:STOP_FAILED_QUARANTINED")
            return "STOP_FAILED_QUARANTINED"
        if (
            self._stop_status == "STOP_FAILED_QUARANTINED"
            and caller_jobs_empty
            and self._starter.is_quiescent()
            and not any(_process_is_alive(p) for p in self._stop_unconfirmed_processes)
        ):
            self._stop_status = "STOPPED"
        self._publish_stop_stage(f"caller_return:{self._stop_status}")
        return self._stop_status

    def snapshot(self) -> SupervisorSnapshot:
        with self._lock:
            workers = tuple(
                {
                    "worker_id": worker.worker_id,
                    "generation": worker.generation,
                    "phase": worker.phase.value,
                    "helper_pid": worker.helper_pid or int(worker.process.pid or 0),
                    "mihomo_pid": worker.mihomo_pid,
                    "mixed_port": worker.mixed_port,
                    "controller_port": worker.controller_port,
                    "lease_id": worker.command.lease_id if worker.command else "",
                    "candidate_digest": worker.command.candidate_digest if worker.command else "",
                    "transaction_id": worker.command.transaction_id if worker.command else "",
                    "slice_id": worker.command.slice_id if worker.command else 0,
                    "stage": worker.command.stage.value if worker.command else "",
                    "deadline_at": worker.deadline_at,
                    "lease_hard_deadline_at": worker.lease_hard_deadline_at,
                    "deadline_phase": worker.deadline_phase,
                    "transport_started_at": worker.transport_started_at,
                    "heartbeat_at": worker.heartbeat_at,
                    "startup_phase": worker.startup_phase,
                    "prewarm_started": worker.prewarm_started,
                    "prewarm_started_at": worker.prewarm_started_at,
                    "prewarm_membership_pending": bool(worker.prewarm_pending_pid),
                    "ready_at": worker.ready_at,
                    "startup_deadline_at": worker.startup_deadline_at,
                    "retirement_started_at": worker.retirement_started_at,
                    "retirement_reason": worker.replacement_reason,
                }
                for worker in sorted(self._workers.values(), key=lambda item: item.worker_id)
            )
            phases = [item["phase"] for item in workers]
            startup_pipeline_workers = self._startup_initializing_count_unlocked()
            return SupervisorSnapshot(
                desired_workers=self._desired_workers,
                shell_prewarm_workers=self._shell_prewarm_workers,
                prewarmed_waiting_workers=sum(
                    1
                    for item in workers
                    if (
                        item["phase"] == "waiting_start"
                        and int(item["worker_id"]) > self._desired_workers
                    )
                ),
                startup_bulkhead=self.startup_bulkhead,
                mihomo_start_bulkhead=self.mihomo_start_bulkhead,
                pending_spawn_workers=len(self._pending_spawns),
                startup_pipeline_workers=startup_pipeline_workers,
                active_workers=sum(
                    1
                    for phase in phases
                    if phase in {"idle", "reserved", "validating", "cancelling"}
                ),
                idle_workers=phases.count("idle"),
                reserved_workers=phases.count("reserved"),
                waiting_start_workers=phases.count("waiting_start"),
                validating_workers=(
                    phases.count("reserved")
                    + phases.count("validating")
                    + phases.count("cancelling")
                ),
                starting_workers=phases.count("starting"),
                retiring_workers=phases.count("retiring"),
                quarantined_workers=phases.count("quarantined"),
                pending_commands=max(0, int(self._commands.qsize())),
                capacity_reservation_rejected_count=(
                    self._capacity_reservation_rejected_count
                ),
                direct_dispatch_count=self._direct_dispatch_count,
                assignment_to_send_sample_count=(
                    self._assignment_to_send_sample_count
                ),
                assignment_to_send_total_ms=(
                    self._assignment_to_send_total_ms
                ),
                assignment_to_send_max_ms=self._assignment_to_send_max_ms,
                send_to_helper_sample_count=self._send_to_helper_sample_count,
                send_to_helper_total_ms=self._send_to_helper_total_ms,
                send_to_helper_max_ms=self._send_to_helper_max_ms,
                completion_count=self._completion_count,
                endpoint_result_count=self._endpoint_result_count,
                facility_failure_count=self._facility_failure_count,
                replacement_count=self._replacement_count,
                forced_job_termination_count=self._forced_job_termination_count,
                last_completion_monotonic=self._last_completion_at,
                stop_status=self._stop_status,
                hard_stall=self._hard_stall,
                disabled_worker_count=len(self._disabled_workers),
                startup_failure_count=self._startup_failure_count,
                last_failure_reason=self._last_failure_reason,
                failure_reason_counts=tuple(
                    sorted(self._failure_reason_counts.items())
                ),
                private_bytes=self._private_bytes,
                handle_count=self._handle_count,
                system_commit_headroom_bytes=self._system_commit_headroom_bytes,
                resource_headroom_limited=self._resource_headroom_limited,
                workers=workers,
            )

    def duplicate_job_handles(
        self,
        timeout_seconds: float | None = None,
    ) -> dict[tuple[int, int], WindowsJob]:
        duplicates: dict[tuple[int, int], WindowsJob] = {}
        if timeout_seconds is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(
                timeout=max(0.0, float(timeout_seconds))
            )
        if not acquired:
            return duplicates
        try:
            for worker in self._workers.values():
                if worker.phase in {Tier3WorkerPhase.STOPPED, Tier3WorkerPhase.QUARANTINED}:
                    continue
                try:
                    duplicates[(worker.worker_id, worker.generation)] = worker.job.duplicate()
                except OSError:
                    continue
        finally:
            self._lock.release()
        return duplicates

    def _log(self, level: str, message: str, *args: Any) -> None:
        target = getattr(self.logger, level, None)
        if callable(target):
            target(message, *args)

    def _publish_state(self) -> None:
        # Snapshot fields are already protected by the supervisor lock. This
        # method exists to keep all state publication sites visually explicit.
        return None

    def _record_spawn_failure(self, worker_id: int, exc: BaseException) -> None:
        failures = int(self._worker_failure_streak.get(worker_id, 0)) + 1
        self._worker_failure_streak[worker_id] = failures
        self._startup_failure_count += 1
        self._last_failure_reason = (
            f"worker_spawn_failed:{type(exc).__name__}:{exc}"
        )
        self._record_failure_reason(self._last_failure_reason)
        self._spawn_retry_at[worker_id] = time.monotonic() + min(
            2.0,
            0.25 * (2 ** max(0, failures - 1)),
        )
        self._log(
            "error",
            "Tier3 validator worker %s spawn failed: %s",
            worker_id,
            exc,
        )
        if failures >= 3:
            self._disabled_workers[worker_id] = self._last_failure_reason

    def _record_failure_reason(self, reason: str) -> None:
        category = str(reason or "unknown").split(":", 1)[0][:96]
        self._failure_reason_counts[category] = (
            int(self._failure_reason_counts.get(category, 0)) + 1
        )

    def _drain_spawn_results(self, *, discard: bool = False) -> None:
        for result in self._starter.drain_results():
            with self._lock:
                expected_generation = self._pending_spawns.pop(
                    result.worker_id,
                    None,
                )
            worker = result.worker
            if result.error is not None:
                if not self._stopping.is_set() and not discard:
                    self._record_spawn_failure(result.worker_id, result.error)
                continue
            if worker is None:
                continue
            if (
                discard
                or self._stopping.is_set()
                or expected_generation != result.generation
            ):
                self._starter._dispose_worker(worker)
                continue
            with self._lock:
                if result.worker_id in self._workers:
                    duplicate = True
                else:
                    self._workers[result.worker_id] = worker
                    duplicate = False
            if duplicate:
                self._starter._dispose_worker(worker)

    def _schedule_worker_spawn(self, worker_id: int) -> bool:
        generation = int(self._next_generation.get(worker_id, 0)) + 1
        self._next_generation[worker_id] = generation
        if not self._starter.submit(worker_id, generation):
            return False
        with self._lock:
            self._pending_spawns[worker_id] = generation
        return True

    def _startup_initializing_count_unlocked(self) -> int:
        return len(self._pending_spawns) + sum(
            worker.phase is Tier3WorkerPhase.STARTING
            or (
                worker.phase is Tier3WorkerPhase.WAITING_START
                and not worker.bootstrap_ready
            )
            for worker in self._workers.values()
        )

    def _initial_cohort_ready_unlocked(self) -> bool:
        if not self._startup_cohort_ready:
            ready = sum(
                worker.phase in {
                    Tier3WorkerPhase.IDLE,
                    Tier3WorkerPhase.RESERVED,
                    Tier3WorkerPhase.VALIDATING,
                    Tier3WorkerPhase.CANCELLING,
                }
                for worker in self._workers.values()
            )
            self._startup_cohort_ready = ready >= min(4, self._desired_workers)
        return self._startup_cohort_ready

    def _activate_waiting_workers(self) -> None:
        with self._lock:
            desired = self._desired_workers
            starting = sum(
                1
                for worker in self._workers.values()
                if worker.phase is Tier3WorkerPhase.STARTING
            )
            waiting = [
                worker
                for worker in sorted(
                    self._workers.values(), key=lambda item: item.worker_id
                )
                if (
                    worker.phase is Tier3WorkerPhase.WAITING_START
                    and worker.bootstrap_ready
                    and worker.worker_id <= desired
                )
            ]
            start_slots = max(0, self.mihomo_start_bulkhead - starting)
        for worker in waiting[:start_slots]:
            try:
                with self._lock:
                    current = self._workers.get(worker.worker_id)
                    if current is not worker:
                        continue
                    if worker.mixed_port <= 0 or worker.controller_port <= 0:
                        mixed_port = _reserve_unique_loopback_port(self._leased_ports)
                        self._leased_ports.add(mixed_port)
                        try:
                            controller_port = _reserve_unique_loopback_port(
                                self._leased_ports
                            )
                        except BaseException:
                            self._leased_ports.discard(mixed_port)
                            raise
                        self._leased_ports.add(controller_port)
                        worker.mixed_port = mixed_port
                        worker.controller_port = controller_port
            except BaseException as exc:
                self._retire_worker(
                    worker,
                    f"helper_port_lease_failed:{type(exc).__name__}:{exc}",
                    terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                )
                continue
            if not self._send(
                worker,
                "start",
                {
                    "worker_id": worker.worker_id,
                    "worker_generation": worker.generation,
                    "binary_path": self.binary_path,
                    "probe_urls": list(self.probe_urls),
                    "confirm_urls": list(self.confirm_urls),
                    "expected_statuses": sorted(self.expected_statuses),
                    "probe_timeout_seconds": self.probe_timeout_seconds,
                    "slow_threshold_ms": self.slow_threshold_ms,
                    "temp_parent": self.temp_parent,
                    "ipv6": self.ipv6,
                    "mixed_port": worker.mixed_port,
                    "controller_port": worker.controller_port,
                    "prewarm_config": build_validator_prewarm_config(
                        worker.mixed_port, worker.controller_port, ipv6=self.ipv6,
                    ),
                },
            ):
                self._retire_worker(
                    worker,
                    "helper_start_latch_send_failed",
                    terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                )
                continue
            with self._lock:
                current = self._workers.get(worker.worker_id)
                if current is worker:
                    worker.phase = Tier3WorkerPhase.STARTING
                    worker.started_at = time.monotonic()
                    worker.heartbeat_at = worker.started_at
                    worker.startup_phase = "start_sent"
                    worker.startup_deadline_at = (
                        worker.started_at + self.start_timeout_seconds
                    )
            starting += 1

    def _start_needed_workers(self) -> None:
        self._drain_spawn_results()
        self._activate_waiting_workers()
        with self._lock:
            existing_ids = set(self._workers) | set(self._pending_spawns)
            disabled_ids = set(self._disabled_workers)
            desired = self._desired_workers
            shell_target = max(desired, self._shell_prewarm_workers)
            shell_slots = max(0, self.startup_bulkhead - len(self._pending_spawns))
            initial_facilities_started = all(
                worker_id in self._workers
                and getattr(self._workers[worker_id], "prewarm_started", False)
                and self._workers[worker_id].phase in {
                    Tier3WorkerPhase.STARTING, Tier3WorkerPhase.IDLE,
                    Tier3WorkerPhase.RESERVED, Tier3WorkerPhase.VALIDATING,
                    Tier3WorkerPhase.CANCELLING,
                }
                for worker_id in range(1, min(4, desired) + 1)
            )
            if not self._initial_cohort_ready_unlocked() and not initial_facilities_started:
                # Start the first owned facilities before importing further
                # shells. Capacity still requires the full READY contract.
                shell_slots = min(
                    shell_slots,
                    max(0, min(4, desired) - len(existing_ids)),
                )
        if self._resource_headroom_limited:
            with self._lock:
                usable = any(
                    item.phase not in {
                        Tier3WorkerPhase.QUARANTINED,
                        Tier3WorkerPhase.STOPPED,
                    }
                    for item in self._workers.values()
                )
                if not usable:
                    self._hard_stall = True
                    self._stop_status = "VALIDATOR_RESOURCE_HARD_STALL"
            return
        for worker_id in range(1, desired + 1):
            if shell_slots <= 0:
                break
            if worker_id in existing_ids or worker_id in disabled_ids:
                continue
            if time.monotonic() < float(self._spawn_retry_at.get(worker_id, 0.0)):
                continue
            if not self._schedule_worker_spawn(worker_id):
                continue
            existing_ids.add(worker_id)
            shell_slots -= 1
        prewarm_slots = min(2, shell_slots)
        for worker_id in range(desired + 1, shell_target + 1):
            if prewarm_slots <= 0:
                break
            if worker_id in existing_ids or worker_id in disabled_ids:
                continue
            if time.monotonic() < float(self._spawn_retry_at.get(worker_id, 0.0)):
                continue
            if not self._schedule_worker_spawn(worker_id):
                continue
            existing_ids.add(worker_id)
            prewarm_slots -= 1
        with self._lock:
            usable = any(
                item.phase not in {
                    Tier3WorkerPhase.QUARANTINED,
                    Tier3WorkerPhase.STOPPED,
                }
                for item in self._workers.values()
            )
            all_disabled = all(
                worker_id in self._disabled_workers
                for worker_id in range(1, self._desired_workers + 1)
            )
            if not usable and not self._pending_spawns and all_disabled:
                self._hard_stall = True
                self._stop_status = "VALIDATOR_POOL_HARD_STALL"

    def refresh_resource_metrics(self) -> None:
        now = time.monotonic()
        if now - self._last_resource_sample_at < 0.5:
            return
        self._last_resource_sample_at = now
        try:
            system = system_resource_snapshot()
        except OSError as exc:
            self._last_failure_reason = f"resource_probe_failed:{exc}"
            return
        with self._lock:
            pids = {
                int(pid)
                for worker in self._workers.values()
                for pid in (worker.helper_pid, worker.mihomo_pid)
                if int(pid or 0) > 0
            }
        private_bytes = 0
        handle_count = 0
        for pid in pids:
            metrics = process_resource_snapshot(pid)
            private_bytes += int(metrics.private_bytes)
            handle_count += int(metrics.handle_count)
        pair_reserve = 128 * 1024 * 1024
        system_reserve = 1024 * 1024 * 1024
        with self._lock:
            self._private_bytes = private_bytes
            self._handle_count = handle_count
            self._system_commit_headroom_bytes = int(system.commit_headroom_bytes)
            self._resource_headroom_limited = (
                int(system.commit_headroom_bytes) < system_reserve + pair_reserve
            )
            if self._resource_headroom_limited:
                self._last_failure_reason = "validator_resource_headroom_limited"

    def _send(self, worker: _WorkerGeneration, kind: str, payload: dict[str, Any]) -> bool:
        with worker.send_lock:
            try:
                worker.connection.send_bytes(message_bytes(kind, payload))
                return True
            except (BrokenPipeError, EOFError, OSError):
                return False

    def _dispatch_reserved_command(
        self,
        worker: _WorkerGeneration,
        command: ValidateEndpointCmd,
        *,
        direct: bool,
    ) -> bool:
        """Deliver one exact reserved lease without a second scheduling queue."""

        send_failed = False
        sent_command: ValidateEndpointCmd | None = None
        with worker.send_lock:
            sent_command = replace(
                command,
                supervisor_sent_monotonic_ns=time.monotonic_ns(),
            )
            with self._lock:
                current = self._workers.get(worker.worker_id)
                valid = bool(
                    not self._stopping.is_set()
                    and current is worker
                    and worker.generation == int(command.worker_generation)
                    and worker.phase is Tier3WorkerPhase.RESERVED
                    and worker.command is command
                    and worker.reservation_id != 0
                )
                if not valid:
                    self._capacity_reservation_rejected_count += 1
                    return False
                # Publish the immutable sent command before making bytes
                # visible to the helper.  An extremely fast fixture can return
                # while this caller is still unwinding send_bytes().
                worker.command = sent_command
            try:
                worker.connection.send_bytes(
                    message_bytes(
                        "validate_endpoint",
                        dataclass_payload(sent_command),
                    )
                )
            except (BrokenPipeError, EOFError, OSError):
                send_failed = True

        assignment_to_send_ms = max(
            0.0,
            (
                int(sent_command.supervisor_sent_monotonic_ns)
                - int(sent_command.supervisor_assigned_monotonic_ns)
            )
            / 1_000_000.0,
        )
        with self._lock:
            self._assignment_to_send_sample_count += 1
            self._assignment_to_send_total_ms += assignment_to_send_ms
            self._assignment_to_send_max_ms = max(
                self._assignment_to_send_max_ms,
                assignment_to_send_ms,
            )
            if direct:
                self._direct_dispatch_count += 1
            current = self._workers.get(worker.worker_id)
            if (
                not send_failed
                and current is worker
                and worker.command is sent_command
                and worker.phase is Tier3WorkerPhase.RESERVED
            ):
                worker.reservation_id = 0
                worker.phase = Tier3WorkerPhase.VALIDATING
                dispatched_at = time.monotonic()
                worker.deadline_at = dispatched_at + 1.0
                worker.lease_hard_deadline_at = (
                    dispatched_at
                    + max(1.0, float(sent_command.candidate_hard_cap_seconds))
                )
                worker.deadline_phase = "dispatch_ack"
                worker.transport_started_at = 0.0
                worker.cancel_deadline_at = 0.0
                worker.sealed_lease_id = ""
                worker.cancel_reason = ""
                worker.cancel_requested_monotonic_ns = 0
        if send_failed:
            self._retire_worker(
                worker,
                "pipe_send_failed",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
        return True

    def _assign_pending(self) -> None:
        while True:
            try:
                pending = self._commands.get_nowait()
            except queue.Empty:
                return
            if int(pending.worker_id) <= 0 or int(pending.worker_generation) <= 0:
                with self._lock:
                    reserved = next(
                        (
                            item
                            for item in sorted(
                                self._workers.values(),
                                key=lambda current: current.worker_id,
                            )
                            if item.phase is Tier3WorkerPhase.IDLE
                            and item.command is None
                            and item.reservation_id <= 0
                        ),
                        None,
                    )
                    if reserved is not None:
                        pending = replace(
                            pending,
                            worker_id=reserved.worker_id,
                            worker_generation=reserved.generation,
                            capacity_reservation_id=-1,
                            supervisor_assigned_monotonic_ns=time.monotonic_ns(),
                        )
                        reserved.command = pending
                        reserved.reservation_id = -1
                        reserved.phase = Tier3WorkerPhase.RESERVED
                if reserved is None:
                    self._commands.put_nowait(pending)
                    return
            if reserved is None:
                self._capacity_reservation_rejected_count += 1
                continue
            self._dispatch_reserved_command(
                reserved,
                pending,
                direct=False,
            )

    def _yield_for_lost_worker(
        self,
        worker: _WorkerGeneration,
        reason: str,
        *,
        clean_confirmed: bool,
        kind: EndpointAttemptKind = EndpointAttemptKind.RIGHT_CENSORED,
    ) -> EndpointAttemptResult | None:
        command = worker.command
        if command is None:
            return None
        attempts: tuple[ProbeAttemptFact, ...] = ()
        endpoint = str(command.endpoint or "")
        if endpoint and worker.transport_started_at > 0.0:
            attempts += (
                # A parent-side seal has no trusted transport result. Preserve
                # it only as a censored fact for deep ordering and diagnostics.
                ProbeAttemptFact(
                    endpoint=endpoint,
                    elapsed_ms=max(0, int(command.probe_budget_seconds * 1000)),
                    full_budget=False,
                    completed=False,
                    outcome="right_censored",
                    failure_domain="slice_deadline",
                    detail=reason,
                    attempt_id=f"{command.lease_id}:parent-seal",
                    generation_id=f"{worker.worker_id}:{worker.generation}",
                    candidate_fingerprint=command.candidate_digest,
                    session_lease_id=command.lease_id,
                    stage=command.stage.value,
                    granted_budget_ms=int(command.probe_budget_seconds * 1000),
                    lifecycle="censored",
                ),
            )
        return EndpointAttemptResult(
            refresh_epoch=command.refresh_epoch,
            transaction_id=command.transaction_id,
            candidate_digest=command.candidate_digest,
            config_digest=command.config_digest,
            lease_id=command.lease_id,
            slice_id=command.slice_id,
            sequence=command.sequence,
            stage=command.stage,
            purpose=command.purpose,
            worker_id=worker.worker_id,
            worker_generation=worker.generation,
            node_json=command.node_json,
            kind=kind,
            report_json=command.report_json,
            reason=reason,
            attempt_facts=attempts,
            clean_confirmed=clean_confirmed,
            helper_pid=worker.helper_pid,
            mihomo_pid=worker.mihomo_pid,
            probe_policy_revision=command.probe_policy_revision,
            portfolio_digest=command.portfolio_digest,
            submitted_monotonic_ns=command.submitted_monotonic_ns,
            dispatch_monotonic_ns=command.dispatch_monotonic_ns,
            supervisor_assigned_monotonic_ns=(
                command.supervisor_assigned_monotonic_ns
            ),
            supervisor_sent_monotonic_ns=command.supervisor_sent_monotonic_ns,
        )

    def _emit_attempt(self, result: EndpointAttemptResult) -> None:
        self._completion_count += 1
        self._endpoint_result_count += 1
        self._last_completion_at = time.monotonic()
        if result.kind is EndpointAttemptKind.FACILITY_FAIL:
            self._facility_failure_count += 1
        try:
            self.attempt_sink(result)
        except Exception:
            self._log("exception", "Tier3 validator endpoint result sink failed")

    def _emit_lost_result(
        self,
        worker: _WorkerGeneration,
        kind: Tier3TerminalKind,
        reason: str,
        *,
        clean_confirmed: bool,
    ) -> None:
        command = worker.command
        if command is not None:
            attempt_kind = {
                Tier3TerminalKind.CANDIDATE_FAIL: EndpointAttemptKind.CANDIDATE_FAIL,
                Tier3TerminalKind.FACILITY_FAIL: EndpointAttemptKind.FACILITY_FAIL,
                Tier3TerminalKind.CANCELLED: EndpointAttemptKind.CANCELLED,
            }.get(kind, EndpointAttemptKind.RIGHT_CENSORED)
            result = self._yield_for_lost_worker(
                worker,
                reason,
                clean_confirmed=clean_confirmed,
                kind=attempt_kind,
            )
            if result is not None:
                self._emit_attempt(result)
            return
    def _remove_worker(
        self,
        worker: _WorkerGeneration,
        *,
        join_timeout: float = 0.1,
        close_process_handle: bool = True,
    ) -> None:
        self._pipe_wait.release(worker.connection)
        with self._lock:
            if self._workers.get(worker.worker_id) is worker:
                self._workers.pop(worker.worker_id, None)
            self._leased_ports.discard(int(worker.mixed_port or 0))
            self._leased_ports.discard(int(worker.controller_port or 0))
        try:
            worker.connection.close()
        except OSError:
            pass
        try:
            worker.process.join(timeout=max(0.0, float(join_timeout)))
        except (AssertionError, ValueError):
            pass
        worker.job.close()
        if close_process_handle:
            _close_stopped_process(worker.process)

    def _retire_worker(
        self,
        worker: _WorkerGeneration,
        reason: str,
        *,
        terminal_kind: Tier3TerminalKind | None = None,
        replace_worker: bool = True,
        record_failure: bool = True,
    ) -> None:
        was_idle = False
        with self._lock:
            if self._workers.get(worker.worker_id) is not worker:
                return
            if worker.phase in {
                Tier3WorkerPhase.RETIRING,
                Tier3WorkerPhase.QUARANTINED,
                Tier3WorkerPhase.STOPPED,
            }:
                return
            was_idle = (
                worker.phase is Tier3WorkerPhase.IDLE
                and worker.command is None
            )
            worker.phase = Tier3WorkerPhase.RETIRING
            worker.reservation_id = 0
            worker.replacement_reason = reason
            worker.retirement_started_at = time.monotonic()
            worker.retirement_terminal_kind = terminal_kind
            worker.retirement_replace_worker = bool(replace_worker)
            worker.retirement_record_failure = bool(record_failure)
            worker.retirement_was_ready = worker.ready_at > 0.0
            if record_failure:
                self._last_failure_reason = str(reason)
                self._record_failure_reason(reason)
        if was_idle:
            self.capacity_sink(-1)
        # TerminateJobObject is normally quick, but Windows can keep the call
        # blocked while a dying helper is unwinding its Pipe/HTTP handles.  It
        # must never run on the sole supervisor thread: doing so stops Pipe
        # draining, deadline checks and every other worker at once.  A
        # duplicate handle gives the bounded terminator thread independent
        # ownership while this thread keeps servicing the generation until
        # the Job is observed empty.
        try:
            termination_job = worker.job.duplicate()
        except OSError as exc:
            termination_job = None
            self._log(
                "error",
                "Tier3 validator Job handle duplication failed: %s",
                exc,
            )
        if termination_job is not None:
            self._forced_job_termination_count += 1
            _terminate_jobs_bounded(
                [termination_job],
                exit_code=74,
                timeout_seconds=0.05,
                close_after=True,
            )
        # TerminateJobObject applies to the full helper/Mihomo generation.  Job
        # emptiness is reaped from the supervisor tick instead of waiting here;
        # otherwise several simultaneous deadlines serialize this single
        # owner thread and prevent both dispatch and bounded stop.
        self._reap_retiring_workers()

    def _finalize_retiring_worker(
        self,
        worker: _WorkerGeneration,
        *,
        job_empty: bool,
    ) -> None:
        with self._lock:
            if self._workers.get(worker.worker_id) is not worker:
                return
            if worker.phase is not Tier3WorkerPhase.RETIRING:
                return
            terminal_kind = worker.retirement_terminal_kind
            replace_worker = worker.retirement_replace_worker
            record_failure = worker.retirement_record_failure
            was_ready = worker.retirement_was_ready
            reason = worker.replacement_reason or "worker_retired"

        released = ports_released((worker.mixed_port, worker.controller_port))
        if not job_empty:
            if terminal_kind is not None and worker.command is not None:
                self._emit_lost_result(
                    worker,
                    terminal_kind,
                    reason,
                    clean_confirmed=False,
                )
            if self.generation_sink is not None:
                try:
                    self.generation_sink(
                        GenerationGone(
                            worker_id=worker.worker_id,
                            worker_generation=worker.generation,
                            helper_pid=worker.helper_pid,
                            mihomo_pid=worker.mihomo_pid,
                            reason=reason,
                            job_empty=False,
                            ports_released=released,
                        )
                    )
                except Exception:
                    self._log("exception", "Tier3 generation sink failed")
            with self._lock:
                if self._workers.get(worker.worker_id) is not worker:
                    return
                worker.phase = Tier3WorkerPhase.QUARANTINED
                worker.command = None
                self._disabled_workers[worker.worker_id] = str(reason)
                usable = sum(
                    1
                    for item in self._workers.values()
                    if item.phase not in {
                        Tier3WorkerPhase.QUARANTINED,
                        Tier3WorkerPhase.STOPPED,
                    }
                )
                eligible_missing = any(
                    worker_id not in self._disabled_workers
                    and worker_id not in self._workers
                    for worker_id in range(1, self._desired_workers + 1)
                )
                if usable <= 0 and not eligible_missing:
                    self._hard_stall = True
                    self._stop_status = "VALIDATOR_POOL_HARD_STALL"
            return

        try:
            worker.process.join(timeout=0.0)
        except (AssertionError, ValueError):
            pass
        # Once the Job is empty, remove the generation from the owner registry
        # before publishing its terminal.  Consumers must not observe a result
        # that is already complete while the same generation still counts as a
        # retiring worker or occupies the worker id in the next owner tick.
        self._remove_worker(worker)
        if terminal_kind is not None and worker.command is not None:
            self._emit_lost_result(
                worker,
                terminal_kind,
                reason,
                clean_confirmed=True,
            )
        if self.generation_sink is not None:
            try:
                self.generation_sink(
                    GenerationGone(
                        worker_id=worker.worker_id,
                        worker_generation=worker.generation,
                        helper_pid=worker.helper_pid,
                        mihomo_pid=worker.mihomo_pid,
                        reason=reason,
                        job_empty=True,
                        ports_released=released,
                    )
                )
            except Exception:
                self._log("exception", "Tier3 generation sink failed")
        failure_streak = int(self._worker_failure_streak.get(worker.worker_id, 0))
        if record_failure:
            failure_streak += 1
            self._worker_failure_streak[worker.worker_id] = failure_streak
            if not was_ready:
                self._startup_failure_count += 1
        if replace_worker and failure_streak >= 3:
            self._disabled_workers[worker.worker_id] = str(reason)
            replace_worker = False
        if replace_worker and not self._stopping.is_set():
            self._replacement_count += 1
        with self._lock:
            usable = sum(
                1
                for item in self._workers.values()
                if item.phase not in {
                    Tier3WorkerPhase.QUARANTINED,
                    Tier3WorkerPhase.STOPPED,
                }
            )
            eligible_missing = any(
                worker_id not in self._disabled_workers
                for worker_id in range(1, self._desired_workers + 1)
            )
            if usable <= 0 and not eligible_missing:
                self._hard_stall = True
                self._stop_status = "VALIDATOR_POOL_HARD_STALL"

    def _reap_retiring_workers(self) -> None:
        now = time.monotonic()
        with self._lock:
            retiring = [
                worker
                for worker in self._workers.values()
                if worker.phase is Tier3WorkerPhase.RETIRING
                and worker.retirement_started_at > 0.0
            ]
        for worker in retiring:
            try:
                snapshot = worker.job.snapshot()
                empty = (
                    snapshot.active_processes <= 0
                    and not snapshot.process_ids
                )
            except OSError:
                empty = False
            if empty:
                self._finalize_retiring_worker(worker, job_empty=True)
                continue
            if now - worker.retirement_started_at >= self.job_empty_wait_seconds:
                self._finalize_retiring_worker(worker, job_empty=False)

    def _confirm_ready_membership(self, worker: _WorkerGeneration) -> bool:
        """Publish helper capacity once both owned processes appear in its Job."""

        with self._lock:
            if (
                self._workers.get(worker.worker_id) is not worker
                or worker.phase is not Tier3WorkerPhase.STARTING
                or worker.startup_phase != "membership_pending"
            ):
                return False
            helper_pid = int(worker.helper_pid or 0)
            mihomo_pid = int(worker.mihomo_pid or 0)
        try:
            snapshot = worker.job.snapshot()
        except OSError:
            return False
        if helper_pid not in snapshot.process_ids or mihomo_pid not in snapshot.process_ids:
            return False
        with self._lock:
            if (
                self._workers.get(worker.worker_id) is not worker
                or worker.phase is not Tier3WorkerPhase.STARTING
                or worker.startup_phase != "membership_pending"
                or worker.helper_pid != helper_pid
                or worker.mihomo_pid != mihomo_pid
            ):
                return False
            worker.ready_at = time.monotonic()
            worker.heartbeat_at = worker.ready_at
            worker.startup_phase = "ready"
            worker.startup_deadline_at = 0.0
            worker.prewarm_pending_pid = 0
            worker.phase = Tier3WorkerPhase.IDLE
            worker.reservation_id = 0
        self.capacity_sink(1)
        return True

    def _handle_ready(self, worker: _WorkerGeneration, payload: dict[str, Any]) -> None:
        helper_pid = int(payload.get("helper_pid", 0) or 0)
        mihomo_pid = int(payload.get("mihomo_pid", 0) or 0)
        reported_mixed_port = int(payload.get("mixed_port", 0) or 0)
        reported_controller_port = int(payload.get("controller_port", 0) or 0)
        if (
            reported_mixed_port != worker.mixed_port
            or reported_controller_port != worker.controller_port
            or reported_mixed_port <= 0
            or reported_controller_port <= 0
            or reported_mixed_port == reported_controller_port
        ):
            self._retire_worker(
                worker,
                "helper_port_lease_mismatch",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
            return
        with self._lock:
            if worker.phase is not Tier3WorkerPhase.STARTING:
                self._capacity_reservation_rejected_count += 1
                return
            worker.helper_pid = helper_pid
            worker.mihomo_pid = mihomo_pid
            worker.startup_phase = "membership_pending"
            # Job accounting can lag the helper's controller-ready message
            # under concurrent Windows process startup.  Keep the sole Pipe
            # reader non-blocking and verify on normal supervisor ticks.
            worker.startup_deadline_at = time.monotonic() + 5.0
        self._confirm_ready_membership(worker)

    def _confirm_prewarm_membership(self, worker: _WorkerGeneration) -> bool:
        now = time.monotonic()
        with self._lock:
            if (
                self._workers.get(worker.worker_id) is not worker
                or worker.phase is not Tier3WorkerPhase.STARTING
                or not worker.prewarm_pending_pid
                or now < worker.prewarm_check_at
            ):
                return False
            pid = worker.prewarm_pending_pid
            helper_pid = worker.helper_pid
            worker.prewarm_check_at = now + 0.05
        try:
            members = worker.job.snapshot().process_ids
        except OSError:
            return False
        if helper_pid not in members or pid not in members:
            return False
        with self._lock:
            if (
                self._workers.get(worker.worker_id) is not worker
                or worker.phase is not Tier3WorkerPhase.STARTING
                or worker.prewarm_pending_pid != pid
            ):
                return False
            worker.prewarm_started = True
            worker.prewarm_started_at = now
            worker.prewarm_pending_pid = 0
        return True

    def _handle_startup_progress(
        self,
        worker: _WorkerGeneration,
        payload: dict[str, Any],
    ) -> None:
        phase = str(payload.get("phase") or "").strip().lower()
        if phase == "prewarm_started":
            pid = payload.get("mihomo_pid")
            with self._lock:
                if (
                    self._workers.get(worker.worker_id) is not worker
                    or worker.phase is not Tier3WorkerPhase.STARTING
                    or payload.get("worker_id") != worker.worker_id
                    or payload.get("worker_generation") != worker.generation
                    or payload.get("helper_pid") != worker.helper_pid
                    or type(pid) is not int or pid <= 0 or pid == worker.helper_pid
                    or payload.get("mixed_port") != worker.mixed_port
                    or payload.get("controller_port") != worker.controller_port
                    or worker.prewarm_started
                    or worker.prewarm_pending_pid not in {0, pid}
                ):
                    return
                worker.prewarm_pending_pid = pid
            self._confirm_prewarm_membership(worker)
            return
        phase_order = {
            "start_sent": 0,
            "runtime_imported": 1,
            "mihomo_starting": 2,
            "mihomo_started": 3,
        }
        if phase not in phase_order:
            return
        now = time.monotonic()
        with self._lock:
            current = self._workers.get(worker.worker_id)
            if current is not worker or worker.phase is not Tier3WorkerPhase.STARTING:
                return
            if phase_order[phase] <= phase_order.get(worker.startup_phase, -1):
                return
            worker.startup_phase = phase
            worker.heartbeat_at = now
            worker.startup_deadline_at = min(
                worker.started_at + TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS,
                now + self.start_timeout_seconds,
            )

    @staticmethod
    def _result_matches_command(
        worker: _WorkerGeneration,
        command: ValidateEndpointCmd,
        result: EndpointAttemptResult,
    ) -> bool:
        return bool(
            result.worker_id == worker.worker_id
            and result.worker_generation == worker.generation
            and result.lease_id == command.lease_id
            and result.refresh_epoch == command.refresh_epoch
            and result.candidate_digest == command.candidate_digest
            and result.config_digest == command.config_digest
            and result.transaction_id == command.transaction_id
            and result.slice_id == command.slice_id
            and result.sequence == command.sequence
            and result.stage is command.stage
            and result.purpose is command.purpose
            and result.probe_policy_revision == command.probe_policy_revision
            and result.portfolio_digest == command.portfolio_digest
        )

    @staticmethod
    def _cancel_terminal_kind(reason: str) -> Tier3TerminalKind:
        if reason in {"helper_stop", "runtime_stop", "supervisor_stop"}:
            return Tier3TerminalKind.CANCELLED
        if reason in {
            "validator_dispatch_ack_timeout",
            "validator_facility_start_timeout",
            "validator_facility_deadline",
        }:
            return Tier3TerminalKind.FACILITY_FAIL
        return Tier3TerminalKind.CENSORED

    def _complete_cancelled_worker(
        self,
        worker: _WorkerGeneration,
        *,
        reason: str,
    ) -> bool:
        command = worker.command
        if command is None:
            return False
        with self._lock:
            if (
                worker.command is not command
                or worker.phase is not Tier3WorkerPhase.CANCELLING
                or worker.sealed_lease_id != command.lease_id
            ):
                return False
        # The parent deadline remains authoritative. A late helper PASS can
        # prove physical cleanup, but can never regain result or admission
        # authority after the lease was sealed.
        self._emit_lost_result(
            worker,
            self._cancel_terminal_kind(reason),
            reason,
            clean_confirmed=True,
        )
        with self._lock:
            if worker.command is not command:
                return False
            worker.command = None
            worker.deadline_at = 0.0
            worker.lease_hard_deadline_at = 0.0
            worker.deadline_phase = ""
            worker.transport_started_at = 0.0
            worker.cancel_deadline_at = 0.0
            worker.sealed_lease_id = ""
            worker.cancel_reason = ""
            worker.cancel_requested_monotonic_ns = 0
            worker.phase = Tier3WorkerPhase.IDLE
            worker.reservation_id = 0
            worker.heartbeat_at = time.monotonic()
        self._worker_failure_streak[worker.worker_id] = 0
        self.capacity_sink(1)
        return True

    def _handle_endpoint_result(
        self,
        worker: _WorkerGeneration,
        payload: dict[str, Any],
    ) -> None:
        result = endpoint_attempt_result_from_payload(payload)
        command = worker.command
        if command is None:
            return
        # Once the parent seals a lease, every later helper result from that
        # lease is diagnostic-only.  Reject it before validating attempt facts
        # so a deliberately fact-less late PASS cannot retire the replacement
        # path or acquire admission authority.
        if worker.sealed_lease_id == result.lease_id:
            if (
                worker.phase is Tier3WorkerPhase.CANCELLING
                and result.clean_confirmed
                and self._result_matches_command(worker, command, result)
            ):
                self._complete_cancelled_worker(
                    worker,
                    reason=(
                        str(getattr(worker, "cancel_reason", "") or "")
                        or "candidate_hard_deadline"
                    ),
                )
            return
        # One command still yields exactly one portfolio fact.  A passing slice
        # may append its mainstream HTTPS confirm facts behind it (at most
        # CONFIRM_HTTPS_ATTEMPTS_PER_URL per configured confirm URL: a
        # transport failure is sampled once more); those never name the
        # command endpoint.
        portfolio_facts = tuple(
            fact for fact in result.attempt_facts if not is_confirm_https_fact(fact)
        )
        confirm_fact_count = len(result.attempt_facts) - len(portfolio_facts)
        result_endpoint = ""
        if portfolio_facts:
            result_endpoint = str(portfolio_facts[-1].endpoint or "")
        command_endpoint = str(command.endpoint or "")
        allows_pre_transport_result = (
            result.kind in {
                EndpointAttemptKind.FACILITY_FAIL,
                EndpointAttemptKind.CANCELLED,
            }
            or (
                result.kind is EndpointAttemptKind.CANDIDATE_FAIL
                and result.reason == "unsupported_proxy"
            )
        )
        identity_mismatch = not self._result_matches_command(
            worker,
            command,
            result,
        )
        fact_mismatch = (
            len(portfolio_facts) > 1
            or confirm_fact_count
            > CONFIRM_HTTPS_ATTEMPTS_PER_URL * len(getattr(self, "confirm_urls", ()))
            or (
                bool(result.attempt_facts)
                and is_confirm_https_fact(result.attempt_facts[0])
            )
            or (
                not result.attempt_facts
                and not allows_pre_transport_result
            )
            or (
                bool(result.attempt_facts)
                and bool(command_endpoint)
                and result_endpoint != command_endpoint
            )
        )
        if identity_mismatch or fact_mismatch:
            self._log(
                "error",
                (
                    "Tier3 helper endpoint result protocol mismatch "
                    "(worker=%s generation=%s kind=%s facts=%s "
                    "identity_mismatch=%s endpoint_mismatch=%s)"
                ),
                worker.worker_id,
                worker.generation,
                result.kind.value,
                len(result.attempt_facts),
                identity_mismatch,
                bool(
                    result.attempt_facts
                    and command_endpoint
                    and result_endpoint != command_endpoint
                ),
            )
            self._retire_worker(
                worker,
                "helper_endpoint_result_protocol_mismatch",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
            return
        # Match every generation/lease/config field before reattaching the
        # parent-owned data. Echoing that data in each result can fill the
        # Windows message Pipe and block the helper's control event loop.
        result = replace(
            result,
            node_json=command.node_json,
            report_json=command.report_json,
        )
        if (
            result.supervisor_sent_monotonic_ns > 0
            and result.helper_started_monotonic_ns
            >= result.supervisor_sent_monotonic_ns
        ):
            send_to_helper_ms = (
                result.helper_started_monotonic_ns
                - result.supervisor_sent_monotonic_ns
            ) / 1_000_000.0
            with self._lock:
                self._send_to_helper_sample_count += 1
                self._send_to_helper_total_ms += send_to_helper_ms
                self._send_to_helper_max_ms = max(
                    self._send_to_helper_max_ms,
                    send_to_helper_ms,
                )
        self._emit_attempt(result)
        if (
            not result.clean_confirmed
            and (worker.transport_started_at > 0.0 or bool(result.attempt_facts))
        ):
            # The result itself is valid evidence, but this generation still
            # owns transport cleanup and must never be returned to the idle
            # pool.  Retirement has no second terminal because the actor has
            # already received this exact endpoint result.
            self._retire_worker(
                worker,
                "helper_endpoint_result_unclean",
                terminal_kind=None,
            )
            return
        self._worker_failure_streak[worker.worker_id] = 0
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
            worker.phase = Tier3WorkerPhase.IDLE
            worker.reservation_id = 0
            worker.heartbeat_at = time.monotonic()
        self.capacity_sink(1)

    def _handle_slice_phase(
        self,
        worker: _WorkerGeneration,
        payload: dict[str, Any],
    ) -> None:
        command = worker.command
        if command is None:
            return
        if (
            str(payload.get("lease_id") or "") != command.lease_id
            or int(payload.get("slice_id", 0) or 0) != command.slice_id
            or str(payload.get("stage") or "") != command.stage.value
        ):
            return
        phase = str(payload.get("phase") or "")
        now = time.monotonic()
        with self._lock:
            if phase == "dispatch_received":
                # The helper consumed this exact command.  Keep a short,
                # separate guard until its async facility task publishes the
                # real facility start; probe and facility entitlement still
                # begin only at their corresponding phase events.
                worker.deadline_at = now + 1.0
                worker.deadline_phase = "facility_start"
            elif phase == "facility_started":
                worker.deadline_at = now + command.facility_budget_seconds + 0.75
                worker.lease_hard_deadline_at = max(
                    worker.lease_hard_deadline_at,
                    now + max(1.0, float(command.candidate_hard_cap_seconds)),
                )
                worker.deadline_phase = "facility"
            elif phase == "transport_started":
                worker.transport_started_at = now
                # The helper owns the exact HTTP entitlement.  The parent guard
                # is only the physical termination boundary, so transport start
                # must not shorten the candidate-wide hard cap established when
                # facility work began.  On Windows, 16 helpers can deliver a
                # normal timeout result after the 750ms delivery allowance even
                # though the transport itself ended on time; shortening here
                # turns that harmless scheduling delay into Mihomo churn.
                worker.deadline_at = max(
                    worker.lease_hard_deadline_at,
                    now + command.probe_budget_seconds + 0.75,
                )
                worker.deadline_phase = "probe"

    def _handle_clean_confirmed(self, worker: _WorkerGeneration, payload: dict[str, Any]) -> None:
        command = worker.command
        if command is None:
            return
        if str(payload.get("lease_id") or "") != command.lease_id:
            return
        if worker.phase is not Tier3WorkerPhase.CANCELLING:
            return
        self._complete_cancelled_worker(
            worker,
            reason=(
                str(payload.get("reason") or "")
                or str(getattr(worker, "cancel_reason", "") or "")
                or "candidate_hard_deadline"
            ),
        )

    def _handle_heartbeat(
        self,
        worker: _WorkerGeneration,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            worker.heartbeat_at = time.monotonic()
            worker.mihomo_pid = int(
                payload.get("mihomo_pid", worker.mihomo_pid) or 0
            )
            command = worker.command
            cancelling = bool(
                command is not None
                and worker.phase is Tier3WorkerPhase.CANCELLING
                and worker.sealed_lease_id == command.lease_id
            )
            cancel_requested_ns = int(
                getattr(worker, "cancel_requested_monotonic_ns", 0) or 0
            )
        if not cancelling or command is None:
            return
        try:
            heartbeat_ns = int(payload.get("monotonic_ns", 0) or 0)
            payload_worker_id = int(payload.get("worker_id", 0) or 0)
            payload_generation = int(payload.get("worker_generation", 0) or 0)
            active_probe_count = int(payload.get("active_probe_count", -1))
        except (TypeError, ValueError):
            return
        heartbeat_lease = str(payload.get("lease_id") or "")
        if (
            payload_worker_id != worker.worker_id
            or payload_generation != worker.generation
            or heartbeat_ns < cancel_requested_ns
            or active_probe_count != 0
            or heartbeat_lease not in {"", command.lease_id}
        ):
            return
        self._complete_cancelled_worker(
            worker,
            reason=(
                str(getattr(worker, "cancel_reason", "") or "")
                or "candidate_hard_deadline"
            ),
        )

    def _handle_pipe(self, worker: _WorkerGeneration) -> None:
        try:
            self._pipe_wait.release(worker.connection)
            raw = receive_ready_pipe_message(worker.connection)
            if raw == BOOTSTRAP_READY_MESSAGE:
                with self._lock:
                    current = self._workers.get(worker.worker_id)
                    if (
                        current is worker
                        and worker.phase is Tier3WorkerPhase.WAITING_START
                    ):
                        worker.bootstrap_ready = True
                        worker.heartbeat_at = time.monotonic()
                self._wake.set()
                return
            kind, payload = parse_message(raw)
        except (EOFError, BrokenPipeError, OSError, ValueError) as exc:
            if self._stopping.is_set() or worker.stop_requested:
                # Stop owns one global deadline.  Do not enter the ordinary
                # per-worker retirement path here: its Job wait is correct for
                # isolated replacement but would serialize across every
                # worker during runtime shutdown.
                with self._lock:
                    if self._workers.get(worker.worker_id) is worker:
                        worker.phase = Tier3WorkerPhase.RETIRING
                        worker.replacement_reason = "stop_pipe_closed"
                return
            self._retire_worker(
                worker,
                f"helper_pipe_lost:{type(exc).__name__}",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
            return
        if kind == "ready":
            self._handle_ready(worker, payload)
        elif kind == "startup_progress":
            self._handle_startup_progress(worker, payload)
        elif kind == "slice_phase":
            self._handle_slice_phase(worker, payload)
        elif kind == "endpoint_result":
            self._handle_endpoint_result(worker, payload)
        elif kind == "clean_confirmed":
            self._handle_clean_confirmed(worker, payload)
        elif kind == "heartbeat":
            self._handle_heartbeat(worker, payload)
        elif kind in {"bootstrap_error", "helper_error"}:
            self._log("error", "Tier3 validator helper failed: %s", payload.get("error"))
            self._retire_worker(
                worker,
                f"helper_error:{payload.get('error_type') or 'unknown'}:{payload.get('error') or ''}",
                terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
            )
        else:
            self._retire_worker(
                worker,
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
        """Consume the bounded control burst already queued for one helper."""

        handled = 0
        while handled < max(1, int(max_messages)):
            with self._lock:
                current = self._workers.get(worker.worker_id)
                if current is not worker or worker.phase in {
                    Tier3WorkerPhase.QUARANTINED,
                    Tier3WorkerPhase.STOPPED,
                }:
                    break
            try:
                if handled == 0:
                    first_message_ready = (
                        self._pipe_wait.release(worker.connection) or first_message_ready
                    )
                ready = (
                    (handled == 0 and first_message_ready)
                    or _pipe_has_queued_message(worker.connection)
                )
            except (EOFError, BrokenPipeError, OSError):
                # Route the failure through the ordinary Pipe lifecycle once.
                self._handle_pipe(worker)
                handled += 1
                break
            if not ready:
                break
            self._handle_pipe(worker)
            handled += 1
        return handled

    def _check_deadlines(self) -> None:
        now = time.monotonic()
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            deadline = 0.0
            if worker.phase is Tier3WorkerPhase.WAITING_START:
                if not worker.bootstrap_ready:
                    deadline = worker.started_at + TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS
            elif worker.phase is Tier3WorkerPhase.STARTING:
                deadline = float(worker.startup_deadline_at or 0.0)
                if deadline <= 0.0:
                    deadline = worker.started_at + self.start_timeout_seconds
            elif worker.phase is Tier3WorkerPhase.VALIDATING and worker.command is not None:
                deadline = worker.deadline_at
            elif worker.phase is Tier3WorkerPhase.CANCELLING:
                deadline = worker.cancel_deadline_at
            if deadline > 0.0 and now >= deadline:
                # A phase/result can arrive just after the batch wait. Check
                # that Pipe before enforcing its deadline, not every idle Pipe
                # on every sweep. The single reader and budgets are unchanged.
                self._drain_worker_pipe(worker)
                with self._lock:
                    if self._workers.get(worker.worker_id) is not worker:
                        continue
            if worker.phase is Tier3WorkerPhase.WAITING_START:
                if (
                    not worker.bootstrap_ready
                    and now - worker.started_at
                    > TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS
                ):
                    self._retire_worker(
                        worker,
                        "helper_bootstrap_timeout",
                        terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                    )
                continue
            if worker.phase is Tier3WorkerPhase.STARTING:
                if worker.prewarm_pending_pid:
                    self._confirm_prewarm_membership(worker)
                if worker.startup_phase == "membership_pending":
                    if self._confirm_ready_membership(worker):
                        continue
                    if now > float(worker.startup_deadline_at or 0.0):
                        self._retire_worker(
                            worker,
                            "job_membership_verification_failed",
                            terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                        )
                    continue
                startup_deadline = float(worker.startup_deadline_at or 0.0)
                if startup_deadline <= 0.0:
                    startup_deadline = worker.started_at + self.start_timeout_seconds
                if now > startup_deadline:
                    self._retire_worker(
                        worker,
                        f"helper_start_timeout:{worker.startup_phase or 'unknown'}",
                        terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                    )
                continue
            if worker.phase is Tier3WorkerPhase.VALIDATING and worker.command is not None:
                if worker.deadline_at > 0.0 and now >= worker.deadline_at:
                    command = worker.command
                    if worker.deadline_phase == "dispatch_ack":
                        cancel_reason = "validator_dispatch_ack_timeout"
                    elif worker.deadline_phase == "facility_start":
                        cancel_reason = "validator_facility_start_timeout"
                    elif worker.deadline_phase == "facility":
                        cancel_reason = "validator_facility_deadline"
                    else:
                        cancel_reason = "candidate_probe_deadline"
                    worker.sealed_lease_id = command.lease_id
                    worker.cancel_reason = cancel_reason
                    worker.cancel_requested_monotonic_ns = time.monotonic_ns()
                    worker.phase = Tier3WorkerPhase.CANCELLING
                    worker.cancel_deadline_at = now + self.cleanup_seconds
                    self._send(
                        worker,
                        "cancel",
                        dataclass_payload(
                            CancelCmd(
                                refresh_epoch=command.refresh_epoch,
                                lease_id=command.lease_id,
                                worker_id=worker.worker_id,
                                worker_generation=worker.generation,
                                sequence=command.sequence,
                                reason=cancel_reason,
                            )
                        ),
                    )
            elif worker.phase is Tier3WorkerPhase.CANCELLING:
                if worker.cancel_deadline_at > 0.0 and now >= worker.cancel_deadline_at:
                    cleanup_phase = str(worker.deadline_phase or "unknown")
                    terminal_kind = (
                        Tier3TerminalKind.FACILITY_FAIL
                        if worker.transport_started_at <= 0.0
                        else Tier3TerminalKind.CENSORED
                    )
                    self._retire_worker(
                        worker,
                        f"{cleanup_phase}_deadline_cleanup_timeout",
                        terminal_kind=terminal_kind,
                    )

    def _process_controls(self) -> None:
        while True:
            try:
                kind, payload = self._control.get_nowait()
            except queue.Empty:
                return
            if kind == "desired":
                with self._lock:
                    self._desired_workers = max(
                        self.min_workers,
                        min(self.max_workers, int(payload)),
                    )
            elif kind == "shell_prewarm":
                with self._lock:
                    self._shell_prewarm_workers = max(
                        self.min_workers,
                        min(self.max_workers, int(payload)),
                    )
            elif kind == "abort":
                worker_id, generation, reason = payload
                with self._lock:
                    worker = self._workers.get(worker_id)
                    if (
                        worker is not None
                        and worker.generation == generation
                        and worker.phase is Tier3WorkerPhase.VALIDATING
                    ):
                        # The supervisor owns the complete deadline state
                        # machine.  An external liveness request may expedite
                        # its normal cooperative cancellation, but must never
                        # retire a generation directly: the request can race
                        # with _check_deadlines() after a stale VALIDATING
                        # snapshot and otherwise steals the helper's cleanup
                        # entitlement.
                        worker.deadline_at = min(
                            worker.deadline_at or time.monotonic(),
                            time.monotonic(),
                        )
                        worker.replacement_reason = f"watchdog:{reason}"
            elif kind == "stop":
                self._stopping.set()

    def _stop_all(self) -> None:
        self._publish_stop_stage("owner_entered")
        self._pipe_wait.close()
        deadline = self._stop_deadline_at or (
            time.monotonic() + self.job_empty_wait_seconds + 1.0
        )

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        self._starter.stop(min(0.5, remaining()))
        self._drain_spawn_results(discard=True)
        with self._lock:
            self._pending_spawns.clear()

        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop_requested = True
            if worker.phase is not Tier3WorkerPhase.WAITING_START:
                self._send(worker, "stop", {})
        self._publish_stop_stage(f"owner_stop_sent:{len(workers)}")
        cooperative_deadline = time.monotonic() + min(0.5, remaining())
        while time.monotonic() < cooperative_deadline:
            alive = [
                worker for worker in workers if _process_is_alive(worker.process)
            ]
            if not alive:
                break
            waitables: list[Any] = []
            waitable_owner: dict[Any, tuple[_WorkerGeneration, str]] = {}
            for worker in alive:
                if not worker.connection.closed:
                    waitables.append(worker.connection)
                    waitable_owner[worker.connection] = (worker, "connection")
                sentinel = _process_sentinel(worker.process)
                if sentinel is not None:
                    waitables.append(sentinel)
                    waitable_owner[sentinel] = (worker, "sentinel")
            if not waitables:
                break
            ready = multiprocessing.connection.wait(waitables, timeout=0.05)
            for item in ready:
                owner = waitable_owner.get(item)
                if owner is None:
                    continue
                worker, kind = owner
                if kind == "connection":
                    self._handle_pipe(worker)

        with self._lock:
            remaining_workers = list(self._workers.values())
            for worker in remaining_workers:
                worker.phase = Tier3WorkerPhase.RETIRING
                worker.replacement_reason = (
                    worker.replacement_reason or "supervisor_stop"
                )
        owner_verification_jobs: dict[tuple[int, int], WindowsJob] = {}
        for worker in remaining_workers:
            try:
                owner_verification_jobs[
                    (worker.worker_id, worker.generation)
                ] = worker.job.duplicate()
            except OSError:
                continue
        self._publish_stop_stage(
            f"owner_cooperative_complete:{len(remaining_workers)}"
        )

        # Terminate every non-empty Job first, then wait for the whole set.
        # Waiting inside _retire_worker made shutdown O(worker_count).
        jobs_to_terminate: list[WindowsJob] = []
        for worker in remaining_workers:
            try:
                snapshot = worker.job.snapshot()
            except OSError:
                snapshot = None
            if _process_is_alive(worker.process) or (
                snapshot is not None and snapshot.active_processes > 0
            ):
                jobs_to_terminate.append(worker.job)
        self._forced_job_termination_count += len(jobs_to_terminate)
        self._publish_stop_stage(
            f"owner_job_termination_started:{len(jobs_to_terminate)}"
        )
        termination_completed = _terminate_jobs_bounded(
            jobs_to_terminate,
            exit_code=74,
            timeout_seconds=min(0.35, remaining()),
            close_after=False,
        )
        self._publish_stop_stage(
            "owner_job_termination_completed"
            if termination_completed
            else "owner_job_termination_incomplete"
        )

        job_deadline = time.monotonic() + min(
            self.job_empty_wait_seconds,
            max(0.0, remaining() - 0.25),
        )
        job_empty: dict[tuple[int, int], bool] = {}
        while time.monotonic() < job_deadline:
            pending = False
            for worker in remaining_workers:
                key = (worker.worker_id, worker.generation)
                if job_empty.get(key):
                    continue
                try:
                    snapshot = worker.job.snapshot()
                    empty = (
                        snapshot.active_processes <= 0
                        and not snapshot.process_ids
                    )
                except OSError:
                    empty = False
                job_empty[key] = empty
                pending = pending or not empty
            if not pending:
                break
            time.sleep(0.025)

        # The final Job/PID evidence below owns the stop verdict.  Keep an
        # incomplete termination helper as a diagnostic event only; treating
        # it as an irreversible failure produced false quarantine under load
        # after every owned process had already exited.
        for worker in remaining_workers:
            # Physical stop must not synchronously call actor, terminal or
            # diagnostic observers. The actor owns transaction cancellation in
            # its own stop event; this owner only clears OS resources.
            worker.command = None
            self._remove_worker(
                worker,
                join_timeout=0.0,
                close_process_handle=False,
            )
        self._publish_stop_stage(f"owner_registry_cleared:{len(remaining_workers)}")

        owner_job_empty: dict[tuple[int, int], bool] = {}
        verification_deadline = time.monotonic() + min(
            self.job_empty_wait_seconds,
            remaining(),
        )
        while time.monotonic() < verification_deadline:
            pending = False
            for key, job in owner_verification_jobs.items():
                if owner_job_empty.get(key):
                    continue
                try:
                    snapshot = job.snapshot()
                    empty = (
                        snapshot.active_processes <= 0
                        and not snapshot.process_ids
                    )
                except OSError:
                    empty = False
                owner_job_empty[key] = empty
                pending = pending or not empty
            if not pending:
                break
            time.sleep(0.01)
        self._publish_stop_stage(
            "owner_job_verification:"
            f"{sum(1 for empty in owner_job_empty.values() if empty)}/"
            f"{len(owner_verification_jobs)}"
        )
        for job in owner_verification_jobs.values():
            job.close()

        # Closing a KILL_ON_JOB_CLOSE handle is the final physical boundary.
        # If a helper Process still reports alive after its owned Job is empty,
        # terminate that exact process handle as a second independent boundary.
        # This covers a rare Windows assignment/exit race without weakening the
        # empty-Job requirement or waiting once per worker.
        direct_kill_count = 0
        for worker in remaining_workers:
            key = (worker.worker_id, worker.generation)
            if not owner_job_empty.get(key, job_empty.get(key, False)):
                continue
            if not _process_is_alive(worker.process):
                continue
            try:
                worker.process.kill()
                direct_kill_count += 1
            except (OSError, AssertionError, ValueError):
                pass
        if direct_kill_count:
            self._forced_job_termination_count += direct_kill_count
        self._publish_stop_stage(f"owner_direct_process_kill:{direct_kill_count}")
        # An empty Job can precede process-sentinel completion. Use the shared
        # deadline, not an earlier independent cap; retain a small finalization
        # margin and never wait once per worker.
        process_wait_seconds = max(0.0, remaining() - 0.05)
        self._publish_stop_stage(
            f"owner_process_wait_budget_ms:{int(process_wait_seconds * 1000)}"
        )
        process_deadline = time.monotonic() + process_wait_seconds
        pending_sentinels: dict[Any, _WorkerGeneration] = {}
        for worker in remaining_workers:
            if not _process_is_alive(worker.process):
                continue
            sentinel = _process_sentinel(worker.process)
            if sentinel is not None:
                pending_sentinels[sentinel] = worker
        while pending_sentinels and time.monotonic() < process_deadline:
            ready = multiprocessing.connection.wait(
                list(pending_sentinels),
                timeout=min(0.05, max(0.0, process_deadline - time.monotonic())),
            )
            for sentinel in ready:
                worker = pending_sentinels.pop(sentinel, None)
                if worker is None:
                    continue
                try:
                    worker.process.join(timeout=0.0)
                except (AssertionError, ValueError):
                    pass
        job_failed = False
        process_handle_lag_count = 0
        alive_process_evidence: list[str] = []
        for worker in remaining_workers:
            key = (worker.worker_id, worker.generation)
            alive = _process_is_alive(worker.process)
            if alive:
                process_handle_lag_count += 1
                alive_process_evidence.append(
                    f"{worker.worker_id}.{worker.generation}:"
                    f"{int(worker.process.pid or 0)}:"
                    f"{worker.process.exitcode}"
                )
            empty = owner_job_empty.get(key, job_empty.get(key, False))
            job_failed = job_failed or not bool(empty)
            if not alive:
                _close_stopped_process(worker.process)
        self._publish_stop_stage(
            "owner_final_evidence:"
            f"job_failed={int(job_failed)}:"
            f"process_handle_lag={process_handle_lag_count}:"
            f"alive={','.join(alive_process_evidence) or 'none'}"
        )
        # Process.start may still be running outside the published worker map.
        # Its result must be disposed before an empty registry can mean stopped.
        self._starter.join(remaining())
        self._drain_spawn_results(discard=True)
        starter_stopped = self._starter.is_quiescent()
        self._stop_unconfirmed_processes = tuple(
            worker.process for worker in remaining_workers
            if _process_is_alive(worker.process)
        )
        self._publish_stop_stage(f"owner_starter_stopped:{int(starter_stopped)}")
        self._stop_status = (
            "STOP_FAILED_QUARANTINED"
            if job_failed or self._stop_unconfirmed_processes or not starter_stopped
            else "STOPPED"
        )
        self._publish_stop_stage(f"owner_return:{self._stop_status}")

    def _run(self) -> None:
        self._running.set()
        self._stop_status = "RUNNING"
        try:
            while not self._stopping.is_set():
                # Clear before consuming controls so a concurrent spawn result
                # remains a wakeup for the no-Pipe startup wait below.
                self._wake.clear()
                self._process_controls()
                self._reap_retiring_workers()
                self._assign_pending()
                self._start_needed_workers()
                with self._lock:
                    workers = list(self._workers.values())
                waitables: list[Any] = []
                owner: dict[Any, _WorkerGeneration] = {}
                for worker in workers:
                    if worker.phase in {
                        Tier3WorkerPhase.QUARANTINED,
                        Tier3WorkerPhase.STOPPED,
                    }:
                        continue
                    if not worker.connection.closed:
                        waitables.append(worker.connection)
                        owner[worker.connection] = worker
                    sentinel = _process_sentinel(worker.process)
                    if sentinel is not None:
                        waitables.append(sentinel)
                        owner[sentinel] = worker
                if waitables:
                    ready = self._pipe_wait.wait(waitables, timeout=0.05)
                else:
                    self._wake.wait(0.05)
                    ready = []
                # A helper can queue heartbeat, facility_started and
                # transport_started while this Python process is briefly
                # descheduled.  Drain those ordered messages before enforcing
                # the parent deadline; otherwise a queued acknowledgement can
                # be mistaken for a dispatch timeout and kill a healthy Job.
                ready_connections = [
                    item
                    for item in ready
                    if (
                        (worker := owner.get(item)) is not None
                        and item is worker.connection
                    )
                ]
                for item in ready_connections:
                    worker = owner.get(item)
                    if worker is None:
                        continue
                    self._drain_worker_pipe(worker, first_message_ready=True)

                for item in ready:
                    worker = owner.get(item)
                    if worker is None or item is worker.connection:
                        continue
                    with self._lock:
                        current = self._workers.get(worker.worker_id)
                    if current is worker and not _process_is_alive(worker.process):
                        self._retire_worker(
                            worker,
                            f"helper_exit:{worker.process.exitcode}",
                            terminal_kind=Tier3TerminalKind.FACILITY_FAIL,
                        )
                self._check_deadlines()
        finally:
            try:
                self._pipe_wait.close()
            finally:
                self._stop_all()
                self._running.clear()


class Tier3ValidatorWatchdog:
    """Independent liveness observer; it never acquires manager business locks."""

    def __init__(
        self,
        supervisor: Tier3ValidatorSupervisor,
        *,
        backlog_depth: Callable[[], int],
        logger: Any | None = None,
        interval_seconds: float = 0.5,
        max_completion_gap_seconds: float = 14.5,
    ) -> None:
        self.supervisor = supervisor
        self.backlog_depth = backlog_depth
        self.logger = logger
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.max_completion_gap_seconds = max(1.0, float(max_completion_gap_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="tier3-validator-watchdog-v2",
            daemon=False,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_seconds)))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._inspect_once()

    def _inspect_once(self, *, now: float | None = None) -> None:
        snapshot = self.supervisor.snapshot()
        backlog = max(0, int(self.backlog_depth()))
        if backlog <= 0:
            return
        if snapshot.idle_workers > 0:
            self.supervisor.wake()
        current = time.monotonic() if now is None else float(now)
        # The supervisor checks these same deadlines every 50ms and owns both
        # lease sealing and cooperative cleanup.  The watchdog may wake that
        # owner, but a snapshot-derived abort command is inherently stale and
        # can arrive after the worker has already entered CANCELLING.
        if any(
            str(worker.get("phase") or "")
            == Tier3WorkerPhase.VALIDATING.value
            and float(worker.get("deadline_at", 0.0) or 0.0) > 0.0
            and current
            > float(worker.get("deadline_at", 0.0) or 0.0) + 0.5
            for worker in snapshot.workers
        ):
            self.supervisor.wake()
        if (
            snapshot.validating_workers <= 0
            and current - snapshot.last_completion_monotonic
            > self.max_completion_gap_seconds
        ):
            self.supervisor.wake()
