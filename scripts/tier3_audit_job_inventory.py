"""Kernel Job inventory for audit-owned processes, not packet-path evidence.

Job notifications can be dropped. Final coverage must agree with the kernel's
cumulative accounting and the independently registered launch receipts.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Mapping
from ctypes import wintypes


class AuditJobEvents:
    def __init__(self):
        self._events = []
        self._errors = []

    def error(self, value):
        if value not in self._errors:
            self._errors.append(value)

    def observe(self, message, pid):
        if len(self._events) >= 8192:
            self.error('event_limit_exceeded')
            return
        self._events.append(dict(message=message, pid=pid,
                                 observed_at_monotonic=time.monotonic()))

    def snapshot(self, *, total_processes, active_processes, query_stable=True):
        return dict(schema=1, total_processes=total_processes,
                    active_processes=active_processes, query_stable=query_stable,
                    events=[dict(event) for event in self._events],
                    errors=list(self._errors))


def job_inventory_failures(snapshot, *, registered_pids):
    try:
        registered_pids = tuple(registered_pids)
    except TypeError:
        return ['job_inventory_invalid']
    if (not isinstance(snapshot, Mapping) or snapshot.get('schema') != 1
            or any(type(snapshot.get(key)) is not int or snapshot[key] < 0
                   for key in ('total_processes', 'active_processes'))
            or not isinstance(snapshot.get('events'), list)
            or not isinstance(snapshot.get('errors'), list)
            or any(type(pid) is not int or pid <= 0 for pid in registered_pids)):
        return ['job_inventory_invalid']
    failures = []
    if snapshot['errors'] or snapshot.get('query_stable') is not True:
        failures.append('job_inventory_read_unproven')
    if snapshot['active_processes']:
        failures.append('job_processes_still_active')
    if not snapshot['total_processes']:
        failures.append('job_no_processes')
    births, exits = set(), set()
    # A PID may be reused after its process exited (Windows recycles PIDs): only a
    # birth while the same PID is still alive in this Job is a duplicate.  The
    # coverage check counts every birth, so a reused PID is not merged away.
    alive, birth_count = set(), 0
    for event in snapshot['events']:
        if not isinstance(event, Mapping) or type(event.get('message')) is not int:
            failures.append('job_event_invalid')
            continue
        message, pid = event['message'], event.get('pid')
        if message not in {6, 7, 8}:
            continue
        if type(pid) is not int or pid <= 0:
            failures.append('job_event_pid_invalid')
            continue
        if message == 6:
            if pid in alive:
                failures.append(f'job_pid_reused_or_duplicate:{pid}')
            births.add(pid)
            alive.add(pid)
            birth_count += 1
        else:
            exits.add(pid)
            alive.discard(pid)
            if message == 8:
                failures.append(f'job_abnormal_exit:{pid}')
    readbacks = snapshot.get('exit_readbacks', [])
    images = snapshot.get('process_images', [])
    confirmed = set()
    if not isinstance(readbacks, list) or not isinstance(images, list):
        failures.append('job_exit_readback_invalid')
    else:
        image_times = {item.get('pid'): item.get('created_at_filetime')
                       for item in images if isinstance(item, Mapping)
                       and type(item.get('pid')) is int}
        seen = set()
        for item in readbacks:
            if (not isinstance(item, Mapping) or type(item.get('pid')) is not int
                    or item['pid'] not in births or item['pid'] in seen
                    or type(item.get('created_at_filetime')) is not int
                    or item['created_at_filetime'] <= 0
                    or image_times.get(item['pid']) != item['created_at_filetime']
                    or type(item.get('exit_confirmed')) is not bool):
                failures.append('job_exit_readback_invalid')
                continue
            pid = item['pid']
            seen.add(pid)
            if item['exit_confirmed']:
                if type(item.get('exitcode')) is not int or not 0 <= item['exitcode'] <= 0xffffffff:
                    failures.append('job_exit_readback_invalid')
                    continue
                confirmed.add(pid)
                if item['exitcode'] >= 0xc0000000:
                    failures.append(f'job_abnormal_exit:{pid}')
            else:
                if item.get('exitcode') is not None:
                    failures.append('job_exit_readback_invalid')
                if pid in exits or snapshot['active_processes'] == 0:
                    failures.append(f'job_retained_process_still_active:{pid}')
    if birth_count != snapshot['total_processes'] or births != exits | confirmed:
        failures.append('job_notification_coverage_unproven')
    failures.extend(f'job_process_unregistered:{pid}' for pid in sorted(births - set(registered_pids)))
    failures.extend(f'job_registered_process_unobserved:{pid}' for pid in sorted(set(registered_pids) - births))
    return failures


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in (
        'TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')]
    _fields_ += [(name, wintypes.DWORD) for name in (
        'TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]


class _Association(ctypes.Structure):
    _fields_ = [('CompletionKey', ctypes.c_void_p), ('CompletionPort', wintypes.HANDLE)]


class WindowsAuditJobInventory:
    """Observe only an explicitly supplied, initially empty audit Job."""

    _KEY = 0x7433

    def __init__(self, job, *, observe_images=False):
        if os.name != 'nt':
            raise OSError('audit Job inventory requires Windows')
        self._job = job
        self._lock = threading.Lock()
        self._events = AuditJobEvents()
        self._port = None
        self._images = {}
        self._process_handles = {}
        self._observe_images = observe_images
        self._stop = threading.Event()
        self._thread = None
        self._api = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self._api
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                               ctypes.c_size_t, wintypes.DWORD]
        api.CreateIoCompletionPort.restype = wintypes.HANDLE
        api.GetQueuedCompletionStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                                   ctypes.POINTER(ctypes.c_size_t),
                                                   ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD]
        api.GetQueuedCompletionStatus.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.QueryFullProcessImageNameW.restype = wintypes.BOOL
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetProcessTimes.restype = wintypes.BOOL
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        if self._accounting() != (0, 0):
            raise ValueError('audit Job witness requires an empty, unused Job')
        port = api.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
        if not port:
            raise ctypes.WinError(ctypes.get_last_error())
        association = _Association(self._KEY, port)
        if not api.SetInformationJobObject(job.handle, 7, ctypes.byref(association),
                                            ctypes.sizeof(association)):
            error = ctypes.get_last_error()
            api.CloseHandle(port)
            raise ctypes.WinError(error)
        self._port = port
        if observe_images:
            self._thread = threading.Thread(target=self._observe, name='audit-job-inventory', daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self.close()
                raise

    def _read_image(self, pid):
        if pid in self._process_handles:
            self._events.error(f'duplicate_process_handle:{pid}')
            return
        handle = self._api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            self._events.error(f'process_image_unproven:{pid}:{ctypes.get_last_error()}')
            return
        # Retain the process object until finalization, preventing PID reuse
        # from turning a later process into an earlier launch receipt.
        self._process_handles[pid] = handle
        name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        times = [wintypes.FILETIME() for _ in range(4)]
        if (not self._api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length))
                or not self._api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))):
            self._events.error(f'process_image_unproven:{pid}:{ctypes.get_last_error()}')
            return
        self._images[pid] = dict(pid=pid, image_path=name.value,
                                created_at_filetime=(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)

    def _read_exits(self):
        results = []
        for pid, handle in self._process_handles.items():
            image = self._images.get(pid)
            if image is None:
                continue
            wait = self._api.WaitForSingleObject(handle, 0)
            if wait not in (0, 258):
                self._events.error(f'process_exit_unproven:{pid}:{ctypes.get_last_error()}')
                continue
            code = wintypes.DWORD()
            if wait == 0 and not self._api.GetExitCodeProcess(handle, ctypes.byref(code)):
                self._events.error(f'process_exit_unproven:{pid}:{ctypes.get_last_error()}')
                continue
            results.append(dict(pid=pid, created_at_filetime=image['created_at_filetime'],
                                exit_confirmed=wait == 0, exitcode=code.value if wait == 0 else None))
        return results

    def _observe(self):
        while not self._stop.wait(.005):
            try:
                with self._lock:
                    self._drain()
            except Exception as exc:
                with self._lock:
                    self._events.error(f'job_observer_failed:{type(exc).__name__}')
                return

    def _accounting(self):
        accounting = _Accounting()
        if not self._api.QueryInformationJobObject(
                self._job.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.TotalProcesses), int(accounting.ActiveProcesses)

    def _drain(self):
        for _ in range(8192):
            message, key, value = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
            if not self._api.GetQueuedCompletionStatus(
                    self._port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(value), 0):
                error = ctypes.get_last_error()
                if error != 258:  # WAIT_TIMEOUT means the queue is empty.
                    self._events.error(f'completion_read_failed:{error}')
                return
            if key.value != self._KEY:
                self._events.error('unexpected_completion_key')
                continue
            self._events.observe(int(message.value), int(value.value or 0))
            if self._observe_images and message.value == 6:
                self._read_image(int(value.value or 0))
        self._events.error('completion_drain_limit')

    def snapshot(self):
        with self._lock:
            if self._port is None:
                raise RuntimeError('audit Job inventory is closed')
            before = self._accounting()
            self._drain()
            readbacks = self._read_exits()
            after = self._accounting()
            result = self._events.snapshot(total_processes=after[0], active_processes=after[1],
                                           query_stable=before == after)
            result['process_images'] = [dict(item) for item in self._images.values()]
            result['exit_readbacks'] = readbacks
            result['image_observation_enabled'] = self._observe_images
            return result

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError('audit Job observer did not exit')
        with self._lock:
            if self._port is None:
                return
            # This detaches only this audit Job, not a system-wide monitor.
            association = _Association(self._KEY, None)
            detached = self._api.SetInformationJobObject(
                self._job.handle, 7, ctypes.byref(association), ctypes.sizeof(association))
            error = ctypes.get_last_error()
            self._api.CloseHandle(self._port)
            self._port = None
            for handle in self._process_handles.values():
                self._api.CloseHandle(handle)
            self._process_handles.clear()
            if not detached:
                raise ctypes.WinError(error)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
