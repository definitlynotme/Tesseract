"""GIL-holding Pipe I/O for the batch validator owner (tier3_batch_pipe_io).

The owner calls these on the parent end of each helper group's duplex Pipe.  They must
behave exactly like the standard library calls they replace (message boundaries, empty
message bookkeeping, EOF/broken-pipe errors, cancellation on close) while keeping the
GIL on calls that cannot block.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import multiprocessing.connection
import os
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Pipe semantics")

import tier3_batch_pipe_io  # noqa: E402
from tier3_batch_pipe_io import (  # noqa: E402
    HoldingPipeConnection,
    has_queued_message,
    hold_gil_for_sends,
    receive_queued_message,
)
from tier3_pipe_wait import Tier3PipeWaitSet  # noqa: E402
from tier3_validator_supervisor import _pipe_has_queued_message  # noqa: E402

SIZES = (1, 7, 200, 4096, 8191, 8192, 8193, 20_000, 1 << 20)


def _frames() -> list[bytes]:
    return [bytes([65 + index % 26]) * size for index, size in enumerate(SIZES)]


def _in_thread(target, *args: Any) -> tuple[threading.Thread, list[Any]]:
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(target(*args))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _pair() -> tuple[Any, Any]:
    parent, child = multiprocessing.Pipe()
    hold_gil_for_sends(parent)
    return parent, child


def test_rebinding_keeps_a_pipe_connection_and_is_idempotent() -> None:
    parent, child = _pair()
    try:
        assert type(parent) is HoldingPipeConnection
        assert isinstance(parent, multiprocessing.connection.PipeConnection)
        assert hold_gil_for_sends(parent) is parent
        assert type(parent) is HoldingPipeConnection
        assert type(child) is multiprocessing.connection.PipeConnection
    finally:
        parent.close()
        child.close()


def test_frames_from_the_helper_keep_their_boundaries() -> None:
    parent, child = _pair()
    frames = _frames()
    writer, outcome = _in_thread(lambda: [child.send_bytes(frame) for frame in frames])
    try:
        received = []
        deadline = time.monotonic() + 10.0
        while len(received) < len(frames) and time.monotonic() < deadline:
            if has_queued_message(parent):
                received.append(receive_queued_message(parent))
            else:
                time.sleep(0.001)
        writer.join(10.0)
        assert not writer.is_alive() and not isinstance(outcome[0], BaseException), outcome
        assert [len(item) for item in received] == [len(item) for item in frames]
        assert received == frames
        assert not has_queued_message(parent)
    finally:
        parent.close()
        child.close()


def test_a_partial_first_read_still_returns_the_whole_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ERROR_MORE_DATA loop reassembles a frame the first ReadFile only partly read.

    PeekNamedPipe reports the whole message length (even for 1 MiB), so in practice the
    first read always fits; under-report it to force every frame through the loop.
    """

    cap = 100
    real_peek = tier3_batch_pipe_io._peek
    real_io = tier3_batch_pipe_io._overlapped_io
    partial_reads: list[int] = []

    def under_reporting_peek(handle: int) -> tuple[int, int]:
        available, left = real_peek(handle)
        return available, min(left, cap)

    def counting_io(*args: Any, **kwargs: Any) -> tuple[int, int]:
        transferred, error = real_io(*args, **kwargs)
        if error:
            partial_reads.append(transferred)
        return transferred, error

    monkeypatch.setattr(tier3_batch_pipe_io, "_peek", under_reporting_peek)
    monkeypatch.setattr(tier3_batch_pipe_io, "_overlapped_io", counting_io)
    parent, child = _pair()
    frames = _frames()
    writer, outcome = _in_thread(lambda: [child.send_bytes(frame) for frame in frames])
    try:
        received = []
        deadline = time.monotonic() + 10.0
        while len(received) < len(frames) and time.monotonic() < deadline:
            if has_queued_message(parent):
                received.append(receive_queued_message(parent))
            else:
                time.sleep(0.001)
        writer.join(10.0)
        assert not writer.is_alive() and not isinstance(outcome[0], BaseException), outcome
        assert [len(item) for item in received] == [len(item) for item in frames]
        assert received == frames
        assert not has_queued_message(parent)
    finally:
        parent.close()
        child.close()
    # Every frame longer than the cap went through the loop at least once.
    assert len(partial_reads) >= sum(1 for size in SIZES if size > cap)


def test_only_a_real_wait_releases_the_gil() -> None:
    """Deterministic check of the bindings the timing test below cannot fully cover.

    Rebinding ReadFile, WriteFile or the non-waiting GetOverlappedResult through
    ``ctypes.WinDLL`` (which releases the GIL) costs too little per call for a timing
    threshold to notice reliably, yet reintroduces the handoff this module removes.
    """

    holds_gil = ctypes._FUNCFLAG_PYTHONAPI
    reads_last_error = ctypes._FUNCFLAG_USE_LASTERROR
    for name in (
        "_peek_hold",
        "_read_hold",
        "_write_hold",
        "_result_hold",
        "_cancel_hold",
        "_create_event_hold",
    ):
        flags = getattr(tier3_batch_pipe_io, name)._flags_
        assert flags & holds_gil, f"{name} must keep the GIL"
        assert flags & reads_last_error, f"{name} must record the last error"
    # A pending operation must be waited on with the GIL released, or the process stalls.
    flags = tier3_batch_pipe_io._result_wait._flags_
    assert not flags & holds_gil, "_result_wait must release the GIL"
    assert flags & reads_last_error, "_result_wait must record the last error"


def test_sends_to_the_helper_keep_their_boundaries_and_wait_when_the_pipe_is_full() -> None:
    parent, child = _pair()
    frames = _frames()
    reader, outcome = _in_thread(lambda: [child.recv_bytes() for _ in frames])
    try:
        for frame in frames:
            # 1 MiB cannot fit the Pipe buffer: that send pends until the reader drains.
            parent.send_bytes(frame)
        reader.join(10.0)
        assert not reader.is_alive()
        assert outcome[0] == frames
    finally:
        parent.close()
        child.close()


def test_interoperates_with_the_owner_wait_set() -> None:
    parent, child = _pair()
    waits = Tier3PipeWaitSet()
    try:
        # Arms the pending zero-byte readiness read.
        assert waits.wait([parent], 0.05) == []
        child.send_bytes(b"first")
        child.send_bytes(b"second")
        assert waits.wait([parent], 5.0) == [parent]
        waits.release(parent)
        assert receive_queued_message(parent) == b"first"
        assert has_queued_message(parent)
        assert receive_queued_message(parent) == b"second"
        assert not has_queued_message(parent)
        assert waits.wait([parent], 0.05) == []
        child.send_bytes(b"third")
        assert waits.wait([parent], 5.0) == [parent]
        waits.release(parent)
        assert receive_queued_message(parent) == b"third"
    finally:
        waits.close()
        parent.close()
        child.close()


def test_an_empty_message_consumed_by_the_wait_set_is_reported() -> None:
    parent, child = _pair()
    waits = Tier3PipeWaitSet()
    try:
        assert waits.wait([parent], 0.05) == []
        child.send_bytes(b"")
        assert waits.wait([parent], 5.0) == [parent]
        waits.release(parent)
        assert has_queued_message(parent)
        assert receive_queued_message(parent) == b""
        assert not has_queued_message(parent)
    finally:
        waits.close()
        parent.close()
        child.close()


def test_a_closed_helper_end_raises_like_the_standard_path() -> None:
    parent, child = _pair()
    child.close()
    try:
        with pytest.raises(EOFError):
            receive_queued_message(parent)
        with pytest.raises(OSError):
            has_queued_message(parent)
        with pytest.raises(OSError):
            _pipe_has_queued_message(parent)
        with pytest.raises(OSError):
            parent.send_bytes(b"after-close")
    finally:
        parent.close()


def test_closing_during_a_pending_send_cancels_it() -> None:
    parent, child = _pair()
    # No reader: 1 MiB cannot fit the Pipe buffer, so this send really pends.
    sender, outcome = _in_thread(parent.send_bytes, b"x" * (1 << 20))
    try:
        deadline = time.monotonic() + 5.0
        while parent._send_ov is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert parent._send_ov is not None, "the send never became pending"
        parent.close()
        sender.join(5.0)
        assert not sender.is_alive()
        assert isinstance(outcome[0], OSError), outcome
    finally:
        child.close()


def test_a_second_concurrent_send_is_refused() -> None:
    parent, child = _pair()
    sender, _outcome = _in_thread(parent.send_bytes, b"x" * (1 << 20))
    try:
        deadline = time.monotonic() + 5.0
        while parent._send_ov is None and time.monotonic() < deadline:
            time.sleep(0.005)
        with pytest.raises(ValueError):
            parent.send_bytes(b"second")
    finally:
        parent.close()
        sender.join(5.0)
        child.close()


def test_calls_that_cannot_block_do_not_queue_behind_a_busy_thread() -> None:
    """The point of the module: no GIL handoff per peek/read/send when nothing waits.

    With a CPU-bound thread competing, a call that releases the GIL waits for a full
    handoff to get it back (a timer tick on Windows); one that keeps it does not.
    """

    parent, child = _pair()
    stop = threading.Event()

    def burn() -> None:
        while not stop.is_set():
            sum(range(2000))

    burner = threading.Thread(target=burn, daemon=True)
    burner.start()
    held = 0.0
    try:
        for index in range(200):
            # The helper side uses the standard (GIL-releasing) calls: not timed.
            child.send_bytes(b"frame-%d" % index)
            started = time.perf_counter()
            assert has_queued_message(parent)
            assert receive_queued_message(parent) == b"frame-%d" % index
            parent.send_bytes(b"ack")
            held += time.perf_counter() - started
            assert child.recv_bytes() == b"ack"
    finally:
        stop.set()
        burner.join(5.0)
        parent.close()
        child.close()
    # 200 rounds x (peek + read + send) with a GIL release each would take seconds.
    assert held < 1.0, held


def _process_handle_count() -> int:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD(0)
    assert k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count))
    return count.value


def test_threads_that_did_pipe_io_do_not_leak_their_event() -> None:
    parent, child = _pair()
    try:
        def one_round(index: int) -> None:
            child.send_bytes(b"in-%d" % index)
            assert has_queued_message(parent)
            assert receive_queued_message(parent) == b"in-%d" % index
            parent.send_bytes(b"out-%d" % index)

        # Warm up everything that allocates handles once per process.
        warm = threading.Thread(target=one_round, args=(-1,))
        warm.start()
        warm.join(5.0)
        assert child.recv_bytes() == b"out--1"
        before = _process_handle_count()
        for index in range(100):
            thread = threading.Thread(target=one_round, args=(index,))
            thread.start()
            thread.join(5.0)
            assert child.recv_bytes() == b"out-%d" % index
        after = _process_handle_count()
    finally:
        parent.close()
        child.close()
    # One event per exited thread would leak ~100 handles.
    assert after - before < 20, (before, after)


def test_an_exception_while_a_send_is_pending_cancels_it_before_unwinding() -> None:
    import tier3_batch_pipe_io as module

    parent, child = _pair()
    try:
        def explode(pending) -> None:
            if pending is not None:
                raise RuntimeError("stand-in for an asynchronous exception")

        data = b"x" * (1 << 20)
        with pytest.raises(RuntimeError):
            # No reader: the 1 MiB write pends, then the exception must cancel it
            # and wait for the kernel before the OVERLAPPED/buffer can be freed.
            module._overlapped_io(module._write_hold, parent.fileno(), data, len(data), on_pending=explode)
        # The thread's event and the Pipe stay usable: a later pending send on the
        # same thread completes in full (a stray signal would end it early).
        reader, outcome = _in_thread(lambda: [child.recv_bytes() for _ in range(2)])
        parent.send_bytes(b"y" * (1 << 20))
        parent.send_bytes(b"after")
        reader.join(10.0)
        assert not reader.is_alive()
        received = outcome[0]
        assert received[-1] == b"after"
        assert received[-2] == b"y" * (1 << 20)
    finally:
        parent.close()
        child.close()


def test_frames_sent_just_before_the_helper_exits_are_read_before_eof() -> None:
    parent, child = _pair()
    try:
        child.send_bytes(b"last-words")
        child.send_bytes(b"x" * 6000)
        child.close()
        assert has_queued_message(parent)
        assert receive_queued_message(parent) == b"last-words"
        assert receive_queued_message(parent) == b"x" * 6000
        with pytest.raises(EOFError):
            receive_queued_message(parent)
    finally:
        parent.close()


def test_the_batch_owner_uses_the_gil_holding_pipe_io() -> None:
    import tier3_batch_pipe_io as module
    import tier3_batch_validator_supervisor as batch_module

    assert batch_module.has_queued_message is module.has_queued_message
    assert batch_module.receive_queued_message is module.receive_queued_message
    assert batch_module.hold_gil_for_sends is module.hold_gil_for_sends
