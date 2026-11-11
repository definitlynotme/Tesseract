"""Windows audit launch with Job membership established before user code runs.

This is lifecycle containment, not proof of the process's network path. No
production process launch is changed by importing this module.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from scripts.tier3_audit_process_registry import AuditNativeProcessRegistry


class _StartupInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
                ('lpDesktop', wintypes.LPWSTR), ('lpTitle', wintypes.LPWSTR)]
    _fields_ += [(name, wintypes.DWORD) for name in (
        'dwX', 'dwY', 'dwXSize', 'dwYSize', 'dwXCountChars', 'dwYCountChars',
        'dwFillAttribute', 'dwFlags')]
    _fields_ += [('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
                ('lpReserved2', ctypes.c_void_p), ('hStdInput', wintypes.HANDLE),
                ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', _StartupInfo), ('lpAttributeList', ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]


def _kernel():
    if os.name != 'nt':
        raise OSError('audit Job launch requires Windows')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    declarations = {
        'InitializeProcThreadAttributeList': (wintypes.BOOL, [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]),
        'UpdateProcThreadAttribute': (wintypes.BOOL, [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]),
        'DeleteProcThreadAttributeList': (None, [ctypes.c_void_p]),
        'CreateProcessW': (wintypes.BOOL, [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation)]),
        'DuplicateHandle': (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        'GetCurrentProcess': (wintypes.HANDLE, []),
        'IsProcessInJob': (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]),
        'ResumeThread': (wintypes.DWORD, [wintypes.HANDLE]),
        'WaitForSingleObject': (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        'GetExitCodeProcess': (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
        'TerminateProcess': (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        'CloseHandle': (wintypes.BOOL, [wintypes.HANDLE]),
        'GetSystemDirectoryW': (wintypes.UINT, [wintypes.LPWSTR, wintypes.UINT]),
    }
    for name, (result, args) in declarations.items():
        getattr(api, name).restype = result
        getattr(api, name).argtypes = args
    return api


def system_console_host_path():
    buffer = ctypes.create_unicode_buffer(32768)
    length = _kernel().GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError('cannot determine the Windows system directory')
    return Path(buffer.value) / 'conhost.exe'


def _environment_block(environment):
    if environment is None:
        return None
    entries = []
    for key, value in environment.items():
        if (not isinstance(key, str) or not isinstance(value, str) or not key
                or '\0' in key or '\0' in value or '=' in key):
            raise ValueError('invalid audit environment entry')
        entries.append(f'{key}={value}')
    entries.sort(key=str.upper)
    return ctypes.create_unicode_buffer('\0'.join(entries) + '\0\0')


class AuditJobProcess:
    def __init__(self, api, handle, pid):
        self._api, self._handle, self.pid = api, handle, int(pid)
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._handle is None:
            raise RuntimeError('audit process handle is closed')
        status = self._api.WaitForSingleObject(self._handle, 0)
        if status == 258:
            return None
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is None:
            milliseconds = 0xFFFFFFFF if timeout is None else min(
                0xFFFFFFFE, max(0, int(timeout * 1000)))
            status = self._api.WaitForSingleObject(self._handle, milliseconds)
            if status == 258:
                raise subprocess.TimeoutExpired('audit-owned process', timeout)
            if status != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        if self.poll() is None and not self._api.TerminateProcess(self._handle, 1):
            error = ctypes.get_last_error()
            if self.poll() is None:
                raise ctypes.WinError(error)

    def close(self):
        if self._handle is not None:
            if self.poll() is None:
                raise RuntimeError('cannot discard a live audit process handle')
            self._api.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.terminate()
        self.wait(timeout=5)
        self.close()


def launch_in_audit_job(job, args, *, stdout, env=None, cwd=None, on_created=None):
    """Create in the supplied Job, register while suspended, then resume.

    Only duplicate stdin/log handles are inherited. Failure never falls back to
    ordinary Popen or a post-start assignment. ``on_created`` must not run the
    child; a rejected receipt terminates this owned suspended process.
    """
    if (not args or isinstance(args, (str, bytes))
            or not Path(args[0]).is_absolute() or not job.handle):
        raise ValueError('audit launch requires an explicit executable and Job')
    api = _kernel()
    import msvcrt

    environment = _environment_block(env)
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([os.fspath(a) for a in args]))
    size = ctypes.c_size_t()
    api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = ctypes.create_string_buffer(size.value)
    if not api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    inherited = []
    process = None
    info = _ProcessInformation()
    try:
        with open(os.devnull, 'rb') as empty_input:
            for stream in (empty_input, stdout):
                duplicate = wintypes.HANDLE()
                if not api.DuplicateHandle(
                        api.GetCurrentProcess(), msvcrt.get_osfhandle(stream.fileno()),
                        api.GetCurrentProcess(), ctypes.byref(duplicate), 0, True, 2):
                    raise ctypes.WinError(ctypes.get_last_error())
                inherited.append(duplicate.value)
        handles = (wintypes.HANDLE * len(inherited))(*inherited)
        jobs = (wintypes.HANDLE * 1)(job.handle)
        for key, value in ((0x20002, handles), (0x2000D, jobs)):
            if not api.UpdateProcThreadAttribute(
                    attributes, 0, key, value, ctypes.sizeof(value), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100 | 0x1  # USESTDHANDLES | USESHOWWINDOW
        startup.StartupInfo.hStdInput = inherited[0]
        startup.StartupInfo.hStdOutput = inherited[1]
        startup.StartupInfo.hStdError = inherited[1]
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        flags = 0x80000 | 0x400 | 0x4 | 0x08000000  # EXTENDED | UNICODE | SUSPENDED | NO_WINDOW
        if not api.CreateProcessW(os.fspath(args[0]), command, None, None, True, flags,
                                  environment, os.fspath(cwd) if cwd else None,
                                  ctypes.byref(startup), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = AuditJobProcess(api, info.hProcess, info.dwProcessId)
        member = wintypes.BOOL()
        if not api.IsProcessInJob(info.hProcess, job.handle, ctypes.byref(member)) or not member.value:
            raise RuntimeError('new audit process has no verified Job membership')
        if on_created is not None:
            on_created(process)
        if api.ResumeThread(info.hThread) != 1:
            raise RuntimeError('audit process could not be resumed exactly once')
        return process
    except BaseException:
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
            process.close()
        raise
    finally:
        if info.hThread:
            api.CloseHandle(info.hThread)
        for handle in inherited:
            api.CloseHandle(handle)
        api.DeleteProcThreadAttributeList(attributes)


class AuditJobLaunchRegistry:
    """Launcher-owned DNS/application attempts, including failure and exit."""

    def __init__(self):
        self._registry = AuditNativeProcessRegistry(lambda _snapshot: None)
        self._roles = {}
        self._processes = {}

    def launch(self, job, role, args, *, config_path, stdout, env=None, cwd=None):
        if role not in {'application', 'resolver'}:
            raise ValueError('unknown audit launcher role')
        key = self._registry.begin()
        self._roles[key] = role
        error = None

        def created(process):
            self._processes[key] = process
            self._registry.launched(key, process.pid, None)

        try:
            with Path(args[0]).open('rb') as binary:
                executable_hash = hashlib.file_digest(binary, 'sha256').hexdigest()
            config_hash = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
            self._registry.configured(key, config_hash, executable_hash)
            self._registry.creating(key)
            return launch_in_audit_job(job, args, stdout=stdout, env=env, cwd=cwd, on_created=created)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            process = self._processes.get(key)
            self._registry.launched(key, process.pid if process else None, error)

    def snapshot(self):
        for key, process in self._processes.items():
            self._registry.exited(key, process.poll())
        result = self._registry.snapshot()
        for attempt in result['attempts']:
            attempt['role'] = self._roles[attempt['attempt_id']]
        return result

    def close(self):
        for process in self._processes.values():
            process.terminate()
        for process in self._processes.values():
            process.wait(timeout=5)
            process.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
