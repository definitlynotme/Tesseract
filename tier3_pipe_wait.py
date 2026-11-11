from __future__ import annotations

import multiprocessing.connection
import os
from typing import Any

if os.name == "nt":
    import _winapi


def receive_ready_pipe_message(connection: Any) -> bytes:
    """Read one owner-ready frame without splitting typical helper messages.

    Mirror PipeConnection's overlapped completion/cancellation ownership, but
    use a 64KiB first read instead of 128 bytes. Larger messages still use its
    standard remainder reader. Message-mode reads never join adjacent frames.
    The caller must release its outstanding readiness read first.
    """

    if os.name != "nt" or not isinstance(
        connection, multiprocessing.connection.PipeConnection
    ):
        return connection.recv_bytes()
    connection._check_closed()
    connection._check_readable()
    if connection._got_empty_message:
        connection._got_empty_message = False
        return b""
    try:
        operation, error = _winapi.ReadFile(connection.fileno(), 65536, True)
        try:
            if error == _winapi.ERROR_IO_PENDING:
                _winapi.WaitForMultipleObjects([operation.event], False, _winapi.INFINITE)
        except BaseException:
            operation.cancel()
            raise
        finally:
            _, error = operation.GetOverlappedResult(True)
        if error == 0:
            return bytes(operation.getbuffer())
        if error == _winapi.ERROR_MORE_DATA:
            return connection._get_more_data(operation, None).getvalue()
        raise OSError(error, "validator Pipe read did not complete")
    except OSError as exc:
        if exc.winerror == _winapi.ERROR_BROKEN_PIPE:
            raise EOFError from exc
        raise


class Tier3PipeWaitSet:
    """Single-reader readiness for the supervisor's owned overlapped Pipes.

    Keep zero-byte reads pending across idle ticks instead of cancelling every
    handle on each wait. Release before recv/close; actual frames stay owned by
    multiprocessing.Connection. No helper payload or validation authority lives
    here. Like Connection.wait, a consumed empty message is preserved explicitly.
    """

    def __init__(self) -> None:
        self._pending: dict[Any, Any] = {}

    @staticmethod
    def _finish(connection: Any, operation: Any, *, cancel: bool) -> bool:
        if cancel:
            operation.cancel()
        try:
            _, error = operation.GetOverlappedResult(True)
        except OSError as exc:
            if exc.winerror not in {
                _winapi.ERROR_BROKEN_PIPE, _winapi.ERROR_NETNAME_DELETED,
            }:
                raise
            error = exc.winerror
        if error == _winapi.ERROR_OPERATION_ABORTED:
            return False
        if error == 0:
            connection._got_empty_message = True
        return True

    def release(self, connection: Any) -> bool:
        operation = self._pending.pop(connection, None)
        return bool(operation is not None and self._finish(connection, operation, cancel=True))

    def close(self) -> None:
        pending, self._pending = self._pending, {}
        # Cancel all before joining any operation. Keep their Python owners alive
        # until Windows confirms completion, including cancel/arrival races.
        for operation in pending.values():
            operation.cancel()
        failure = None
        for connection, operation in pending.items():
            try:
                self._finish(connection, operation, cancel=False)
            except OSError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def wait(self, objects: list[Any], timeout: float) -> list[Any]:
        objects = list(objects)
        if os.name != "nt" or any(
            not isinstance(value, (int, multiprocessing.connection.PipeConnection))
            for value in objects
        ):
            self.close()
            return multiprocessing.connection.wait(objects, timeout)
        try:
            return self._wait_windows(objects, timeout)
        except BaseException:
            self.close()
            raise

    def _wait_windows(self, objects: list[Any], timeout: float) -> list[Any]:
        current = set(objects)
        for connection in tuple(self._pending):
            if connection not in current:
                self.release(connection)
        handles: dict[int, Any] = {}
        ready: set[Any] = set()
        for value in objects:
            if isinstance(value, int):
                handles[value] = value
                continue
            if value._got_empty_message:
                ready.add(value)
                continue
            operation = self._pending.get(value)
            if operation is None:
                try:
                    operation, error = _winapi.ReadFile(value.fileno(), 0, True)
                except OSError as exc:
                    if exc.winerror not in {
                        _winapi.ERROR_BROKEN_PIPE, _winapi.ERROR_NETNAME_DELETED,
                    }:
                        raise
                    ready.add(value)
                    continue
                if error != _winapi.ERROR_IO_PENDING:
                    self._finish(value, operation, cancel=False)
                    ready.add(value)
                    continue
                self._pending[value] = operation
            handles[operation.event] = value
        # Only integer events/sentinels enter the stdlib wait: it performs one
        # native event wait, without allocating another Pipe read for each item.
        signalled = multiprocessing.connection.wait(
            list(handles), 0.0 if ready else timeout,
        ) if handles else []
        for handle in signalled:
            value = handles[handle]
            if isinstance(value, int):
                ready.add(value)
            elif self._finish(value, self._pending.pop(value), cancel=False):
                ready.add(value)
        return [value for value in objects if value in ready]
