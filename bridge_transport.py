"""Windows overlapped, single-handle NDJSON transport for the sidecar.

The shell owns both pipe ends and inherits only the overlapped client handle
into this process. A pending write owns its buffer until completion is reaped.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from collections import deque
from ctypes import wintypes

import bridge_protocol as protocol


ERROR_IO_PENDING = 997
ERROR_IO_INCOMPLETE = 996
ERROR_OPERATION_ABORTED = 995
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_NOT_FOUND = 1168
PIPE_ERRORS = {ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED}
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
HANDLE_FLAG_INHERIT = 1
SYNCHRONIZE = 0x00100000


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


if os.name == "nt":
    # WinDLL releases the GIL. ReadFile/WriteFile can block even on an
    # overlapped handle, and WaitForSingleObject/join-like waits do block.
    _blocking = ctypes.WinDLL("kernel32", use_last_error=True)
    # These calls are immediate queries, cancellation requests, or handle
    # bookkeeping. PyDLL avoids a GIL handoff for each bridge tick.
    _immediate = ctypes.PyDLL("kernel32", use_last_error=True)
    _blocking.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    _blocking.ReadFile.restype = wintypes.BOOL
    _blocking.WriteFile.argtypes = _blocking.ReadFile.argtypes
    _blocking.WriteFile.restype = wintypes.BOOL
    _blocking.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _blocking.WaitForSingleObject.restype = wintypes.DWORD
    _blocking.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _blocking.OpenProcess.restype = wintypes.HANDLE
    _blocking.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _blocking.PeekNamedPipe.restype = wintypes.BOOL
    _immediate.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                               ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    _immediate.GetOverlappedResult.restype = wintypes.BOOL
    _immediate.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    _immediate.CancelIoEx.restype = wintypes.BOOL
    _immediate.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _immediate.SetHandleInformation.restype = wintypes.BOOL
    _immediate.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    _immediate.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    _immediate.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                        wintypes.LPCWSTR]
    _immediate.CreateEventW.restype = wintypes.HANDLE
    _immediate.CloseHandle.argtypes = [wintypes.HANDLE]
    _immediate.CloseHandle.restype = wintypes.BOOL


def _error(operation: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{operation}: {ctypes.FormatError(code)}")


def _event() -> int:
    handle = _immediate.CreateEventW(None, True, False, None)
    if not handle:
        raise _error("CreateEventW")
    return handle


class PipeTransport:
    """One reader thread and one caller-owned writer; no transport send queue.

    ``send`` returns ``'complete'``, ``'pending'``, or ``'busy'``. On busy,
    the caller retains/coalesces its new frame. ``poll_write`` returns the
    current write state; pending data must never be submitted again. The
    writer thread must remain alive until its submitted IO completes: Windows
    cancels an originating thread's unfinished overlapped IO on thread exit.
    """

    def __init__(self, handle: int, shell_pid: int, *, on_disconnect=None):
        if os.name != "nt":
            raise OSError("Windows named pipes are required")
        if not handle or shell_pid <= 0:
            raise ValueError("invalid pipe handle or shell PID")
        self.handle = handle
        self._closed = threading.Event()
        self._disconnected = threading.Event()
        self._disconnect_lock = threading.Lock()
        self._on_disconnect = on_disconnect
        self._inbox: queue.Queue[dict] = queue.Queue(maxsize=64)
        self.diagnostics: deque[str] = deque(maxlen=128)
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._finalized = False
        self._write_buffer = None
        self._write_expected = 0
        self._write_pending = False
        self._read_event = None
        self._write_event = None
        self._shell_process = None
        try:
            if not _immediate.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0):
                raise _error("SetHandleInformation")
            actual_pid = wintypes.ULONG()
            if not _immediate.GetNamedPipeServerProcessId(handle, ctypes.byref(actual_pid)):
                raise _error("GetNamedPipeServerProcessId")
            if actual_pid.value != shell_pid:
                raise ValueError("pipe_server_pid_mismatch")
            # Hold the verified process object so PID reuse cannot redirect
            # liveness checks. OpenProcess may cross a security boundary and
            # wait for kernel access, so it uses WinDLL and releases the GIL.
            self._shell_process = _blocking.OpenProcess(SYNCHRONIZE, False, shell_pid)
            if not self._shell_process:
                raise _error("OpenProcess(shell)")
            self._read_event = _event()
            self._write_event = _event()
            self._read_op = OVERLAPPED()
            self._write_op = OVERLAPPED()
            self._read_op.hEvent = self._read_event
            self._write_op.hEvent = self._write_event
            self._reader = threading.Thread(target=self._read_loop, name="bridge-pipe-read", daemon=True)
            self._shell_watcher = threading.Thread(target=self._watch_shell,
                                                   name="bridge-shell-watch", daemon=True)
            self._shell_watcher.start()
            self._reader.start()
        except BaseException:
            for resource in (self._read_event, self._write_event, self._shell_process, handle):
                if resource:
                    _immediate.CloseHandle(resource)
            raise

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    def _mark_disconnected(self) -> None:
        with self._disconnect_lock:
            if self._disconnected.is_set():
                return
            self._disconnected.set()
            notify = self._on_disconnect is not None and not self._closed.is_set()
        if notify:
            # User shutdown callbacks may call close(); never invoke one
            # from the reader or while holding the writer's lock.
            threading.Thread(target=self._on_disconnect,
                             name="bridge-pipe-disconnect", daemon=True).start()

    def _watch_shell(self) -> None:
        # A full inbox can park the reader before its next ReadFile. Poll the
        # verified process object independently so shell death still notifies
        # the owner. WaitForSingleObject releases the GIL during each wait.
        while not self._closed.is_set():
            outcome = _blocking.WaitForSingleObject(self._shell_process, 100)
            if outcome == WAIT_OBJECT_0:
                if not self._closed.is_set():
                    self._mark_disconnected()
                return
            if outcome != WAIT_TIMEOUT:
                self.diagnostics.append("shell_wait_failed")
                self._mark_disconnected()
                return

    def _read_loop(self) -> None:
        chunk = ctypes.create_string_buffer(64 * 1024)
        line = bytearray()
        discarding = False
        try:
            while not self._closed.is_set():
                count = wintypes.DWORD()
                with self._read_lock:
                    if self._closed.is_set():
                        return
                    ctypes.memset(ctypes.byref(self._read_op), 0, ctypes.sizeof(OVERLAPPED))
                    self._read_op.hEvent = self._read_event
                    ok = _blocking.ReadFile(self.handle, chunk, len(chunk), ctypes.byref(count),
                                            ctypes.byref(self._read_op))
                if not ok:
                    code = ctypes.get_last_error()
                    if code == ERROR_IO_PENDING:
                        outcome = _blocking.WaitForSingleObject(self._read_event, 0xFFFFFFFF)
                        if outcome != WAIT_OBJECT_0:
                            raise _error("WaitForSingleObject(read)")
                        if not _immediate.GetOverlappedResult(self.handle, ctypes.byref(self._read_op),
                                                               ctypes.byref(count), False):
                            code = ctypes.get_last_error()
                            if code == ERROR_OPERATION_ABORTED and self._closed.is_set():
                                return
                            if code in PIPE_ERRORS:
                                break
                            raise _error("GetOverlappedResult(read)")
                    elif code in PIPE_ERRORS:
                        break
                    elif code == ERROR_OPERATION_ABORTED and self._closed.is_set():
                        return
                    else:
                        raise _error("ReadFile")
                if count.value == 0:
                    break
                data = chunk.raw[:count.value]
                while data:
                    head, sep, tail = data.partition(b"\n")
                    size = len(line) + len(head) + (1 if sep else 0)
                    if size > protocol.HARD_FRAME_LIMIT:
                        if not discarding:
                            self.diagnostics.append("frame_too_large")
                        discarding = True
                        line.clear()
                    elif not discarding:
                        line.extend(head)
                    if sep:
                        if not discarding:
                            try:
                                frame = protocol.decode_line(bytes(line) + b"\n")
                            except ValueError as exc:
                                self.diagnostics.append(str(exc))
                            else:
                                while not self._closed.is_set():
                                    try:
                                        self._inbox.put(frame, timeout=0.1)
                                        break
                                    except queue.Full:
                                        # Keep the queue bounded, but do not let
                                        # backpressure hide a closed server end.
                                        # PeekNamedPipe does no read; WinDLL
                                        # releases the GIL if the call stalls.
                                        if not _blocking.PeekNamedPipe(self.handle, None, 0,
                                                                       None, None, None):
                                            code = ctypes.get_last_error()
                                            if code in PIPE_ERRORS:
                                                return
                                            raise _error("PeekNamedPipe")
                                        continue
                        line.clear()
                        discarding = False
                    data = tail if sep else b""
        except OSError as exc:
            self.diagnostics.append(f"io:{exc.errno}")
        except Exception as exc:
            self.diagnostics.append(f"read:{type(exc).__name__}")
        finally:
            if not self._closed.is_set():
                self._mark_disconnected()

    def receive(self, timeout: float | None = None) -> dict | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                return self._inbox.get(timeout=0.1 if remaining is None else min(remaining, 0.1))
            except queue.Empty:
                if self.disconnected or self._closed.is_set():
                    return None
                if deadline is not None and time.monotonic() >= deadline:
                    return None

    def _finish_write(self) -> str:
        count = wintypes.DWORD()
        if not _immediate.GetOverlappedResult(self.handle, ctypes.byref(self._write_op),
                                               ctypes.byref(count), False):
            code = ctypes.get_last_error()
            if code == ERROR_IO_INCOMPLETE:
                return "pending"
            self._write_pending = False
            self._write_buffer = None
            if code in PIPE_ERRORS or code == ERROR_OPERATION_ABORTED:
                self._mark_disconnected()
            raise OSError(code, f"GetOverlappedResult(write): {ctypes.FormatError(code)}")
        self._write_pending = False
        self._write_buffer = None
        if count.value != self._write_expected:
            self._mark_disconnected()
            raise OSError("partial_pipe_write")
        return "complete"

    def poll_write(self) -> str:
        with self._write_lock:
            if self.disconnected or self._closed.is_set():
                raise BrokenPipeError("pipe disconnected")
            return self._finish_write() if self._write_pending else "complete"

    def send(self, frame: dict) -> str:
        with self._write_lock:
            if self.disconnected or self._closed.is_set():
                raise BrokenPipeError("pipe disconnected")
            if self._write_pending and self._finish_write() == "pending":
                return "busy"
            payload = protocol.encode_frame(frame)
            self._write_buffer = ctypes.create_string_buffer(payload)
            self._write_expected = len(payload)
            ctypes.memset(ctypes.byref(self._write_op), 0, ctypes.sizeof(OVERLAPPED))
            self._write_op.hEvent = self._write_event
            count = wintypes.DWORD()
            ok = _blocking.WriteFile(self.handle, self._write_buffer, len(payload),
                                     ctypes.byref(count), ctypes.byref(self._write_op))
            if not ok:
                code = ctypes.get_last_error()
                if code == ERROR_IO_PENDING:
                    self._write_pending = True
                    return "pending"  # Submitted exactly once; buffer remains alive.
                self._write_buffer = None
                if code in PIPE_ERRORS:
                    self._mark_disconnected()
                raise _error("WriteFile")
            self._write_buffer = None
            if count.value != len(payload):
                self._mark_disconnected()
                raise OSError("partial_pipe_write")
            return "complete"

    def close(self) -> None:
        with self._close_lock:
            if self._finalized:
                return
            # Serialize against WriteFile submission. Without this lock close
            # could miss an operation that becomes pending just afterwards.
            with self._write_lock, self._read_lock:
                self._closed.set()
                for op in (self._read_op, self._write_op if self._write_pending else None):
                    if op is not None and not _immediate.CancelIoEx(self.handle, ctypes.byref(op)):
                        if ctypes.get_last_error() != ERROR_NOT_FOUND:
                            self.diagnostics.append(f"cancel:{ctypes.get_last_error()}")
            self._reader.join(timeout=5)
            if self._reader.is_alive():
                raise TimeoutError("read cancellation did not complete")
            self._shell_watcher.join(timeout=5)
            if self._shell_watcher.is_alive():
                raise TimeoutError("shell watcher did not complete")
            if self._write_pending:
                outcome = _blocking.WaitForSingleObject(self._write_event, 5000)
                if outcome != WAIT_OBJECT_0:
                    raise TimeoutError("write cancellation did not complete")
                try:
                    self._finish_write()
                except OSError as exc:
                    if exc.errno != ERROR_OPERATION_ABORTED:
                        self.diagnostics.append(f"write_close:{exc.errno}")
            for resource in (self._read_event, self._write_event, self._shell_process, self.handle):
                _immediate.CloseHandle(resource)
            self._finalized = True
            self._mark_disconnected()
