from __future__ import annotations

import ctypes
import os
import socket
import time
from ctypes import wintypes
from dataclasses import dataclass


IS_WINDOWS = os.name == "nt"


if IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = wintypes.WPARAM
    SIZE_T = ctypes.c_size_t

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", SIZE_T),
            ("WorkingSetSize", SIZE_T),
            ("QuotaPeakPagedPoolUsage", SIZE_T),
            ("QuotaPagedPoolUsage", SIZE_T),
            ("QuotaPeakNonPagedPoolUsage", SIZE_T),
            ("QuotaNonPagedPoolUsage", SIZE_T),
            ("PagefileUsage", SIZE_T),
            ("PeakPagefileUsage", SIZE_T),
            ("PrivateUsage", SIZE_T),
        ]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    SYNCHRONIZE = 0x00100000
    DUPLICATE_SAME_ACCESS = 0x00000002


class WindowsJobError(OSError):
    pass


def _raise_last_error(action: str) -> None:
    code = ctypes.get_last_error()
    raise WindowsJobError(code, f"{action} failed", None, code)


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    active_processes: int
    process_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SystemResourceSnapshot:
    commit_headroom_bytes: int
    available_physical_bytes: int


@dataclass(frozen=True, slots=True)
class ProcessResourceSnapshot:
    private_bytes: int
    handle_count: int


def system_resource_snapshot() -> SystemResourceSnapshot:
    if not IS_WINDOWS:
        return SystemResourceSnapshot(2**63 - 1, 2**63 - 1)
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        _raise_last_error("GlobalMemoryStatusEx")
    return SystemResourceSnapshot(
        commit_headroom_bytes=int(status.ullAvailPageFile),
        available_physical_bytes=int(status.ullAvailPhys),
    )


def process_resource_snapshot(pid: int) -> ProcessResourceSnapshot:
    target_pid = int(pid or 0)
    if target_pid <= 0 or not IS_WINDOWS:
        return ProcessResourceSnapshot(0, 0)
    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
        False,
        target_pid,
    )
    if not process:
        process = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
            False,
            target_pid,
        )
    if not process:
        return ProcessResourceSnapshot(0, 0)
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        private_bytes = 0
        if psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            ctypes.sizeof(counters),
        ):
            private_bytes = int(counters.PrivateUsage)
        handle_count = wintypes.DWORD()
        handles = 0
        if kernel32.GetProcessHandleCount(process, ctypes.byref(handle_count)):
            handles = int(handle_count.value)
        return ProcessResourceSnapshot(private_bytes, handles)
    finally:
        kernel32.CloseHandle(process)




class WindowsJob:
    """A non-inheritable Windows Job used as a hard validator kill boundary."""

    def __init__(self, handle: int | None = None, *, owns_handle: bool = True) -> None:
        self._owns_handle = bool(owns_handle)
        if not IS_WINDOWS:
            self.handle = 0
            self._pids: set[int] = set()
            return
        if handle:
            self.handle = int(handle)
            return
        raw_handle = kernel32.CreateJobObjectW(None, None)
        if not raw_handle:
            _raise_last_error("CreateJobObjectW")
        self.handle = int(raw_handle)
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(self.handle),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            _raise_last_error("SetInformationJobObject")

    def duplicate(self) -> "WindowsJob":
        if not IS_WINDOWS:
            duplicate = WindowsJob()
            duplicate._pids = set(self._pids)
            return duplicate
        current = kernel32.GetCurrentProcess()
        duplicate_handle = wintypes.HANDLE()
        if not kernel32.DuplicateHandle(
            current,
            wintypes.HANDLE(self.handle),
            current,
            ctypes.byref(duplicate_handle),
            0,
            False,
            DUPLICATE_SAME_ACCESS,
        ):
            _raise_last_error("DuplicateHandle")
        return WindowsJob(int(duplicate_handle.value), owns_handle=True)

    def assign_pid(self, pid: int) -> None:
        target_pid = int(pid)
        if not IS_WINDOWS:
            self._pids.add(target_pid)
            return
        process = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
            False,
            target_pid,
        )
        if not process:
            _raise_last_error(f"OpenProcess({target_pid})")
        try:
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(self.handle),
                process,
            ):
                _raise_last_error(f"AssignProcessToJobObject({target_pid})")
            in_job = wintypes.BOOL(False)
            if not kernel32.IsProcessInJob(
                process,
                wintypes.HANDLE(self.handle),
                ctypes.byref(in_job),
            ):
                _raise_last_error(f"IsProcessInJob({target_pid})")
            if not bool(in_job.value):
                raise WindowsJobError(f"process {target_pid} was not assigned to validator Job")
        finally:
            kernel32.CloseHandle(process)

    def snapshot(self) -> JobSnapshot:
        if not IS_WINDOWS:
            return JobSnapshot(len(self._pids), tuple(sorted(self._pids)))
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(self.handle),
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            _raise_last_error("QueryInformationJobObject(accounting)")
        capacity = max(8, int(accounting.ActiveProcesses) + 4)
        while True:
            byte_size = ctypes.sizeof(wintypes.DWORD) * 2 + ctypes.sizeof(ULONG_PTR) * capacity
            buffer = ctypes.create_string_buffer(byte_size)
            if kernel32.QueryInformationJobObject(
                wintypes.HANDLE(self.handle),
                JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                buffer,
                byte_size,
                None,
            ):
                header = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
                count = int(header[1])
                offset = ctypes.sizeof(wintypes.DWORD) * 2
                array_type = ULONG_PTR * capacity
                values = ctypes.cast(
                    ctypes.byref(buffer, offset),
                    ctypes.POINTER(array_type),
                ).contents
                pids = tuple(int(values[index]) for index in range(min(count, capacity)))
                return JobSnapshot(int(accounting.ActiveProcesses), pids)
            code = ctypes.get_last_error()
            if code != 122 or capacity >= 4096:
                _raise_last_error("QueryInformationJobObject(process ids)")
            capacity *= 2

    def terminate(self, exit_code: int = 74) -> None:
        if not IS_WINDOWS:
            for pid in tuple(self._pids):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            self._pids.clear()
            return
        if self.handle and not kernel32.TerminateJobObject(
            wintypes.HANDLE(self.handle),
            int(exit_code),
        ):
            _raise_last_error("TerminateJobObject")

    def wait_empty(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            snapshot = self.snapshot()
            if snapshot.active_processes <= 0 and not snapshot.process_ids:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.025)

    def close(self) -> None:
        handle = int(getattr(self, "handle", 0) or 0)
        self.handle = 0
        if IS_WINDOWS and handle and self._owns_handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> "WindowsJob":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def ports_released(ports: tuple[int, ...]) -> bool:
    for port in ports:
        if int(port or 0) <= 0:
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.05)
        try:
            # A refused/errored probe (including a socket policy that forbids the
            # connect) means the port is not accepting for us, i.e. released for
            # this port.  A raise here must never escape into a supervisor
            # reactor thread and kill it.
            if probe.connect_ex(("127.0.0.1", int(port))) == 0:
                return False
        except OSError:
            continue
        finally:
            probe.close()
    return True
