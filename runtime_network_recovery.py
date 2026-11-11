"""Bounded startup and runtime-exit recovery of a system proxy snapshot.

The caller must hold the single-instance lease. Startup needs no runtime object
or current proxy configuration: ownership comes from the old journal.
RT-1 owns the pre-runtime phase; exit recovery owns the proxy transaction lock
and uses the existing manager's revocable writer. Both retain their gate until
the worker actually exits, even when an OS operation outlives the deadline.
"""

from __future__ import annotations

import json
import os
import ctypes
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from killswitch import (
    recovery_command_runner, recovery_delete_owned_firewall_rule, recovery_firewall_rules,
    recovery_rule_identity_valid,
)
from runtime_state import RuntimeStateStore
from system_proxy import (
    ProxySettings, RegistryBackend, WinRegProxyBackend,
    _begin_proxy_rt1, _end_proxy_rt1, _proxy_rt1_worker, assert_proxy_journal_writer,
    _proxy_transaction, _PROXY_WRITE_INTENT_LOCK, _proxy_write_authorized,
    ProxyWritesRevoked, SystemProxyManager,
)


def _report(state: str, error_code: str | None = None, remaining_count: int | None = 0) -> dict[str, Any]:
    return {"state": state, "error_code": error_code, "remaining_count": remaining_count}


def _journal_error(path: Path, diagnostics: dict[str, Any], code: str, exc: BaseException | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path), "error_code": code}
    try:
        stat = path.stat()
        info.update(size=stat.st_size, modified_ns=stat.st_mtime_ns)
    except OSError:
        pass
    if isinstance(exc, OSError):
        info["errno"] = exc.errno
    if exc is not None:
        info["exception_type"] = type(exc).__name__
    diagnostics["journal"] = info
    return _report("unknown", code, None)


def _snapshot(payload: Any) -> tuple[str, ProxySettings] | None:
    if not isinstance(payload, dict):
        return None
    entry = payload.get("system_proxy")
    if not isinstance(entry, dict) or entry.get("active") is not True:
        return None
    # Old snapshots have neither owner nor phase. New snapshots retain the
    # same target/original shape so recovery never depends on today's port.
    owner, phase = entry.get("owner"), entry.get("phase")
    if owner is not None and (not isinstance(owner, str) or not owner.startswith("tesseract:") or len(owner) <= 10):
        return None
    if phase is not None and (not isinstance(phase, str) or phase not in {
        "prepared", "applied", "write_failed", "verify_failed", "unknown", "restoring",
    }):
        return None
    target = entry.get("target")
    original = entry.get("original")
    if not isinstance(target, str) or not target or not isinstance(original, dict):
        return None
    enabled = original.get("enabled")
    server = original.get("server")
    override = original.get("override")
    if not isinstance(enabled, bool) or not isinstance(server, str) or not isinstance(override, str):
        return None
    return target, ProxySettings(enabled=enabled, server=server, override=override)


class RecoveryStillRunning(RuntimeError):
    """A later host-network takeover must wait for the old recovery worker."""


class ProxyRecoveryGate:
    """Startup handoff gate; an uninterruptible registry call may outlive the deadline.

    The caller must call require_idle() immediately before any subsequent host
    network takeover. A deadline report alone does not grant that permission.
    """

    def __init__(self, stop_at: float) -> None:
        self.stop_at = stop_at
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._committing = False

    def expired(self) -> bool:
        return self.cancelled.is_set() or time.monotonic() >= self.stop_at

    def begin_commit(self) -> bool:
        with self._lock:
            if self.expired():
                return False
            self._committing = True
            return True

    def cancel_before_commit(self) -> bool:
        """Return False if an irreversible replace has already started."""
        with self._lock:
            if self._committing:
                return False
            self.cancelled.set()
            return True

    @property
    def committing(self) -> bool:
        with self._lock:
            return self._committing

    def require_idle(self) -> None:
        if not self.done.is_set():
            raise RecoveryStillRunning("startup proxy recovery is still running")


class RecoveryReport(dict):
    """Protocol-shaped result with a local-only startup handoff gate."""

    def __init__(self, value: dict[str, Any], gate: ProxyRecoveryGate) -> None:
        super().__init__(value)
        self.gate = gate


def _clear_proxy_entry(
    path: Path, original_bytes: bytes, payload: dict[str, Any],
    expired: Callable[[], bool], begin_commit: Callable[[], bool],
) -> bool:
    """Remove only the verified proxy entry, retaining unrelated journal data."""
    assert_proxy_journal_writer()
    if path.read_bytes() != original_bytes:
        return False
    remaining = dict(payload)
    remaining.pop("system_proxy")
    encoded = (json.dumps(remaining, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Defense in depth only, not a concurrent replacement/CAS guarantee.
        if expired() or path.read_bytes() != original_bytes:
            return False
        # read_bytes itself may cross the deadline. The final replace must be
        # serialized against the caller's timeout decision.
        if not begin_commit():
            return False
        os.replace(name, path)
        replaced = True
        return True
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        except OSError:
            if not replaced:
                raise


def recover_startup_system_proxy(
    store: RuntimeStateStore,
    *,
    may_take_over_host_network: bool,
    deadline_seconds: float = 2.0,
    backend: RegistryBackend | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pre-runtime RT-1 only; retain the returned gate until its worker is idle."""
    return _recover_system_proxy(
        store, may_take_over_host_network=may_take_over_host_network,
        deadline_seconds=deadline_seconds, backend=backend, diagnostics=diagnostics)


def _recover_system_proxy(
    store: RuntimeStateStore,
    *,
    may_take_over_host_network: bool,
    deadline_seconds: float = 2.0,
    backend: RegistryBackend | None = None,
    diagnostics: dict[str, Any] | None = None,
    proxy_manager: SystemProxyManager | None = None,
) -> RecoveryReport:
    """Return the protocol's ``network_recovery.proxy`` value.

    An isolated scope exits before opening the journal or constructing a
    registry backend. The returned gate must be checked before the caller
    starts any later host-network takeover. A blocked registry call cannot be
    cancelled; its gate remains closed until that call and worker finish. Once
    the final journal replace starts, a deadline returns commit_pending with
    the same gate. The caller must wait for the gate and recheck the journal
    and registry to learn the final outcome.
    """
    # RT-1 has no revocation bypass: a revoked/runtime process is an invalid
    # caller. Keep the phase alive even if this call times out before its worker.
    runtime = proxy_manager is not None
    phase = None if runtime else _begin_proxy_rt1(isolated=not may_take_over_host_network)
    gate = ProxyRecoveryGate(time.monotonic() + max(0.0, deadline_seconds))
    if not may_take_over_host_network:
        gate.done.set()
        return RecoveryReport(_report("not_needed", "scope_isolated"), gate)
    if diagnostics is None:
        diagnostics = {}
    if deadline_seconds <= 0:
        if not runtime:
            _end_proxy_rt1(phase)
        gate.done.set()
        return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)

    stop_at = gate.stop_at
    answers: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    expired = gate.expired

    def work() -> dict[str, Any]:
        path = store.path
        if expired():
            return _report("unknown", "deadline_exceeded", None)
        if runtime:
            # This ordinary exit transaction never bypasses RT-2 revocation.
            with _PROXY_WRITE_INTENT_LOCK:
                if not _proxy_write_authorized():
                    return _report("unknown", "writes_revoked", None)
            proxy_manager._bind_journal(store)
            if not proxy_manager.supported:
                return _report("unknown", "unsupported", None)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return _report("not_needed")
        except OSError as exc:
            return _journal_error(path, diagnostics, "journal_unreadable", exc)
        if expired():
            return _report("unknown", "deadline_exceeded", None)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            return _journal_error(path, diagnostics, "journal_corrupt", exc)
        if not isinstance(payload, dict):
            return _journal_error(path, diagnostics, "journal_corrupt")
        if "system_proxy" not in payload:
            return _report("not_needed")
        snapshot = _snapshot(payload)
        if snapshot is None:
            return _journal_error(path, diagnostics, "journal_corrupt")
        target, original = snapshot
        if expired():
            return _report("unknown", "deadline_exceeded", None)
        registry = (proxy_manager.backend if runtime else
                    backend if backend is not None else WinRegProxyBackend())
        try:
            current = registry.read()
        except Exception as exc:
            diagnostics["registry_error_type"] = type(exc).__name__
            return _report("unknown", "registry_read_failed", None)
        if expired():
            return _report("unknown", "deadline_exceeded", None)
        # A prepared write may never have started, or a prior recovery may
        # have restored already. Verify equality again before clearing either.
        if current != original:
            if not current.enabled or current.server != target:
                return _report("not_owned", "proxy_taken_over", 1)
            try:
                if runtime:
                    proxy_manager._write_settings(original)
                else:
                    registry.write(original)
            except ProxyWritesRevoked:
                return _report("unknown", "writes_revoked", None)
            except Exception as exc:
                diagnostics["registry_error_type"] = type(exc).__name__
                return _report("failed", "registry_write_failed", 1)
        if expired():
            return _report("unknown", "deadline_exceeded", None)
        try:
            verified = registry.read()
        except Exception as exc:
            diagnostics["registry_error_type"] = type(exc).__name__
            return _report("unknown", "registry_verify_failed", None)
        if expired():
            return _report("unknown", "deadline_exceeded", None)
        if verified != original:
            return _report("failed", "registry_verify_mismatch", 1)
        if runtime:
            with _PROXY_WRITE_INTENT_LOCK:
                if not _proxy_write_authorized():
                    return _report("unknown", "writes_revoked", None)
        try:
            if expired() or not _clear_proxy_entry(path, raw, payload, expired, gate.begin_commit):
                if expired():
                    return _report("unknown", "deadline_exceeded", None)
                return _report("failed", "journal_changed", 1)
        except OSError as exc:
            diagnostics["journal_clear_error_type"] = type(exc).__name__
            return _report("failed", "journal_clear_failed", 1)
        if runtime:
            proxy_manager.active = False
            proxy_manager.original_settings = None
            proxy_manager._journal_pending = False
        return _report("restored")

    def run() -> None:
        try:
            with (_proxy_transaction() if runtime else _proxy_rt1_worker(phase)):
                result = work()
        except Exception as exc:
            diagnostics["recovery_error_type"] = type(exc).__name__
            result = _report("failed", "recovery_internal_error", 1)
        finally:
            gate.done.set()
        answers.put_nowait(result)

    thread = threading.Thread(target=run, name="exit-proxy-recovery" if runtime else "startup-proxy-recovery", daemon=True)
    try:
        thread.start()
    except BaseException:
        if not runtime:
            _end_proxy_rt1(phase)
        raise
    try:
        result = answers.get(timeout=max(0.0, stop_at - time.monotonic()))
    except queue.Empty:
        if gate.cancel_before_commit():
            return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
        # The commit cannot be cancelled. The worker owns the gate until
        # os.replace and its cleanup finish, regardless of this report.
        return RecoveryReport(_report("unknown", "commit_pending", None), gate)
    if expired() and not gate.committing:
        gate.cancel_before_commit()
        return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
    return RecoveryReport(result, gate)


def recover_startup_firewall(
    *, may_take_over_host_network: bool, deadline_seconds: float = 2.0,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = recovery_command_runner,
    admin_available: bool | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> RecoveryReport:
    """Inspect, delete and verify old fixed-name rules under one deadline.

    The caller owns the default-instance startup gate, excluding a live legacy
    instance. The runner must accept ``timeout`` and terminate/wait for its
    child before returning or raising. Commands are synchronous: no deletion
    can remain in flight after this function returns.
    """
    gate = ProxyRecoveryGate(time.monotonic() + max(0.0, deadline_seconds))
    if diagnostics is None:
        diagnostics = {}
    if not may_take_over_host_network:
        gate.done.set()
        return RecoveryReport(_report("not_needed", "scope_isolated"), gate)
    if deadline_seconds <= 0:
        gate.done.set()
        return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)

    def remaining() -> float:
        return max(0.0, gate.stop_at - time.monotonic())

    def inventory() -> list[dict[str, str]]:
        budget = remaining()
        if budget <= 0:
            raise TimeoutError("firewall_deadline_exceeded")
        return recovery_firewall_rules(command_runner, timeout=budget)

    def owned(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        return [row for row in rows if recovery_rule_identity_valid(
            row["name"], row["display_name"])]

    try:
        try:
            initial = inventory()
        except (subprocess.TimeoutExpired, TimeoutError):
            return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
        except Exception as exc:
            diagnostics["inspection_error_type"] = type(exc).__name__
            return RecoveryReport(_report("unknown", "inspection_failed", None), gate)
        if remaining() <= 0:
            return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
        candidates = owned(initial)
        diagnostics["unconfirmed_rules"] = [row for row in initial if row not in candidates]
        if admin_available is None:
            try:
                admin_available = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                admin_available = False
        if not admin_available:
            state = "permission_required" if candidates else ("not_owned" if initial else "not_needed")
            code = "admin_required" if candidates else ("unconfirmed_rules" if initial else None)
            return RecoveryReport(_report(state, code,
                                          len(candidates) if candidates else len(initial)), gate)
        if not candidates:
            return RecoveryReport(_report("not_owned" if initial else "not_needed",
                                          "unconfirmed_rules" if initial else None,
                                          len(initial)), gate)
        outcomes: list[dict[str, Any]] = []
        diagnostics["deletions"] = outcomes
        for row in candidates:
            budget = remaining()
            if budget <= 0:
                return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
            try:
                recovery_delete_owned_firewall_rule(
                    row["name"], row["display_name"], command_runner, timeout=budget,
                )
                outcomes.append({"name": row["name"], "ok": True})
            except (subprocess.TimeoutExpired, TimeoutError):
                outcomes.append({"name": row["name"], "ok": False, "error": "timeout"})
                return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
            except Exception as exc:
                outcomes.append({"name": row["name"], "ok": False,
                                 "error": type(exc).__name__})
        try:
            verified = inventory()
        except (subprocess.TimeoutExpired, TimeoutError):
            return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
        except Exception as exc:
            diagnostics["verification_error_type"] = type(exc).__name__
            return RecoveryReport(_report("unknown", "verification_failed", None), gate)
        if remaining() <= 0:
            return RecoveryReport(_report("unknown", "deadline_exceeded", None), gate)
        left = owned(verified)
        diagnostics["unconfirmed_rules_after"] = [row for row in verified if row not in left]
        if verified:
            return RecoveryReport(_report("failed", "rules_remaining", len(verified)), gate)
        return RecoveryReport(_report("restored"), gate)
    finally:
        gate.done.set()


class NetworkRecoveryGate:
    """The caller must check both domains before taking over host networking."""

    def __init__(self, *gates: ProxyRecoveryGate) -> None:
        self.gates = gates

    def require_idle(self) -> None:
        for gate in self.gates:
            gate.require_idle()


def recover_network_after_unclean_exit(
    store: RuntimeStateStore, *, scope: Any, deadline_seconds: float = 2.0,
    backend: RegistryBackend | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = recovery_command_runner,
    admin_available: bool | None = None,
    diagnostics: dict[str, Any] | None = None,
    proxy_manager: SystemProxyManager | None = None,
) -> RecoveryReport:
    """Recover under one budget: RT-1 at startup, or a manager transaction at exit.

    Callers with a constructed runtime MUST supply its proxy manager. The
    returned gate must be idle before releasing the instance lease.
    """
    start = time.monotonic()
    allowed = scope.may_take_over_host_network
    if diagnostics is None:
        diagnostics = {}
    proxy = _recover_system_proxy(
        store, may_take_over_host_network=allowed,
        deadline_seconds=max(0.0, deadline_seconds - (time.monotonic() - start)),
        backend=backend, diagnostics=diagnostics.setdefault("proxy", {}),
        proxy_manager=proxy_manager,
    )
    firewall = recover_startup_firewall(
        may_take_over_host_network=allowed,
        deadline_seconds=max(0.0, deadline_seconds - (time.monotonic() - start)),
        command_runner=command_runner, admin_available=admin_available,
        diagnostics=diagnostics.setdefault("firewall", {}),
    )
    result = RecoveryReport({"proxy": dict(proxy), "firewall": dict(firewall)}, proxy.gate)
    result.gate = NetworkRecoveryGate(proxy.gate, firewall.gate)
    return result
