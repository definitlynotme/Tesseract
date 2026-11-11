"""P1 control-domain stop and owner-thread sidecar shutdown coordination.

The read loop only submits jobs and sends the resolved stop-intent ack. The
instance lease is released by the thread that acquired it, after all recovery
gates prove that no earlier network writer remains active.

Full exit (T-P2-31b), in this order:
1. refuse new work: the first app.quit closes admission (executor
   prepare_quit / begin_shutdown);
2. ordered stop: the app.quit job (the runtime's app_exit stop);
3. close every business owner (``close_owners``: node and subscription
   diagnostics, pairing polls, exit scheduling, desktop receipts, export
   grants, deferred apply) on a helper thread, bounded; with no service
   running at quit, ``runtime.alerts.close(timeout=3)`` as Qt does.  An
   outer cap (``owner_close_seconds`` + 3.5 s from the helper's start)
   abandons a helper that ignores its budget (it is a daemon thread); once
   RT-2 (app.sessionEnd) has started, owners close without waits and the
   cap shrinks to ``SESSION_END_OWNER_GRACE`` so bye is not delayed;
4. network recovery report (shared final barrier with RT-2), then the log /
   traffic / sampler producers and executor workers (``close_diagnostics``)
   so the recovery's own log lines still reach the shell, then the final
   publication drain;
5. ``bye`` as the very last frame; exit code 74 semantics unchanged.
``on_phase`` reports the shell-only ``shutdown`` topic phases.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from bridge_jobs import Ack, JobContext, JobExecutor, StopIntent
from bridge_runtime_access import RuntimeAccess, StopRequest, frozen_record

_LOG = logging.getLogger("tesseract.bridge.shutdown")
# Step 3 outer cap beyond owner_close_seconds: alerts.close(timeout=3) + grace.
OWNER_CLOSE_EXTRA_SECONDS = 3.5
# Once RT-2 started, the owner helper gets at most this long (Windows logoff).
SESSION_END_OWNER_GRACE = 0.5


def unknown_recovery() -> dict[str, dict[str, Any]]:
    return {name: {"state": "unknown", "error_code": None, "remaining_count": None}
            for name in ("proxy", "firewall")}


class ShutdownCoordinator:
    """Own the four shutdown triggers without moving the instance lease.

    ``finish_quit`` must be called on the lease-acquiring thread. A False
    return means cleanup is still active: the caller must retain its lease
    and retry, or keep this process alive until it can do so.
    """

    def __init__(
        self, executor: JobExecutor, access: RuntimeAccess, *,
        lease: Any, startup_recovery: Any,
        recover_network: Callable[[float], Any],
        send: Callable[[dict[str, Any]], Any],
        exit_process: Callable[[int], Any],
        poll_write: Callable[[], str] | None = None,
        close_diagnostics: Callable[[float], bool] | None = None,
        drain_publications: Callable[[], bool] | None = None,
        close_owners: Callable[[float], bool] | None = None,
        on_phase: Callable[..., Any] | None = None,
        owner_close_seconds: float = 4.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if lease is None or not hasattr(startup_recovery, "gate"):
            raise ValueError("owned lease and original startup recovery gate required")
        self.executor = executor
        self.access = access
        self.lease = lease
        self.startup_recovery = startup_recovery
        self.recover_network = recover_network
        self.send = send
        self.poll_write = poll_write
        self.exit_process = exit_process
        self.close_diagnostics = close_diagnostics
        self.drain_publications = drain_publications
        self.close_owners = close_owners
        self.on_phase = on_phase
        self.owner_close_seconds = float(owner_close_seconds)
        self._owners_thread: threading.Thread | None = None
        self._owners_done = threading.Event()
        self._owners_started_at: float | None = None
        self._owners_abandoned = False
        # Lifecycle at the first quit intent: Qt closes pending alert
        # deliveries itself when no service was running (the runtime's
        # app_exit stop closes them otherwise).
        self._service_running_at_quit: bool | None = None
        self.monotonic = monotonic
        self.wall_ms = wall_ms
        self._lock = threading.Lock()
        self._quitting = threading.Event()
        self._quit_ack: Ack | None = None
        self._disconnected = False
        self._fatal = False
        self._recovery_report: Any = None
        self._recovery_thread: threading.Thread | None = None
        self._recovery_done = threading.Event()
        self._recovery_error: BaseException | None = None
        self._finished = False
        self._bye_frame: dict[str, Any] | None = None
        self._bye_pending = False
        self._bye_submitted = False
        self._network_recovery_serial = threading.Lock()
        from runtime_session_end import SessionEnd
        self.session_end = SessionEnd(self)
        executor.register("service.stop", self._stop, stop_intent=self._stop_intent)
        executor.register("app.quit", self._quit_stop, stop_intent=self._quit_intent)

    def _await_startup_idle(self) -> None:
        while True:
            try:
                self.startup_recovery.gate.require_idle()
                return
            except Exception:
                time.sleep(.01)

    def _stop_intent(self, args: dict[str, Any], context: JobContext) -> StopIntent:
        self._await_startup_idle()
        request = self.access.request_stop(expected_generation=args.get("expected_generation"))
        wire = request.wire
        reason = str(wire["skipped_reason"] or "") or None
        if reason == "stale_runtime_generation":
            reason = "conflict"
        return StopIntent(bool(wire["accepted"]),
                          reason, request)

    def _stop(self, _args: dict[str, Any], context: JobContext) -> dict[str, Any]:
        intent = context.intent
        if intent is None or not isinstance(intent.token, StopRequest):
            raise RuntimeError("accepted stop has no runtime intent")
        return self.access.stop(stop_request=intent.token)

    def _quit_intent(self, args: dict[str, Any], context: JobContext) -> StopIntent:
        # Submit(app.quit) permanently closes non-control admission. Keep the
        # control worker available until an intent exists so quit can retry.
        self._await_startup_idle()
        if self.session_end.started:
            # A timed-out RT-2 OS call still owns recovery. Ordinary stop must
            # not request Core termination ahead of that recovery transaction.
            self.session_end.done.wait()
        if self._service_running_at_quit is None:
            try:
                status = self.access.lifecycle_status()
                self._service_running_at_quit = bool(status["runtime"]["runtime_running"])
            except Exception:
                self._service_running_at_quit = True   # unknown: leave it to stop
        # RuntimeAccess.request_stop has no actor parameter. The existing
        # runtime assigns exit code 74 only to an app_exit intent, so use its
        # public request_stop boundary here and retain that exact intent.
        raw = self.access.runtime.request_stop(actor="app_exit")
        request = StopRequest(raw, frozen_record(
            accepted=bool(raw.accepted), generation=int(raw.generation),
            lifecycle_revision=int(raw.lifecycle_revision),
            skipped_reason=str(raw.skipped_reason or "")))
        if bool(request.wire["accepted"]) and not self.session_end.started:
            self.executor.begin_shutdown()
        return StopIntent(bool(request.wire["accepted"]),
                          str(request.wire["skipped_reason"] or "") or None,
                          request)

    def _quit_stop(self, args: dict[str, Any], context: JobContext) -> dict[str, Any]:
        return self._stop(args, context)

    def request_stop(self, *, expected_generation: int | None = None) -> Ack:
        args = {} if expected_generation is None else {"expected_generation": expected_generation}
        return self.executor.submit("service.stop", args)

    def request_quit(self, *, disconnected: bool = False) -> Ack:
        # Permanent from the first quit attempt, including rejected/retried
        # intents and bye backpressure. Collection admission uses this fact.
        self._quitting.set()
        with self._lock:
            if disconnected:
                self._disconnected = True
            if (self._quit_ack is not None and self._quit_ack.op is not None
                    and self._quit_ack.wait_stop_intent(0) is not None
                    and self.executor.get_job(self._quit_ack.op) is None):
                self._quit_ack = None
            if self._quit_ack is None:
                self._quit_ack = self.executor.submit("app.quit", {})
                if self._quit_ack.accepted and not self.session_end.started:
                    self.executor.prepare_quit()
            return self._quit_ack

    @property
    def quitting(self) -> bool:
        return self._quitting.is_set()

    @property
    def bye_submitted(self) -> bool:
        """Permanent wire barrier, including an asynchronous pending write."""
        return self._bye_submitted

    def on_disconnect(self) -> Ack:
        """Suitable for PipeTransport.on_disconnect; does no blocking I/O."""
        return self.request_quit(disconnected=True)

    def on_fatal(self) -> Ack:
        """Read/publish exception enters the same coordinator as normal quit."""
        with self._lock:
            self._fatal = True
        return self.request_quit()

    def _phase(self, phase: str, *, workers_pending: bool = True) -> None:
        if self.on_phase is None:
            return
        try:
            self.on_phase(phase, self._recovery_report, workers_pending=workers_pending)
        except Exception:
            pass

    def _run_owner_close(self) -> None:
        # Logoff (RT-2 started): skip every wait; the report and bye come first.
        hurried = bool(getattr(self.session_end, "started", False))
        try:
            if self._service_running_at_quit is False:
                alerts = getattr(self.access.runtime, "alerts", None)
                close = getattr(alerts, "close", None)
                if callable(close):
                    try:
                        close(timeout=0.0 if hurried else 3.0)
                    except Exception:
                        pass
            if self.close_owners is not None:
                try:
                    self.close_owners(0.0 if hurried else self.owner_close_seconds)
                except Exception:
                    pass
        finally:
            self._owners_done.set()

    def _owner_close_cap(self) -> float:
        cap = self.owner_close_seconds + OWNER_CLOSE_EXTRA_SECONDS
        if getattr(self.session_end, "started", False):
            cap = min(cap, SESSION_END_OWNER_GRACE)
        return cap

    def _owners_closed(self, deadline: float) -> bool:
        """Step 3 wait: True once the helper finished or exceeded its cap."""
        if self._owners_done.is_set() or self._owners_abandoned:
            return True
        started = self._owners_started_at if self._owners_started_at is not None else self.monotonic()
        give_up = started + self._owner_close_cap()
        now = self.monotonic()
        if self._owners_done.wait(max(0.0, min(deadline, give_up) - now)):
            return True
        if self.monotonic() < give_up:
            return False
        # An owner ignored its budget: leave the daemon helper behind.
        self._owners_abandoned = True
        _LOG.warning("owner close exceeded its cap; continuing the exit")
        return True

    def _run_recovery(self, budget: float) -> None:
        deadline = time.monotonic() + budget
        try:
            with self._network_recovery_serial:
                if self.session_end.started:
                    self._recovery_report = self.session_end.report()
                else:
                    report = self.recover_network(max(0., deadline - time.monotonic()))
                    if not hasattr(report, "gate"):
                        raise TypeError("network recovery must retain its original gate")
                    self._recovery_report = report
                    self._recovery_done.set()
                    # Keep RT-2 behind an already-started ordinary OS recovery,
                    # even when its bounded report has returned unknown.
                    while True:
                        try:
                            report.gate.require_idle()
                            break
                        except RuntimeError:
                            time.sleep(.005)
        except BaseException as exc:
            self._recovery_error = exc
        finally:
            self._recovery_done.set()

    def finish_quit(self, *, deadline_seconds: float = 5.0) -> bool:
        """Complete quit on the owner thread; never release on an active gate.

        A deadline bounds waits, not an already-running runtime stop or a
        recovery backend's commit. A caller gets False and retains ownership
        when a stage is still active at the deadline.
        """
        if deadline_seconds < 0:
            raise ValueError("deadline_seconds must be nonnegative")
        deadline = self.monotonic() + deadline_seconds
        with self._lock:
            ack = self._quit_ack
            if self._finished:
                return True
        if self.bye_submitted:
            return self._finish_bye(deadline, self._bye_frame["exit_code"])
        if ack is None or not ack.accepted or ack.op is None:
            return False
        self._phase("draining")
        job = self.executor.wait(ack.op, max(0.0, deadline - self.monotonic()))
        if job is None:
            # The executor removes rejected provisional jobs. A later owner
            # attempt may submit again; this call stays bounded.
            if ack.wait_stop_intent(0) is not None:
                with self._lock:
                    if self._quit_ack is ack:
                        self._quit_ack = None
                if self.monotonic() < deadline:
                    self.request_quit()
            return False
        # A rejected stop intent means the runtime did not stop. Neither a
        # report nor a bye may claim that process cleanup completed.
        if job["state"] not in {"done", "failed"}:
            self._phase("stopping")
            return False
        # Step 3: business owners close before the recovery report (bounded;
        # each owner bounds its own waits).
        if self._owners_thread is None:
            self._owners_started_at = self.monotonic()
            self._owners_thread = threading.Thread(target=self._run_owner_close,
                                                   name="bridge-owner-close", daemon=True)
            self._owners_thread.start()
        if not self._owners_closed(deadline):
            return False
        try:
            self.startup_recovery.gate.require_idle()
        except Exception:
            return False
        if self._recovery_thread is None:
            self._phase("recovering")
            self._recovery_thread = threading.Thread(
                target=self._run_recovery,
                args=(max(0.0, deadline - self.monotonic()),),
                name="bridge-network-recovery", daemon=True)
            self._recovery_thread.start()
        if not self._recovery_done.wait(max(0.0, deadline - self.monotonic())):
            return False
        if self._recovery_error is not None:
            return False
        try:
            self._recovery_report.gate.require_idle()
        except Exception:
            return False
        self._phase("waiting_workers")
        if self.close_diagnostics is not None:
            try:
                if self.close_diagnostics(max(0.0, deadline - self.monotonic())) is False:
                    return False
            except Exception:
                return False
        self._phase("finished", workers_pending=False)
        if not self._disconnected and self.drain_publications is not None:
            if not self.drain_publications():
                return False
        if not self._close_recovery_entries(deadline):
            return False
        if self.session_end.started:
            self._recovery_report = self.session_end.report()
        result = job.get("result") or {}
        code = result.get("shutdown_exit_code")
        exit_code = 74 if (code == 74 or result.get("cleanup_pending")
                           or result.get("startup_phase") == "failed") else (
            0 if job["state"] == "done" and not self._fatal else 1)
        return self._finish_bye(deadline, exit_code)

    def _close_recovery_entries(self, deadline: float) -> bool:
        # Closing and counting use the SAME locks as admission. In particular,
        # an idle observation in close_diagnostics grants no release authority.
        if not self.session_end.gate.close_and_wait(max(0., deadline - self.monotonic())):
            return False
        proxy = getattr(self.access.runtime, "system_proxy", None)
        gate = getattr(proxy, "emergency_gate", None)
        if gate is not None and not gate.close_and_wait(max(0., deadline - self.monotonic())):
            return False
        from killswitch import _FIREWALL_AUTHORITY
        if not _FIREWALL_AUTHORITY.gate.close_and_wait(max(0., deadline - self.monotonic())):
            return False
        # An old writer may have entered its OS call just before revocation,
        # while RT-2 exhausted its budget before opening a recovery transaction.
        # Revocation makes these idle checks stable against new ordinary writes.
        from system_proxy import _PROXY_WRITES_REVOKED, system_proxy_writes_idle
        if _PROXY_WRITES_REVOKED.is_set():
            while not system_proxy_writes_idle():
                if self.monotonic() >= deadline:
                    return False
                time.sleep(min(.005, max(0., deadline - self.monotonic())))
        with _FIREWALL_AUTHORITY.condition:
            return _FIREWALL_AUTHORITY.condition.wait_for(
                lambda: not _FIREWALL_AUTHORITY.in_flight,
                max(0., deadline - self.monotonic()))

    def _finish_bye(self, deadline: float, exit_code: int) -> bool:
        """Only finish the final write or disconnected cleanup after submission."""
        if not self._disconnected:
            if self._bye_frame is None:
                now = self.wall_ms()
                self._bye_frame = {"t": "bye", "epoch": self.executor.epoch,
                                   "produced_at_ms": now, "sent_at_ms": now,
                                   "exit_code": exit_code,
                                   "network_recovery": dict(self._recovery_report)}
            try:
                while not self._disconnected:
                    if self._bye_pending and self.poll_write is None:
                        return False
                    if not self._bye_pending:
                        self._bye_frame["sent_at_ms"] = self.wall_ms()
                    outcome = (self.poll_write() if self._bye_pending else
                               self.send(self._bye_frame))
                    if outcome in {"complete", "pending"}:
                        self._bye_submitted = True
                    if outcome == "complete":
                        self._bye_pending = False
                        break
                    if outcome == "pending":
                        self._bye_pending = True
                    elif outcome != "busy":
                        return False
                    if self.monotonic() >= deadline:
                        return False
                    time.sleep(min(.005, max(0.0, deadline - self.monotonic())))
            except (BrokenPipeError, ConnectionError):
                self._disconnected = True
            except OSError as exc:
                if getattr(exc, "winerror", None) in (109, 232, 233) or getattr(exc, "errno", None) in (109, 232, 233):
                    self._disconnected = True
                else:
                    return False
        self.lease.close()
        with self._lock:
            self._finished = True
        self.exit_process(exit_code)
        return True
