"""Read-only environment evidence required by isolated network audits."""
from collections.abc import Mapping


def windows_process_snapshot_csv() -> str:
    """Toolhelp names/PIDs only; no process handles or tasklist subprocess.

    https://learn.microsoft.com/windows/win32/api/tlhelp32/ns-tlhelp32-processentry32w
    """
    import csv
    import ctypes
    from ctypes import wintypes
    import io
    import sys

    if sys.platform != "win32":
        raise OSError("Windows process snapshot requires Windows")

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(api, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = io.StringIO()
    writer = csv.writer(result)
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not api.Process32FirstW(handle, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            writer.writerow((entry.szExeFile, entry.th32ProcessID))
            if not api.Process32NextW(handle, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES is the sole complete end.
                    raise ctypes.WinError(error)
                break
    finally:
        api.CloseHandle(handle)
    return result.getvalue()


FIELDS = ("default_route_summary", "fake_ip_route_summary", "dns_summary",
          "system_proxy_enabled", "system_proxy_digest")


def network_snapshot_complete(snapshot) -> bool:
    return bool(
        isinstance(snapshot, Mapping)
        and snapshot.get("state") in {
            "external_tun_present", "external_proxy_present", "external_proxy_absent",
        }
        and isinstance(snapshot.get("errors"), (tuple, list)) and not snapshot["errors"]
        and isinstance(snapshot.get("system_proxy_enabled"), bool)
        and isinstance(snapshot.get("system_proxy_digest"), str)
        and all(isinstance(snapshot.get(name), (tuple, list)) for name in FIELDS[:3])
        and snapshot["default_route_summary"]
    )


def external_network_unchanged(before, after) -> bool:
    return (network_snapshot_complete(before) and network_snapshot_complete(after)
            and all(tuple(before[name]) == tuple(after[name]) for name in FIELDS[:3])
            and all(before[name] == after[name] for name in FIELDS[3:]))
