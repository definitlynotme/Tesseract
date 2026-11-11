"""Windows instance identity and sidecar single-instance ownership.

The legacy mutex is an existence gate, not an owned mutex.  The sidecar must
keep its handle as well as the owned runtime mutex until final cleanup.  A
shell that creates the legacy gate first selects the explicit inherited mode;
the default create mode never assumes an existing gate belongs to its caller.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping


ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF

_DEFAULT_MUTEX = r"Local\TesseractProxyManagerSingleInstance"
_DEFAULT_RESTORE = r"Local\TesseractProxyManagerRestoreWindow"
_DEFAULT_RUNTIME = r"Local\TesseractRuntime"
_AUDIT_ENV = "TESSERACT_ISOLATED_AUDIT_MANIFEST"
_CUSTOM_ENV = "TESSERACT_INSTANCE_SCOPE"
_CONFIG_ENV = "TESSERACT_CONFIG_DIR"
_DATA_ENV = "TESSERACT_DATA_DIR"
_AUDIT_KEYS = {
    "schema", "audit_id", "interface", "interface_index",
    "dns_controller_port", "dns_listener_port", "config_dir", "data_dir",
}
_CUSTOM_HASH_PREFIX = b"tesseract-instance-scope-v1\0"
_LCMAP_UPPERCASE = 0x00000200
_owned_names: set[str] = set()
_owned_names_lock = threading.Lock()


class ScopeError(ValueError):
    """Invalid scope input; code is stable for callers and tests."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class InstanceNames:
    legacy_mutex: str
    restore_event: str
    runtime_mutex: str
    shell_mutex: str
    quitting_event: str


@dataclass(frozen=True)
class InstanceScope:
    kind: Literal["default", "audit", "custom"]
    id: str
    names: InstanceNames
    may_take_over_host_network: bool
    config_dir: Path | None = None
    data_dir: Path | None = None

    def hello_scope(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


def _invariant_upper(value: str) -> str:
    """Apply the Windows invariant-locale uppercase mapping used by Rust."""
    if os.name != "nt":
        return value.upper()
    mapper = ctypes.WinDLL("kernel32", use_last_error=True).LCMapStringEx
    mapper.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p,
                       ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
    mapper.restype = ctypes.c_int
    required = mapper("", _LCMAP_UPPERCASE, value, -1, None, 0, None, None, 0)
    if not required:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required)
    written = mapper("", _LCMAP_UPPERCASE, value, -1,
                     buffer, required, None, None, 0)
    if written != required:
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _identity_path(path: str | Path) -> str:
    """Windows spelling after strict real-path resolution by resolve_scope."""
    spelling = ntpath.normpath(str(path))
    if spelling[:8].casefold() == "\\\\?\\unc\\":
        spelling = "\\\\" + spelling[8:]
    elif (spelling.startswith("\\\\?\\") and len(spelling) >= 6
          and spelling[4].isalpha() and spelling[5] == ":"):
        spelling = spelling[4:]
    return _invariant_upper(ntpath.normpath(spelling))


def _canonical_resolved_path(path: Path) -> Path:
    return Path(_identity_path(path)) if os.name == "nt" else path


def custom_scope_id(config_dir: str | Path, data_dir: str | Path) -> str:
    """Hash a resolved directory pair; the opt-in label does not change identity.

    Rust must use the same UTF-8, four-byte big-endian lengths, domain prefix,
    path order, and first 24 lowercase SHA-256 hex digits.
    """
    parts = [_identity_path(config_dir).encode("utf-8"),
             _identity_path(data_dir).encode("utf-8")]
    digest = hashlib.sha256(_CUSTOM_HASH_PREFIX)
    for part in parts:
        digest.update(len(part).to_bytes(4, "big"))
        digest.update(part)
    return digest.hexdigest()[:24]


def names_for_scope(kind: str, scope_id: str) -> InstanceNames:
    if kind == "default" and scope_id == "default":
        return InstanceNames(_DEFAULT_MUTEX, _DEFAULT_RESTORE, _DEFAULT_RUNTIME,
                             r"Local\TesseractShell", r"Local\TesseractShellQuitting")
    if kind == "audit" and re.fullmatch(r"[0-9a-f]{32}", scope_id):
        stem = f"Local\\TesseractAudit-{scope_id}"
    elif kind == "custom" and re.fullmatch(r"[0-9a-f]{24}", scope_id):
        stem = f"Local\\TesseractCustom-{scope_id}"
    else:
        raise ScopeError("invalid_scope_id", "invalid instance scope kind or id")
    return InstanceNames(f"{stem}-instance", f"{stem}-restore", f"{stem}-runtime",
                         f"{stem}-shell", f"{stem}-quitting")


def _resolved_directory(raw: str | Path, field: str) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise ScopeError("directory_missing", f"{field} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ScopeError("directory_relative", f"{field} must be absolute")
    try:
        resolved = _canonical_resolved_path(path.resolve(strict=True))
        if not resolved.is_dir():
            raise ScopeError("directory_not_dir", f"{field} is not a directory")
    except (OSError, RuntimeError) as exc:
        raise ScopeError("directory_invalid", f"{field} cannot be resolved") from exc
    return resolved


def _probe_writable(path: Path, field: str) -> None:
    try:
        with tempfile.NamedTemporaryFile(prefix=".tesseract-scope-", dir=path) as probe:
            probe.write(b"1")
            probe.flush()
    except OSError as exc:
        raise ScopeError("directory_not_writable", f"{field} is not writable") from exc


def _directory_pair(env: Mapping[str, str], config_dir: str | Path | None,
                    data_dir: str | Path | None) -> tuple[Path, Path]:
    if not env.get(_CONFIG_ENV) or not env.get(_DATA_ENV):
        raise ScopeError("directory_override_required", "both directory overrides are required")
    config = _resolved_directory(env[_CONFIG_ENV], "config_dir")
    data = _resolved_directory(env[_DATA_ENV], "data_dir")
    for supplied, resolved, field in ((config_dir, config, "config_dir"),
                                      (data_dir, data, "data_dir")):
        if supplied is not None and _resolved_directory(supplied, field) != resolved:
            raise ScopeError("directory_mismatch", f"{field} differs from its environment override")
    return config, data


def _default_directories(env: Mapping[str, str]) -> tuple[Path, Path]:
    appdata = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    localappdata = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return (_canonical_resolved_path((Path(appdata) / "Tesseract").resolve()),
            _canonical_resolved_path((Path(localappdata) / "Tesseract").resolve()))


def _audit_manifest(env: Mapping[str, str], config: Path, data: Path) -> str:
    raw_path = env[_AUDIT_ENV]
    if not raw_path or not Path(raw_path).is_absolute():
        raise ScopeError("audit_manifest_invalid", "audit manifest path must be absolute")
    try:
        path = _canonical_resolved_path(Path(raw_path).resolve(strict=True))
        if not path.is_file() or path.stat().st_size > 65536:
            raise ScopeError("audit_manifest_invalid", "audit manifest is not a small file")
        value = json.loads(path.read_bytes())
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScopeError("audit_manifest_invalid", "audit manifest cannot be read") from exc
    if (not isinstance(value, dict) or set(value) != _AUDIT_KEYS
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise ScopeError("audit_manifest_invalid", "audit manifest schema is invalid")
    audit_id = value["audit_id"]
    if not isinstance(audit_id, str) or not re.fullmatch(r"[0-9a-f]{32}", audit_id):
        raise ScopeError("audit_id_invalid", "audit id must be 32 lowercase hex digits")
    if (not isinstance(value["interface"], str) or not value["interface"].strip()
            or type(value["interface_index"]) is not int
            or not 1 <= value["interface_index"] <= 0xFFFFFFFF):
        raise ScopeError("audit_manifest_invalid", "audit interface is invalid")
    ports = (value["dns_controller_port"], value["dns_listener_port"])
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports) or ports[0] == ports[1]:
        raise ScopeError("audit_manifest_invalid", "audit ports are invalid")
    expected = (("config_dir", config, path.parent / "config"),
                ("data_dir", data, path.parent / "data"))
    for field, supplied, dedicated in expected:
        if (_resolved_directory(value[field], field) != supplied
                or supplied != dedicated):
            raise ScopeError("audit_directory_mismatch", f"{field} is not dedicated to the audit")
    return audit_id


def resolve_scope(*, environ: Mapping[str, str] | None = None,
                  config_dir: str | Path | None = None,
                  data_dir: str | Path | None = None) -> InstanceScope:
    """Resolve scope before any runtime or network operation.

    Audit takes precedence over custom. Invalid opted-in input always raises;
    it never silently falls back to the default scope.
    """
    env = dict(os.environ if environ is None else environ)
    if _AUDIT_ENV not in env and _CUSTOM_ENV not in env:
        return InstanceScope("default", "default", names_for_scope("default", "default"), True,
                             Path(config_dir) if config_dir is not None else None,
                             Path(data_dir) if data_dir is not None else None)
    config, data = _directory_pair(env, config_dir, data_dir)
    if _AUDIT_ENV in env:
        audit_id = _audit_manifest(env, config, data)
        kind: Literal["audit", "custom"] = "audit"
        scope_id = audit_id
    else:
        if not env[_CUSTOM_ENV].strip():
            raise ScopeError("custom_scope_empty", "custom scope opt-in is empty")
        default_config, default_data = _default_directories(env)
        if config == default_config or data == default_data:
            raise ScopeError("default_directory_forbidden", "custom scope uses a default directory")
        kind = "custom"
        scope_id = custom_scope_id(config, data)
    _probe_writable(config, "config_dir")
    _probe_writable(data, "data_dir")
    return InstanceScope(kind, scope_id, names_for_scope(kind, scope_id), False, config, data)


class Win32MutexAPI:
    """Small injectable Win32 boundary; every method returns its error at once."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 named mutexes require Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel.ReleaseMutex.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        self._kernel = kernel

    def create_mutex(self, name: str) -> tuple[int | None, int]:
        handle = self._kernel.CreateMutexW(None, False, name)
        return handle, ctypes.get_last_error()

    def wait(self, handle: int, timeout_ms: int) -> tuple[int, int]:
        status = self._kernel.WaitForSingleObject(handle, timeout_ms)
        return status, ctypes.get_last_error() if status == WAIT_FAILED else 0

    def release(self, handle: int) -> tuple[bool, int]:
        ok = bool(self._kernel.ReleaseMutex(handle))
        return ok, 0 if ok else ctypes.get_last_error()

    def close(self, handle: int) -> tuple[bool, int]:
        ok = bool(self._kernel.CloseHandle(handle))
        return ok, 0 if ok else ctypes.get_last_error()


class InstanceLease:
    """Explicit owner; close on the thread that acquired the runtime mutex."""

    def __init__(self, api: Win32MutexAPI, runtime_handle: int,
                 legacy_handle: int | None, runtime_name: str, *,
                 runtime_owned: bool = True, registered: bool = False):
        self._api = api
        self._runtime_handle: int | None = runtime_handle
        self._legacy_handle = legacy_handle
        self._runtime_owned = runtime_owned
        self._runtime_name = runtime_name.casefold()
        self._registered = registered
        self._thread_id = threading.get_ident()

    @property
    def closed(self) -> bool:
        return self._runtime_handle is None and self._legacy_handle is None

    def close(self) -> None:
        if self.closed:
            return
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("runtime mutex must be released by its acquiring thread")
        if self._runtime_owned:
            ok, error = self._api.release(self._runtime_handle)
            if not ok:
                raise OSError(error, "ReleaseMutex failed")
            self._runtime_owned = False
        if self._runtime_handle is not None:
            ok, error = self._api.close(self._runtime_handle)
            if not ok:
                raise OSError(error, "CloseHandle(runtime) failed")
            self._runtime_handle = None
        if self._legacy_handle is not None:
            ok, error = self._api.close(self._legacy_handle)
            if not ok:
                raise OSError(error, "CloseHandle(legacy) failed")
            self._legacy_handle = None
        if self._registered:
            with _owned_names_lock:
                _owned_names.discard(self._runtime_name)
            self._registered = False

    def __enter__(self) -> InstanceLease:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


Status = Literal["acquired", "already_running", "legacy_instance_present",
                 "access_denied", "error"]


@dataclass(frozen=True)
class AcquisitionResult:
    status: Status
    reason_code: str
    scope: InstanceScope
    lease: InstanceLease | None = None
    win32_error: int | None = None

    @property
    def may_take_over_host_network(self) -> bool:
        return self.scope.may_take_over_host_network


def _failure(scope: InstanceScope, status: Status, reason: str, error: int | None,
             lease: InstanceLease | None = None) -> AcquisitionResult:
    if lease is not None:
        try:
            lease.close()
        except OSError as exc:
            return AcquisitionResult("error", "cleanup_failed", scope, lease, exc.errno)
        except RuntimeError:
            return AcquisitionResult("error", "cleanup_failed", scope, lease)
    return AcquisitionResult(status, reason, scope, win32_error=error)


def _close_unowned_runtime(scope: InstanceScope, api: Win32MutexAPI, handle: int,
                           status: Status, reason: str,
                           error: int | None) -> AcquisitionResult:
    lease = InstanceLease(api, handle, None, scope.names.runtime_mutex, runtime_owned=False)
    try:
        lease.close()
    except OSError as exc:
        return AcquisitionResult("error", "runtime_close_failed", scope, lease, exc.errno)
    except Exception:
        return AcquisitionResult("error", "runtime_close_exception", scope, lease)
    return AcquisitionResult(status, reason, scope, win32_error=error)


def acquire_instance(scope: InstanceScope, *, legacy_gate: Literal["create", "inherited"] = "create",
                     win32: Win32MutexAPI | None = None) -> AcquisitionResult:
    """Try once; only runtime contention identifies a new-protocol instance.

    A returned lease must stay on the acquiring thread and be closed only after
    RT-1 and final sidecar cleanup. The old gate is never waited on.
    """
    if legacy_gate not in ("create", "inherited"):
        return AcquisitionResult("error", "invalid_legacy_gate", scope)
    try:
        api = win32 if win32 is not None else Win32MutexAPI()
    except OSError:
        return AcquisitionResult("error", "win32_unavailable", scope)
    name = scope.names.runtime_mutex.casefold()
    with _owned_names_lock:
        if name in _owned_names:
            return AcquisitionResult("already_running", "runtime_owned_in_process", scope)
    try:
        runtime, error = api.create_mutex(scope.names.runtime_mutex)
    except Exception:
        return AcquisitionResult("error", "runtime_create_exception", scope)
    if not runtime:
        status: Status = "access_denied" if error == ERROR_ACCESS_DENIED else "error"
        reason = "object_access_denied" if status == "access_denied" else "runtime_create_failed"
        return AcquisitionResult(status, reason, scope, win32_error=error)
    if error not in (0, ERROR_ALREADY_EXISTS):
        return _close_unowned_runtime(scope, api, runtime, "error",
                                      "runtime_create_unexpected_error", error)
    try:
        wait_status, error = api.wait(runtime, 0)
    except Exception:
        return _close_unowned_runtime(scope, api, runtime, "error", "runtime_wait_exception", None)
    if wait_status not in (WAIT_OBJECT_0, WAIT_ABANDONED):
        if wait_status == WAIT_TIMEOUT:
            return _close_unowned_runtime(scope, api, runtime,
                                          "already_running", "runtime_owned", None)
        status = "access_denied" if error == ERROR_ACCESS_DENIED else "error"
        reason = "object_access_denied" if status == "access_denied" else "runtime_wait_failed"
        return _close_unowned_runtime(scope, api, runtime, status, reason, error)
    lease = InstanceLease(api, runtime, None, scope.names.runtime_mutex)
    try:
        legacy, error = api.create_mutex(scope.names.legacy_mutex)
    except Exception:
        return _failure(scope, "error", "legacy_create_exception", None, lease)
    if not legacy:
        status = "access_denied" if error == ERROR_ACCESS_DENIED else "error"
        reason = "object_access_denied" if status == "access_denied" else "legacy_create_failed"
        return _failure(scope, status, reason, error, lease)
    lease._legacy_handle = legacy
    if legacy_gate == "inherited" and error == 0:
        return _failure(scope, "error", "legacy_gate_not_held", None, lease)
    if legacy_gate == "create" and error == ERROR_ALREADY_EXISTS:
        return _failure(scope, "legacy_instance_present", "legacy_gate_exists", None, lease)
    if error not in (0, ERROR_ALREADY_EXISTS):
        return _failure(scope, "error", "legacy_create_unexpected_error", error, lease)
    with _owned_names_lock:
        _owned_names.add(name)
    lease._registered = True
    return AcquisitionResult("acquired", "acquired_after_abandonment" if wait_status == WAIT_ABANDONED
                             else "acquired", scope, lease)
