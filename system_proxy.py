"""System proxy authority and journal single-writer boundary.

Per 决定_P2-29_journal单写者.md and P3-02, the instance mutex excludes
other processes. In this process, pre-runtime RT-1 and runtime transactions
never overlap. RT-1 is initiated by the startup coordinator (normally the main
thread); its bounded worker retains the phase until it actually finishes.
Constructing a SystemProxyManager seals RT-1 for the rest of this process.
Journal comparisons are defense in depth, not concurrent CAS guarantees.
"""

from __future__ import annotations

import ctypes
import platform
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from config_utils import coerce_bool, coerce_port
from system_proxy_runtime import DEFAULT_SYSTEM_PROXY_BYPASS, normalize_system_proxy_bypass


INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


# One irreversible authority shared by every manager in this process.
_PROXY_WRITES_REVOKED = threading.Event()
_PROXY_TRANSACTION_LOCK = threading.RLock()
_PROXY_WRITE_INTENT_LOCK = threading.Lock()
_PROXY_WRITES_IN_FLIGHT = 0
_PROXY_TRANSACTIONS_IN_FLIGHT = 0
_PROXY_CONTEXT = threading.local()
_PROXY_RUNTIME_CONSTRUCTED = False
_PROXY_RT1_PHASE = None
_PROXY_EMERGENCY_CREDENTIAL = None


def _mark_proxy_runtime_constructed() -> None:
    global _PROXY_RUNTIME_CONSTRUCTED
    with _PROXY_WRITE_INTENT_LOCK:
        if _PROXY_RT1_PHASE is not None:
            raise RuntimeError("RT-1 recovery is still running")
        _PROXY_RUNTIME_CONSTRUCTED = True


def _begin_proxy_rt1(*, isolated: bool = False):
    global _PROXY_RT1_PHASE
    with _PROXY_WRITE_INTENT_LOCK:
        # RT-1 precedes runtime construction, hence revocation cannot exist.
        if _PROXY_WRITES_REVOKED.is_set():
            raise RuntimeError("RT-1 cannot run after writes_revoked")
        # Isolated scopes return without host I/O, so never enter RT-1's write
        # phase. Their shutdown may repeat this no-op after runtime creation.
        if isolated:
            return None
        if (_PROXY_RUNTIME_CONSTRUCTED or _PROXY_RT1_PHASE is not None
                or _PROXY_TRANSACTIONS_IN_FLIGHT):
            raise RuntimeError("RT-1 requires an exclusive pre-runtime phase")
        _PROXY_RT1_PHASE = object()
        return _PROXY_RT1_PHASE


def _end_proxy_rt1(phase) -> None:
    global _PROXY_RT1_PHASE
    with _PROXY_WRITE_INTENT_LOCK:
        if _PROXY_RT1_PHASE is phase:
            _PROXY_RT1_PHASE = None


@contextmanager
def _proxy_rt1_worker(phase):
    _PROXY_CONTEXT.rt1_phase = phase
    try:
        yield
    finally:
        _PROXY_CONTEXT.rt1_phase = None
        _end_proxy_rt1(phase)


def assert_proxy_journal_writer() -> None:
    """Only this thread's transaction or the designated RT-1 worker may write."""
    if getattr(_PROXY_CONTEXT, "transaction_depth", 0):
        return
    phase = getattr(_PROXY_CONTEXT, "rt1_phase", None)
    if phase is not None and phase is _PROXY_RT1_PHASE:
        if _PROXY_WRITES_REVOKED.is_set():
            raise RuntimeError("RT-1 cannot run after writes_revoked")
        return
    raise RuntimeError("proxy journal write requires a transaction or RT-1 phase")


def _proxy_write_authorized(credential=None) -> bool:
    # Call while holding _PROXY_WRITE_INTENT_LOCK.
    return (not _PROXY_WRITES_REVOKED.is_set()
            or credential is not None and credential is _PROXY_EMERGENCY_CREDENTIAL)


@contextmanager
def _proxy_transaction():
    global _PROXY_TRANSACTIONS_IN_FLIGHT
    with _PROXY_TRANSACTION_LOCK:
        with _PROXY_WRITE_INTENT_LOCK:
            if _PROXY_RT1_PHASE is not None:
                raise RuntimeError("proxy transaction cannot overlap RT-1")
            _PROXY_TRANSACTIONS_IN_FLIGHT += 1
        _PROXY_CONTEXT.transaction_depth = getattr(_PROXY_CONTEXT, "transaction_depth", 0) + 1
        try:
            yield
        finally:
            _PROXY_CONTEXT.transaction_depth -= 1
            with _PROXY_WRITE_INTENT_LOCK:
                _PROXY_TRANSACTIONS_IN_FLIGHT -= 1


class ProxyWritesRevoked(RuntimeError):
    pass


def revoke_system_proxy_writes() -> dict[str, Any]:
    """Revoke ordinary writers and issue a local, opaque RT-2 recovery capability.

    Each call invalidates the previous credential. Only emergency restoration
    accepts it, for one attempt (including failed attempts). Revoke again to
    retry. The object must stay in process; do not serialize it into protocol
    messages. RT-2 must drain existing transactions/OS calls before recovering.
    """
    global _PROXY_EMERGENCY_CREDENTIAL
    with _PROXY_WRITE_INTENT_LOCK:
        if _PROXY_RT1_PHASE is not None:
            raise RuntimeError("revocation cannot overlap RT-1")
        _PROXY_WRITES_REVOKED.set()
        _PROXY_EMERGENCY_CREDENTIAL = object()
        credential = _PROXY_EMERGENCY_CREDENTIAL
    return {"state": "writes_revoked", "error_code": "writes_revoked",
            "recovery_credential": credential}


def system_proxy_writes_idle() -> bool:
    """After revocation, RT-2 must drain writes AND journal commits first."""
    if not _PROXY_TRANSACTION_LOCK.acquire(blocking=False):
        return False
    try:
        with _PROXY_WRITE_INTENT_LOCK:
            return (_PROXY_WRITES_IN_FLIGHT == 0 and _PROXY_TRANSACTIONS_IN_FLIGHT == 0
                    and _PROXY_RT1_PHASE is None)
    finally:
        _PROXY_TRANSACTION_LOCK.release()


@dataclass
class ProxySettings:
    enabled: bool
    server: str
    override: str


class RegistryBackend(Protocol):
    def read(self) -> ProxySettings:
        ...

    def write(self, settings: ProxySettings) -> None:
        ...


class WinRegProxyBackend:
    def __init__(self, key_path: str = INTERNET_SETTINGS_KEY) -> None:
        self.key_path = key_path

    def read(self) -> ProxySettings:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ) as key:
            enabled = bool(self._read_value(winreg, key, "ProxyEnable", 0))
            server = str(self._read_value(winreg, key, "ProxyServer", "") or "")
            override = str(self._read_value(winreg, key, "ProxyOverride", "") or "")
            return ProxySettings(enabled=enabled, server=server, override=override)

    def write(self, settings: ProxySettings) -> None:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if settings.enabled else 0)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, settings.server)
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, settings.override)
        self._notify_windows()

    def _read_value(self, winreg_module, key, name: str, default: Any) -> Any:
        try:
            return winreg_module.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return default

    def _notify_windows(self) -> None:
        try:
            internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
            internet_set_option(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
            internet_set_option(0, INTERNET_OPTION_REFRESH, 0, 0)
        except Exception:
            return


class SystemProxyManager:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        backend: RegistryBackend | None = None,
        platform_name: str | None = None,
    ) -> None:
        _mark_proxy_runtime_constructed()
        self.config = config or {}
        self.enabled = coerce_bool(self.config.get("enabled", True), True)
        self.host = str(self.config.get("host", "127.0.0.1"))
        self.port = coerce_port(self.config.get("port", 7890), 7890)
        self.bypass = self._normalize_bypass(self.config.get("bypass", DEFAULT_SYSTEM_PROXY_BYPASS))
        self.platform_name = (platform_name or platform.system()).lower()
        self.backend = backend or (WinRegProxyBackend() if self.platform_name == "windows" else None)
        self.original_settings: ProxySettings | None = None
        self.active = False
        self.last_error: str | None = None
        self._lock = _PROXY_TRANSACTION_LOCK
        self._journal_store = None
        self._journal_pending = False
        self._owner = "tesseract:" + uuid.uuid4().hex
        from runtime_session_end import RecoveryBarrier
        # Share the barrier with the irreversible process authority, including
        # managers constructed after shutdown closes recovery admission. Tests
        # model a new process by replacing that authority Event, never clearing
        # or reopening a production barrier.
        with _PROXY_WRITE_INTENT_LOCK:
            gate = getattr(_PROXY_WRITES_REVOKED, "_recovery_gate", None)
            if gate is None:
                gate = RecoveryBarrier()
                _PROXY_WRITES_REVOKED._recovery_gate = gate
            self.emergency_gate = gate

    @property
    def supported(self) -> bool:
        return self.platform_name == "windows" and self.backend is not None

    def proxy_server(self) -> str:
        return f"{self.host}:{self.port}"

    def _normalize_bypass(self, value: Any) -> list[str]:
        return normalize_system_proxy_bypass(value)

    def proxy_override(self) -> str:
        return ";".join(self.bypass)

    def is_target_active(self) -> bool:
        with self._lock:
            if not self.supported:
                return False
            try:
                current = self.backend.read()
            except Exception as exc:
                self.last_error = str(exc)
                return False
            return self._settings_match_target(current)

    def capture_original(self) -> ProxySettings | None:
        with self._lock:
            if not self.supported:
                return None
            if self.original_settings is None:
                self.original_settings = self.backend.read()
            return self.original_settings

    def revoke_writes(self) -> dict[str, Any]:
        return revoke_system_proxy_writes()

    def _write_settings(self, settings: ProxySettings, *, credential=None) -> None:
        # Final checkpoint, including legacy enable/restore/override writers.
        global _PROXY_WRITES_IN_FLIGHT
        with _PROXY_WRITE_INTENT_LOCK:
            if not _proxy_write_authorized(credential):
                raise ProxyWritesRevoked("writes_revoked")
            _PROXY_WRITES_IN_FLIGHT += 1
        try:
            self.backend.write(settings)
        finally:
            with _PROXY_WRITE_INTENT_LOCK:
                _PROXY_WRITES_IN_FLIGHT -= 1

    def _transaction_result(self, state: str, code: str | None = None,
                            current: ProxySettings | None = None) -> dict[str, Any]:
        self.last_error = code
        return {
            "enabled": self.enabled, "supported": self.supported,
            "active": self.active,
            "restorable": self.original_settings is not None or self._journal_pending,
            "target": self.proxy_server(), "bypass": list(self.bypass),
            "current": vars(current).copy() if current is not None else None,
            "last_error": code, "state": state, "error_code": code,
            "remaining_count": None if state == "unknown" else int(self._journal_pending),
            "ok": code is None,
        }

    def _bind_journal(self, store) -> None:
        self._journal_store = store
        store.proxy_transactions_managed = True

    def _read_journal(self, store):
        # Lazy import avoids runtime_state -> system_proxy -> runtime_state.
        from runtime_network_recovery import _snapshot

        payload = store.load_proxy_journal()
        self._journal_pending = "system_proxy" in payload
        if not self._journal_pending:
            return None
        snapshot = _snapshot(payload)
        if snapshot is None:
            raise ValueError("journal_corrupt")
        return payload["system_proxy"], snapshot

    def enable_transaction(self, store) -> dict[str, Any]:
        with _proxy_transaction():
            self._bind_journal(store)
            if _PROXY_WRITES_REVOKED.is_set():
                return self._transaction_result("writes_revoked", "writes_revoked")
            if not self.enabled or not self.supported:
                return self._transaction_result("not_needed")
            try:
                journal = self._read_journal(store)
            except Exception:
                self._journal_pending = True
                return self._transaction_result("unknown", "journal_unreadable")
            try:
                current = self.backend.read()
            except Exception:
                return self._transaction_result("unknown", "registry_read_failed")
            target = self.proxy_server()
            if journal is not None:
                entry, (old_target, original) = journal
                if old_target != target:
                    return self._transaction_result("not_owned", "journal_target_conflict", current)
                if not (current.enabled and current.server == target) and current != original:
                    return self._transaction_result("not_owned", "proxy_taken_over", current)
                owner = entry.get("owner") or self._owner
            else:
                original = self.original_settings or current
                owner = self._owner
            desired = ProxySettings(True, target, self.proxy_override())
            try:
                store.save_proxy_snapshot(original=original, target=target,
                                          owner=owner, phase="prepared")
            except Exception:
                return self._transaction_result("failed", "journal_write_failed", current)
            self.original_settings = original
            self._journal_pending = True
            try:
                # fsync may have taken long enough for another client to act.
                observed = self.backend.read()
            except Exception:
                return self._transaction_result("unknown", "registry_read_failed")
            if observed != current:
                return self._transaction_result("not_owned", "proxy_taken_over", observed)
            try:
                self._write_settings(desired)
            except ProxyWritesRevoked:
                return self._transaction_result("writes_revoked", "writes_revoked", current)
            except Exception:
                state, code, phase = "failed", "registry_write_failed", "write_failed"
                try:
                    current = self.backend.read()
                except Exception:
                    current = None
            else:
                try:
                    current = self.backend.read()
                except Exception:
                    current = None
                    state, code, phase = "unknown", "registry_verify_failed", "unknown"
                else:
                    if current == desired:
                        state, code, phase = "applied", None, "applied"
                    else:
                        state, code, phase = "failed", "registry_verify_mismatch", "verify_failed"
            self.active = bool(current == desired and code is None)
            try:
                store.save_proxy_snapshot(original=original, target=target,
                                          owner=owner, phase=phase)
            except Exception:
                return self._transaction_result("failed", "journal_phase_write_failed", current)
            return self._transaction_result(state, code, current)

    def restore_transaction(self, store) -> dict[str, Any]:
        return self._restore_transaction(store)

    def emergency_restore_transaction(self, store, credential, *, expired=lambda: False) -> dict[str, Any]:
        # Enter BEFORE waiting for the transaction lock. Final shutdown closes
        # this entry and drains every admitted emergency, including waiters.
        if not self.emergency_gate.enter():
            return self._transaction_result("unknown", "recovery_closed")
        try:
            return self._emergency_restore_transaction(store, credential, expired=expired)
        finally:
            self.emergency_gate.leave()

    def _emergency_restore_transaction(self, store, credential, *, expired) -> dict[str, Any]:
        """RT-2 only: consume the credential issued by revoke_writes().

        Uses the ordinary ownership/readback/clear contract under the same
        transaction lock. A forged, superseded or consumed credential is denied.
        """
        global _PROXY_EMERGENCY_CREDENTIAL
        with _proxy_transaction():
            with _PROXY_WRITE_INTENT_LOCK:
                if (not _PROXY_WRITES_REVOKED.is_set() or credential is None
                        or credential is not _PROXY_EMERGENCY_CREDENTIAL
                        or _PROXY_TRANSACTIONS_IN_FLIGHT != 1):
                    # In particular, do not reenter an existing transaction
                    # through an OS/filesystem callback on this same thread.
                    return self._transaction_result("writes_revoked", "writes_revoked")
            try:
                return self._restore_transaction(store, credential=credential, expired=expired)
            finally:
                with _PROXY_WRITE_INTENT_LOCK:
                    if _PROXY_EMERGENCY_CREDENTIAL is credential:
                        _PROXY_EMERGENCY_CREDENTIAL = None

    def _restore_transaction(self, store, *, credential=None, expired=lambda: False) -> dict[str, Any]:
        from runtime_state import ProxyJournalChanged

        with _proxy_transaction():
            if expired():
                return self._transaction_result("unknown", "deadline_exceeded")
            with _PROXY_WRITE_INTENT_LOCK:
                if not _proxy_write_authorized(credential):
                    return self._transaction_result("writes_revoked", "writes_revoked")
            self._bind_journal(store)
            if not self.supported:
                return self._transaction_result("unknown", "unsupported")
            try:
                journal = self._read_journal(store)
            except Exception:
                self._journal_pending = True
                return self._transaction_result("unknown", "journal_unreadable")
            if journal is None:
                return self._transaction_result("not_needed")
            entry, (target, original) = journal
            try:
                current = self.backend.read()
            except Exception:
                return self._transaction_result("unknown", "registry_read_failed")
            # Equality is proof that no further write is needed, including a
            # crash after restoration but before journal removal.
            if current != original:
                if not current.enabled or current.server != target:
                    self.active = False
                    return self._transaction_result("not_owned", "proxy_taken_over", current)
                with _PROXY_WRITE_INTENT_LOCK:
                    if not _proxy_write_authorized(credential):
                        return self._transaction_result("writes_revoked", "writes_revoked", current)
                if expired():
                    return self._transaction_result("unknown", "deadline_exceeded")
                try:
                    entry = store.save_proxy_snapshot(
                        original=original, target=target,
                        owner=entry.get("owner") or self._owner,
                        phase="restoring", expected=entry)
                except ProxyJournalChanged:
                    return self._transaction_result("failed", "journal_changed", current)
                except Exception:
                    return self._transaction_result("failed", "journal_write_failed", current)
                try:
                    observed = self.backend.read()
                except Exception:
                    return self._transaction_result("unknown", "registry_read_failed")
                if not observed.enabled or observed.server != target:
                    return self._transaction_result("not_owned", "proxy_taken_over", observed)
                try:
                    if store.load_proxy_journal().get("system_proxy") != entry:
                        return self._transaction_result("failed", "journal_changed", observed)
                except Exception:
                    return self._transaction_result("unknown", "journal_unreadable", observed)
                if expired():
                    return self._transaction_result("unknown", "deadline_exceeded")
                try:
                    self._write_settings(original, credential=credential)
                except ProxyWritesRevoked:
                    return self._transaction_result("writes_revoked", "writes_revoked", current)
                except Exception:
                    return self._transaction_result("failed", "registry_write_failed", current)
            try:
                verified = self.backend.read()
            except Exception:
                return self._transaction_result("unknown", "registry_verify_failed")
            if verified != original:
                return self._transaction_result("failed", "registry_verify_mismatch", verified)
            self.active = False
            with _PROXY_WRITE_INTENT_LOCK:
                if not _proxy_write_authorized(credential):
                    return self._transaction_result("writes_revoked", "writes_revoked", verified)
            if expired():
                return self._transaction_result("unknown", "deadline_exceeded")
            try:
                if credential is None:
                    cleared = store.clear_proxy_snapshot(entry)
                else:
                    # Preserve the original RT-2 deadline across fsync. The
                    # ordinary store API has no deadline; emergency cleanup
                    # uses its same compare/replace primitive with cancellation.
                    import json
                    from runtime_network_recovery import _clear_proxy_entry
                    raw = store.path.read_bytes()
                    payload = json.loads(raw.decode("utf-8"))
                    cleared = (payload.get("system_proxy") == entry and
                               _clear_proxy_entry(store.path, raw, payload, expired,
                                                  lambda: not expired()))
                if expired():
                    return self._transaction_result("unknown", "deadline_exceeded")
                if not cleared:
                    return self._transaction_result("failed", "journal_changed", verified)
            except Exception:
                return self._transaction_result("failed", "journal_clear_failed", verified)
            self.original_settings = None
            self._journal_pending = False
            return self._transaction_result("restored", current=verified)

    def enable(self) -> dict[str, Any]:
        with _proxy_transaction():
            if self._journal_store is not None:
                return self.enable_transaction(self._journal_store)
            if _PROXY_WRITES_REVOKED.is_set():
                return self._transaction_result("writes_revoked", "writes_revoked")
            if not self.enabled:
                return self.get_state()
            if not self.supported:
                self.last_error = "system proxy is only supported on Windows"
                return self.get_state()
            try:
                self.capture_original()
                self._write_settings(
                    ProxySettings(
                        enabled=True,
                        server=self.proxy_server(),
                        override=self.proxy_override(),
                    )
                )
                self.active = True
                self.last_error = None
            except ProxyWritesRevoked:
                return self._transaction_result("writes_revoked", "writes_revoked")
            except Exception as exc:
                self.last_error = str(exc)
                raise
            return self.get_state()

    def apply_proxy_override(self) -> dict[str, Any]:
        """Push ``self.bypass`` to Windows only while our own target is live.

        Only ProxyOverride changes.  The server, the captured original and
        ``active`` stay as they are, so a proxy owned by another client is
        never taken over; the list then waits for the next ``enable()``.
        """
        with _proxy_transaction():
            if _PROXY_WRITES_REVOKED.is_set():
                return {"applied": False, "reason": "writes_revoked", "error_code": "writes_revoked"}
            if not self.enabled:
                return {"applied": False, "reason": "disabled"}
            if not self.supported:
                return {"applied": False, "reason": "unsupported"}
            if not self.active:
                return {"applied": False, "reason": "inactive"}
            try:
                current = self.backend.read()
            except Exception as exc:
                self.last_error = str(exc)
                return {"applied": False, "reason": "read_failed"}
            if not self._settings_match_target(current):
                return {"applied": False, "reason": "not_target"}
            override = self.proxy_override()
            if current.override == override:
                return {"applied": True, "reason": "unchanged"}
            try:
                self._write_settings(
                    ProxySettings(
                        enabled=True,
                        server=current.server,
                        override=override,
                    )
                )
                self.last_error = None
            except ProxyWritesRevoked:
                return {"applied": False, "reason": "writes_revoked", "error_code": "writes_revoked"}
            except Exception as exc:
                self.last_error = str(exc)
                raise
            return {"applied": True, "reason": "written"}

    def disable(self) -> dict[str, Any]:
        with _proxy_transaction():
            # Explicit disable retains the legacy clear-target behavior.
            # Implicit restoration must never invent a missing original.
            if self._journal_store is None and self.original_settings is None:
                return self.restore_settings(ProxySettings(False, "", ""))
            return self.restore()

    def restore(self) -> dict[str, Any]:
        with _proxy_transaction():
            if _PROXY_WRITES_REVOKED.is_set():
                return self._transaction_result("writes_revoked", "writes_revoked")
            if self._journal_store is not None:
                return self.restore_transaction(self._journal_store)
            if self.original_settings is None:
                return self._transaction_result("unknown", "original_missing")
            return self.restore_settings(self.original_settings)

    def restore_settings(self, settings: ProxySettings) -> dict[str, Any]:
        with _proxy_transaction():
            if _PROXY_WRITES_REVOKED.is_set():
                return self._transaction_result("writes_revoked", "writes_revoked")
            if self._journal_store is not None:
                return self.restore_transaction(self._journal_store)
            if not self.supported:
                return self.get_state()
            try:
                current = self.backend.read()
            except Exception:
                return self._transaction_result("unknown", "registry_read_failed")
            if current != settings:
                if not self._settings_match_target(current):
                    self.active = False
                    return self._transaction_result("not_owned", "proxy_taken_over", current)
                try:
                    self._write_settings(settings)
                except ProxyWritesRevoked:
                    return self._transaction_result("writes_revoked", "writes_revoked", current)
                except Exception as exc:
                    self.last_error = str(exc)
                    raise
            try:
                current = self.backend.read()
            except Exception:
                return self._transaction_result("unknown", "registry_verify_failed")
            if current != settings:
                return self._transaction_result("failed", "registry_verify_mismatch", current)
            self.original_settings = None
            self.active = False
            return self._transaction_result("restored", current=current)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            current: ProxySettings | None = None
            if self.supported:
                try:
                    current = self.backend.read()
                except Exception as exc:
                    self.last_error = str(exc)
            return {
                "enabled": self.enabled,
                "supported": self.supported,
                "active": self.active,
                "restorable": self.original_settings is not None or self._journal_pending,
                "target": self.proxy_server(),
                "bypass": list(self.bypass),
                "current": {
                    "enabled": current.enabled,
                    "server": current.server,
                    "override": current.override,
                } if current else None,
                "last_error": self.last_error,
            }

    def _settings_match_target(self, settings: ProxySettings) -> bool:
        return bool(settings.enabled and settings.server == self.proxy_server())
