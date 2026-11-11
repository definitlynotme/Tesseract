"""Batch validator owner Pipe I/O that keeps the GIL on calls that cannot block.

Why this exists (measured 2026-09-25 on the physical path, 8 helpers x 8 identities):
the batch supervisor's single owner thread handles every helper frame of up to 64
identities.  Each ``_winapi`` Pipe call (PeekNamedPipe, ReadFile, GetOverlappedResult,
WriteFile) releases the GIL.  In the loaded manager process some other thread is
always waiting for the GIL, so after every such call the owner queues for a full GIL
handoff, which CPython implements as a timed wait that Windows rounds up to the timer
tick (median 16 ms per PeekNamedPipe).  With ~3 calls per frame the owner stalled for
tens of seconds and the validators idled.  Holding the GIL through the peek alone cut
the median peek to 0.03 ms and multiplied frames handled 2.8x in the same conditions.

PeekNamedPipe, a ReadFile of a message that is already queued and a WriteFile that
fits in the Pipe buffer all complete without waiting, so they are issued through
``ctypes.PyDLL`` (which does not release the GIL).  Only an operation that really
pends waits through ``ctypes.WinDLL`` (which does).  Every other state falls back to
the standard library path, so semantics match ``tier3_pipe_wait.receive_ready_pipe_message``,
``tier3_validator_supervisor._pipe_has_queued_message`` and
``multiprocessing.connection.PipeConnection._send_bytes``.
"""

from __future__ import annotations

import errno
import multiprocessing.connection
import os
import threading
from typing import Any

from tier3_pipe_wait import receive_ready_pipe_message
from tier3_validator_supervisor import _pipe_has_queued_message

_ENABLED = os.name == "nt"

if _ENABLED:
    import ctypes
    from ctypes import wintypes

    _ERROR_BROKEN_PIPE = 109
    _ERROR_NO_DATA = 232
    _ERROR_MORE_DATA = 234
    _ERROR_IO_PENDING = 997
    _ERROR_OPERATION_ABORTED = 995
    _ERROR_NETNAME_DELETED = 64
    _PIPE_GONE = {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_NETNAME_DELETED}

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _LPDWORD = ctypes.POINTER(wintypes.DWORD)
    _LPOVERLAPPED = ctypes.POINTER(_OVERLAPPED)
    # GIL held for the duration of the call: only for calls that cannot block.
    _hold = ctypes.PyDLL("kernel32", use_last_error=True)
    # GIL released for the duration of the call: only for real waits.
    _wait = ctypes.WinDLL("kernel32", use_last_error=True)

    def _bind(library: Any, name: str, restype: Any, argtypes: tuple[Any, ...]) -> Any:
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    _peek_hold = _bind(_hold, "PeekNamedPipe", wintypes.BOOL, (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, _LPDWORD, _LPDWORD, _LPDWORD,
    ))
    _read_hold = _bind(_hold, "ReadFile", wintypes.BOOL, (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, _LPDWORD, _LPOVERLAPPED,
    ))
    _write_hold = _bind(_hold, "WriteFile", wintypes.BOOL, (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, _LPDWORD, _LPOVERLAPPED,
    ))
    _result_hold = _bind(_hold, "GetOverlappedResult", wintypes.BOOL, (
        wintypes.HANDLE, _LPOVERLAPPED, _LPDWORD, wintypes.BOOL,
    ))
    _result_wait = _bind(_wait, "GetOverlappedResult", wintypes.BOOL, (
        wintypes.HANDLE, _LPOVERLAPPED, _LPDWORD, wintypes.BOOL,
    ))
    _cancel_hold = _bind(_hold, "CancelIoEx", wintypes.BOOL, (wintypes.HANDLE, _LPOVERLAPPED))
    _create_event_hold = _bind(_hold, "CreateEventW", wintypes.HANDLE, (
        ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
    ))

    _reset_event_hold = _bind(_hold, "ResetEvent", wintypes.BOOL, (wintypes.HANDLE,))
    _close_handle_hold = _bind(_hold, "CloseHandle", wintypes.BOOL, (wintypes.HANDLE,))
    _STATUS_PENDING = 0x103

    _events = threading.local()

    class _ThreadEvent:
        """The calling thread's manual-reset event, closed with the thread's locals.

        Every ``_overlapped_io`` call has finished with the event before it returns
        (also on exceptions), so nothing can still reference it at thread exit.
        """

        __slots__ = ("handle",)

        def __init__(self) -> None:
            handle = _create_event_hold(None, True, False, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle

        def __del__(self) -> None:
            handle, self.handle = self.handle, None
            if handle:
                try:
                    _close_handle_hold(handle)
                except Exception:  # noqa: BLE001 - interpreter shutdown
                    pass

    def _thread_event() -> int:
        """A manual-reset event owned by the calling thread (reset by each new I/O)."""

        holder = getattr(_events, "event", None)
        if holder is None:
            holder = _events.event = _ThreadEvent()
        return holder.handle

    def _wait_complete(handle: int, overlapped: Any, transferred: Any) -> bool:
        """Wait, GIL released, until Windows has finished with ``overlapped``.

        A signal that arrives while the OVERLAPPED still reads STATUS_PENDING is not
        a completion: reset the event, re-check, and wait again (a completion between
        the reset and the check is seen by the check, so none is lost).
        """

        while True:
            completed = _result_wait(
                handle, ctypes.byref(overlapped), ctypes.byref(transferred), True,
            )
            if (overlapped.Internal or 0) != _STATUS_PENDING:
                return bool(completed)
            _reset_event_hold(overlapped.hEvent)
            if (overlapped.Internal or 0) != _STATUS_PENDING:
                _result_hold(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False)
                return True

    class _PendingSend:
        """What ``PipeConnection._close`` cancels while a send is really pending."""

        def __init__(self, handle: int, overlapped: Any) -> None:
            self._handle = handle
            self._overlapped = overlapped

        def cancel(self) -> None:
            _cancel_hold(self._handle, ctypes.byref(self._overlapped))

    def _overlapped_io(
        function: Any, handle: int, buffer: Any, size: int, *, on_pending: Any = None,
    ) -> tuple[int, int]:
        """Run one overlapped ReadFile/WriteFile; return (bytes transferred, error).

        The error is 0 or ERROR_MORE_DATA; anything else raises.  ``buffer`` and the
        OVERLAPPED stay referenced here until the operation has completed.
        """

        overlapped = _OVERLAPPED()
        overlapped.hEvent = _thread_event()
        transferred = wintypes.DWORD(0)
        if function(handle, buffer, size, None, ctypes.byref(overlapped)):
            if not _result_hold(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
                error = ctypes.get_last_error()
                if error != _ERROR_MORE_DATA:
                    raise ctypes.WinError(error)
                return transferred.value, error
            return transferred.value, 0
        error = ctypes.get_last_error()
        if error == _ERROR_IO_PENDING:
            try:
                if on_pending is not None:
                    on_pending(_PendingSend(handle, overlapped))
                completed = _wait_complete(handle, overlapped, transferred)
            except BaseException:
                # Never unwind (and free the OVERLAPPED/buffer) while Windows still
                # owns them: cancel, wait for the kernel to let go, then re-raise.
                _cancel_hold(handle, ctypes.byref(overlapped))
                _wait_complete(handle, overlapped, transferred)
                raise
            finally:
                if on_pending is not None:
                    on_pending(None)
            if completed:
                return transferred.value, 0
            error = ctypes.get_last_error()
        elif error == _ERROR_MORE_DATA:
            # Completed at once with a partial message: the count is in the OVERLAPPED.
            _result_hold(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False)
            return transferred.value, error
        if error == _ERROR_MORE_DATA:
            return transferred.value, error
        if error in _PIPE_GONE:
            raise BrokenPipeError(errno.EPIPE, "validator Pipe is closed", None, error)
        if error == _ERROR_OPERATION_ABORTED:
            raise OSError(errno.EPIPE, "handle is closed")
        raise ctypes.WinError(error)

    def _peek(handle: int) -> tuple[int, int]:
        """(bytes queued, bytes left in the next message); raises when the Pipe is gone."""

        available = wintypes.DWORD(0)
        left = wintypes.DWORD(0)
        if not _peek_hold(handle, None, 0, None, ctypes.byref(available), ctypes.byref(left)):
            error = ctypes.get_last_error()
            if error in _PIPE_GONE:
                raise BrokenPipeError(errno.EPIPE, "validator Pipe is closed", None, error)
            raise ctypes.WinError(error)
        return available.value, left.value


def _fast(connection: Any) -> bool:
    return _ENABLED and isinstance(connection, multiprocessing.connection.PipeConnection)


def has_queued_message(connection: Any) -> bool:
    """``_pipe_has_queued_message`` without releasing the GIL on Windows Pipes."""

    if not _fast(connection):
        return _pipe_has_queued_message(connection)
    if connection.closed or not connection.readable:
        raise OSError("validator Pipe is not readable")
    if connection._got_empty_message:
        return True
    try:
        available, _left = _peek(connection.fileno())
    except OSError:
        # Let the standard path produce its usual exception for this state.
        return _pipe_has_queued_message(connection)
    return available > 0


def receive_queued_message(connection: Any) -> bytes:
    """``receive_ready_pipe_message`` that keeps the GIL when the frame is already queued.

    The caller must have released its outstanding readiness read first (exactly as for
    ``receive_ready_pipe_message``).  Message-mode reads never join adjacent frames.
    """

    if not _fast(connection):
        return receive_ready_pipe_message(connection)
    connection._check_closed()
    connection._check_readable()
    if connection._got_empty_message:
        connection._got_empty_message = False
        return b""
    handle = connection.fileno()
    try:
        available, left = _peek(handle)
    except BrokenPipeError as exc:
        raise EOFError from exc
    if available <= 0:
        # Nothing queued (or only an empty message): the standard path waits for it.
        return receive_ready_pipe_message(connection)
    chunks: list[bytes] = []
    size = left or available
    while True:
        buffer = ctypes.create_string_buffer(max(1, size))
        try:
            transferred, error = _overlapped_io(_read_hold, handle, buffer, max(1, size))
        except BrokenPipeError as exc:
            raise EOFError from exc
        chunks.append(buffer.raw[:transferred])
        if error == 0:
            return b"".join(chunks)
        # ERROR_MORE_DATA: the rest of this same message is still queued.
        try:
            available, left = _peek(handle)
        except BrokenPipeError as exc:
            raise EOFError from exc
        size = left or 65536


if _ENABLED:

    class HoldingPipeConnection(multiprocessing.connection.PipeConnection):
        """A ``PipeConnection`` whose sends keep the GIL unless the write really pends."""

        def _send_bytes(self, buf: Any) -> None:
            if self._send_ov is not None:
                # A connection should only be used by a single thread.
                raise ValueError("concurrent send_bytes() calls are not supported")
            data = bytes(buf)
            size = len(data)

            def on_pending(pending: Any) -> None:
                self._send_ov = pending

            transferred, error = _overlapped_io(
                _write_hold, self._handle, data, size, on_pending=on_pending,
            )
            if error != 0 or transferred != size:
                raise OSError(errno.EIO, f"validator Pipe short write: {transferred}/{size}")

else:  # pragma: no cover - non-Windows has no overlapped Pipes
    HoldingPipeConnection = None  # type: ignore[assignment,misc]


def hold_gil_for_sends(connection: Any) -> Any:
    """Switch one parent-side Pipe end to GIL-holding sends (in place); return it."""

    if _fast(connection) and HoldingPipeConnection is not None and type(connection) is (
        multiprocessing.connection.PipeConnection
    ):
        connection.__class__ = HoldingPipeConnection
    return connection


__all__ = [
    "HoldingPipeConnection",
    "has_queued_message",
    "hold_gil_for_sends",
    "receive_queued_message",
]
