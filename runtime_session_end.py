"""RT-2: irreversible admission, revocation and bounded owned recovery.

Only relative budgets cross the bridge. A timed-out worker retains its gate
until it really exits; neither timeout nor an idle observation releases a lease.
"""
from __future__ import annotations

from copy import deepcopy
import threading
import time


class RecoveryBarrier:
    """Close admission atomically with counting all admitted transactions."""

    def __init__(self):
        self._condition = threading.Condition()
        self._closed = False
        self._active = 0

    def enter(self):
        with self._condition:
            if self._closed:
                return False
            self._active += 1
            return True

    def leave(self):
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def require_idle(self):
        with self._condition:
            if self._active:
                raise RuntimeError("emergency recovery is still running")

    def close_and_wait(self, remaining):
        with self._condition:
            self._closed = True
            return self._condition.wait_for(lambda: not self._active, max(0., remaining))


def domain(state="unknown", code="deadline_exceeded", count=None):
    return dict(state=state, error_code=code, remaining_count=count)


class SessionEnd:
    def __init__(self, shutdown, *, may_take_over_host_network=True):
        self.shutdown = shutdown
        self.runtime = shutdown.access.runtime
        self.allowed = may_take_over_host_network
        self.gate = RecoveryBarrier()
        self._lock = threading.Lock()
        self.started = False
        self._received_deadline = None
        self.done = threading.Event()
        self._result = dict(writes_revoked=False,
                            recovery={key: domain() for key in ("proxy", "firewall")},
                            workers_pending=True)

    def snapshot(self):
        with self._lock:
            return deepcopy(self._result)

    def received(self, budget_ms):
        """Raw-reader timestamp only: no lock, intent, I/O or worker callback.

        The single pipe reader supplies the first validated current-epoch
        request. Duplicates never replenish its budget while it is queued.
        """
        if self._received_deadline is None:
            self._received_deadline = time.monotonic() + budget_ms / 1000.

    @staticmethod
    def wire(result):
        # v1 Recovery has per-domain enums; bye retains the diagnostic records.
        return {**result, "recovery": {key: value["state"]
                                      for key, value in result["recovery"].items()}}

    def _record(self, key, value):
        if value.get("state") not in {
                "not_needed", "restored", "not_owned", "permission_required", "failed", "unknown"}:
            value = domain(code=value.get("error_code") or "recovery_unconfirmed")
        with self._lock:
            self._result["recovery"][key] = {
                name: value.get(name) for name in ("state", "error_code", "remaining_count")}

    def handle(self, args, context=None):
        # A duplicate never gets a fresh budget, worker or credential.
        deadline = self._received_deadline
        if deadline is None:  # Direct in-process invocation has no ingress queue.
            deadline = time.monotonic() + args["budget_ms"] / 1000.
        with self._lock:
            if self.started or not self.gate.enter():
                return self.wire(deepcopy(self._result))
            self.started = True
        self.shutdown._quitting.set()
        # P2 executor has no public admission-only primitive yet (P2-03 SHOULD).
        # Do not use begin_shutdown/prepare_quit: they cancel this live res.
        with self.shutdown.executor._lock:
            self.shutdown.executor._quitting = True
        worker = threading.Thread(target=self._work, args=(deadline,),
                                  name="runtime-session-end", daemon=True)
        try:
            worker.start()
        except Exception:
            self.gate.leave()
            self.done.set()
        self.done.wait(max(0., deadline - time.monotonic()))
        result = self.snapshot()
        # Ordinary cleanup may block; submission only uses the short executor
        # boundary. Its worker cannot cancel the pending sessionEnd response.
        self.shutdown.request_quit()
        return self.wire(result)

    def _work(self, deadline):
        from killswitch import revoke_killswitch_writes, emergency_restore_firewall
        from system_proxy import revoke_system_proxy_writes
        expired = lambda: time.monotonic() >= deadline
        try:
            if expired():
                return
            self.runtime.begin_session_end()
            proxy_credential = revoke_system_proxy_writes()["recovery_credential"]
            ks = getattr(self.runtime, "kill_switch", None)
            firewall_credential = revoke_killswitch_writes(ks)
            with self._lock:
                self._result["writes_revoked"] = True
            if not self.allowed:
                for key in ("proxy", "firewall"):
                    self._record(key, domain("not_needed", "scope_isolated", 0))
            elif self.shutdown._network_recovery_serial.acquire(timeout=max(0., deadline - time.monotonic())):
                try:
                    if not expired():
                        value = self.runtime.system_proxy.emergency_restore_transaction(
                            self.runtime.runtime_state, proxy_credential, expired=expired)
                        if not expired():
                            self._record("proxy", value)
                    if not expired():
                        value = emergency_restore_firewall(
                            firewall_credential, manager=ks, deadline=deadline)
                        if not expired():
                            self._record("firewall", value)
                finally:
                    self.shutdown._network_recovery_serial.release()
            if not expired():
                self.runtime.begin_session_end(request_core_exit=True)
        except Exception:
            # Unknown is deliberately retained: no speculative success from a
            # missing receipt, failed intent or backend exception.
            pass
        finally:
            self.gate.leave()
            self.done.set()

    def report(self):
        from runtime_network_recovery import RecoveryReport
        return RecoveryReport(self.snapshot()["recovery"], self.gate)
