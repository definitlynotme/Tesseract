from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ACL_REVISION = 2
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_FILE_ALL_ACCESS = 0x001F01FF
_ERROR_INSUFFICIENT_BUFFER = 122

_STATUS_LOCK = threading.RLock()
_UNCONFIRMED_PATHS: dict[str, str] = {}


@dataclass(frozen=True)
class CurrentUserAccessResult:
    path: str
    confirmed: bool
    detail: str = ""


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("sid", wintypes.LPVOID),
        ("attributes", wintypes.DWORD),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class _Acl(ctypes.Structure):
    _fields_ = [
        ("revision", ctypes.c_ubyte),
        ("sbz1", ctypes.c_ubyte),
        ("size", wintypes.WORD),
        ("ace_count", wintypes.WORD),
        ("sbz2", wintypes.WORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", wintypes.WORD),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("header", _AceHeader),
        ("mask", wintypes.DWORD),
        ("sid_start", wintypes.DWORD),
    ]


def restrict_to_current_user(path: str | Path) -> CurrentUserAccessResult:
    """Best-effort protection for local configuration and backup material.

    Windows receives a protected DACL containing one full-control ACE for the
    current user. On POSIX, the closest equivalent is mode 0700 for directories
    and 0600 for files. Failures are retained for the UI security warning and do
    not make an otherwise valid configuration write unrecoverable.
    """

    target = Path(path)
    if not target.exists():
        return _remember_result(target, False, "path does not exist")
    try:
        if os.name == "nt":
            _restrict_windows_path(target)
        else:
            target.chmod(0o700 if target.is_dir() else 0o600)
    except Exception as exc:
        return _remember_result(
            target,
            False,
            f"{type(exc).__name__}: {exc}",
        )
    return _remember_result(target, True, "")


def current_user_access_warnings() -> tuple[CurrentUserAccessResult, ...]:
    """Return paths whose current-user-only access could not be confirmed."""

    with _STATUS_LOCK:
        return tuple(
            CurrentUserAccessResult(path=path, confirmed=False, detail=detail)
            for path, detail in sorted(_UNCONFIRMED_PATHS.items())
        )


def _remember_result(
    path: Path,
    confirmed: bool,
    detail: str,
) -> CurrentUserAccessResult:
    normalized = str(path.resolve())
    with _STATUS_LOCK:
        if confirmed:
            _UNCONFIRMED_PATHS.pop(normalized, None)
        else:
            _UNCONFIRMED_PATHS[normalized] = detail
    return CurrentUserAccessResult(
        path=normalized,
        confirmed=confirmed,
        detail=detail,
    )


def _restrict_windows_path(path: Path) -> None:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = (wintypes.LPVOID,)
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.InitializeAcl.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        _raise_last_windows_error("OpenProcessToken")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        error = ctypes.get_last_error()
        if error != _ERROR_INSUFFICIENT_BUFFER or required.value <= 0:
            raise OSError(error, "GetTokenInformation size query failed")
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            token_buffer,
            required.value,
            ctypes.byref(required),
        ):
            _raise_last_windows_error("GetTokenInformation")
        user_sid = ctypes.cast(
            token_buffer,
            ctypes.POINTER(_TokenUser),
        ).contents.user.sid
        sid_length = int(advapi32.GetLengthSid(user_sid))
        if sid_length <= 0:
            _raise_last_windows_error("GetLengthSid")

        ace_size = ctypes.sizeof(_AccessAllowedAce) - ctypes.sizeof(
            wintypes.DWORD
        ) + sid_length
        acl_size = ctypes.sizeof(_Acl) + ace_size
        acl_buffer = ctypes.create_string_buffer(acl_size)
        if not advapi32.InitializeAcl(
            acl_buffer,
            acl_size,
            _ACL_REVISION,
        ):
            _raise_last_windows_error("InitializeAcl")
        inheritance_flags = (
            _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
            if path.is_dir()
            else 0
        )
        if not advapi32.AddAccessAllowedAceEx(
            acl_buffer,
            _ACL_REVISION,
            inheritance_flags,
            _FILE_ALL_ACCESS,
            user_sid,
        ):
            _raise_last_windows_error("AddAccessAllowedAceEx")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION
            | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl_buffer,
            None,
        )
        if result:
            raise OSError(int(result), "SetNamedSecurityInfoW failed")
    finally:
        kernel32.CloseHandle(token)


def _raise_last_windows_error(operation: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed")
