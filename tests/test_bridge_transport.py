"""Real overlapped named-pipe fault tests for the Python sidecar endpoint."""

import ctypes
import os
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes

import pytest

import bridge_transport as bt


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named pipe")


@pytest.fixture
def pipe():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p]
    api.CreateNamedPipeW.restype = wintypes.HANDLE
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(bt.OVERLAPPED)]
    api.ConnectNamedPipe.restype = wintypes.BOOL
    api.WriteFile.argtypes = bt._blocking.WriteFile.argtypes
    api.WriteFile.restype = wintypes.BOOL
    api.ReadFile.argtypes = bt._blocking.ReadFile.argtypes
    api.ReadFile.restype = wintypes.BOOL
    api.GetOverlappedResult.argtypes = bt._immediate.GetOverlappedResult.argtypes
    api.GetOverlappedResult.restype = wintypes.BOOL
    name = r"\\.\pipe\tesseract-test-" + uuid.uuid4().hex
    server = api.CreateNamedPipeW(name, 0x00000003 | 0x40000000,
                                  0x00000000 | 0x00000008, 1, 4096, 4096, 0, None)
    assert server != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    client = api.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
    assert client != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    op = bt.OVERLAPPED()
    event = bt._immediate.CreateEventW(None, True, False, None)
    op.hEvent = event
    connected = api.ConnectNamedPipe(server, ctypes.byref(op))
    assert connected or ctypes.get_last_error() == 535  # ERROR_PIPE_CONNECTED
    bt._immediate.CloseHandle(event)

    class Server:
        def __init__(self):
            self.handle = server
            self.client = client

        def write(self, data):
            buf = ctypes.create_string_buffer(data)
            count = wintypes.DWORD()
            operation = bt.OVERLAPPED()
            operation.hEvent = bt._event()
            try:
                ok = api.WriteFile(server, buf, len(data), ctypes.byref(count), ctypes.byref(operation))
                if not ok:
                    assert ctypes.get_last_error() == bt.ERROR_IO_PENDING
                    if bt._blocking.WaitForSingleObject(operation.hEvent, 5000) != 0:
                        bt._immediate.CancelIoEx(server, ctypes.byref(operation))
                        bt._blocking.WaitForSingleObject(operation.hEvent, 0xFFFFFFFF)
                        api.GetOverlappedResult(server, ctypes.byref(operation),
                                                ctypes.byref(count), False)
                        raise TimeoutError("server write did not complete")
                    assert api.GetOverlappedResult(server, ctypes.byref(operation),
                                                   ctypes.byref(count), False)
                assert count.value == len(data)
            finally:
                bt._immediate.CloseHandle(operation.hEvent)

        def read(self, count=65536):
            buf = ctypes.create_string_buffer(count)
            got = wintypes.DWORD()
            operation = bt.OVERLAPPED()
            operation.hEvent = bt._event()
            try:
                ok = api.ReadFile(server, buf, count, ctypes.byref(got), ctypes.byref(operation))
                if not ok:
                    assert ctypes.get_last_error() == bt.ERROR_IO_PENDING
                    if bt._blocking.WaitForSingleObject(operation.hEvent, 5000) != 0:
                        bt._immediate.CancelIoEx(server, ctypes.byref(operation))
                        bt._blocking.WaitForSingleObject(operation.hEvent, 0xFFFFFFFF)
                        api.GetOverlappedResult(server, ctypes.byref(operation),
                                                ctypes.byref(got), False)
                        raise TimeoutError("server read did not complete")
                    assert api.GetOverlappedResult(server, ctypes.byref(operation),
                                                   ctypes.byref(got), False)
                return buf.raw[:got.value]
            finally:
                bt._immediate.CloseHandle(operation.hEvent)

        def close(self):
            if self.handle:
                bt._immediate.CloseHandle(self.handle)
                self.handle = None

    endpoint = Server()
    try:
        yield endpoint
    finally:
        endpoint.close()
        if endpoint.client:
            bt._immediate.CloseHandle(endpoint.client)
            endpoint.client = None


def frame(n):
    return {"t": "ping", "epoch": "test", "produced_at_ms": n,
            "sent_at_ms": n, "n": n}


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pid_inheritance_and_round_trip(pipe):
    with pytest.raises(ValueError, match="pipe_server_pid_mismatch"):
        bt.PipeTransport(pipe.client, os.getpid() + 1)
    # The failed constructor owns and closes the passed handle.
    pipe.client = None


def test_normal_multiframe_utf8_and_bad_frames(pipe):
    assert bt._immediate.SetHandleInformation(pipe.client, bt.HANDLE_FLAG_INHERIT,
                                              bt.HANDLE_FLAG_INHERIT)
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    try:
        flags = wintypes.DWORD()
        assert bt._immediate.GetHandleInformation(transport.handle, ctypes.byref(flags))
        assert not flags.value & bt.HANDLE_FLAG_INHERIT
        first = frame(1)
        second = frame(2)
        unicode_frame = {"t": "error", "epoch": "test", "produced_at_ms": 2,
                         "sent_at_ms": 2, "error": {"code": "io",
                                                      "message_key": "errors.io",
                                                      "params": {}, "detail": "中文 Ω"}}
        pipe.write(bt.protocol.encode_frame(first) + bt.protocol.encode_frame(unicode_frame) + b"\xff\n" +
                   bt.protocol.encode_frame(second))
        assert transport.receive() == first
        assert transport.receive() == unicode_frame
        assert transport.receive() == second
        assert "invalid_utf8" in transport.diagnostics
        outbound = frame(3)
        assert transport.send(outbound) in ("complete", "pending")
        assert pipe.read() == bt.protocol.encode_frame(outbound)
        assert eventually(lambda: transport.poll_write() == "complete")
    finally:
        transport.close()


def test_oversize_is_discarded_then_resynchronizes(pipe):
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    try:
        # Small pipe buffer forces the reader to consume while this writes.
        pipe.write(b"x" * (bt.protocol.HARD_FRAME_LIMIT + 1) + b"\n" +
                   bt.protocol.encode_frame(frame(7)))
        assert transport.receive(timeout=5) == frame(7)
        assert list(transport.diagnostics).count("frame_too_large") == 1
    finally:
        transport.close()


def test_peer_close_notifies_reader_and_writer(pipe):
    observed = threading.Event()
    transport = bt.PipeTransport(pipe.client, os.getpid(), on_disconnect=observed.set)
    pipe.client = None
    try:
        pipe.close()
        assert observed.wait(5)
        assert transport.receive(timeout=0.1) is None
        with pytest.raises(BrokenPipeError):
            transport.send(frame(1))
    finally:
        transport.close()


def test_full_inbox_detects_server_close_without_consuming(pipe):
    observed = threading.Event()
    transport = bt.PipeTransport(pipe.client, os.getpid(), on_disconnect=observed.set)
    pipe.client = None
    try:
        pipe.write(b"".join(bt.protocol.encode_frame(frame(n)) for n in range(65)))
        assert eventually(lambda: transport._inbox.qsize() == 64)
        pipe.close()
        assert observed.wait(5), "full inbox hid server close"
        assert transport.disconnected
        transport.close()
        transport.close()
    finally:
        transport.close()


def test_stalled_shell_does_not_hold_gil_or_replay_pending_frame():
    # An in-process timeout cannot run if a regression holds the GIL forever.
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_bridge_transport.py::test_stalled_shell_worker"],
        env={**os.environ, "BRIDGE_GIL_WORKER": "1"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stalled_shell_worker(pipe):
    if os.environ.get("BRIDGE_GIL_WORKER") != "1":
        pytest.skip("run by the parent test with a process deadline")
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    running = threading.Event()
    running.set()
    count = [0]

    def counter():
        while running.is_set():
            count[0] += 1

    worker = threading.Thread(target=counter)
    worker.start()
    try:
        # The first large NDJSON frame fills the small pipe buffer.
        # A valid large error detail exercises a pending overlapped write.
        payload = {"t": "error", "epoch": "test", "produced_at_ms": 1,
                   "sent_at_ms": 1, "error": {"code": "io", "message_key": "errors.io",
                                              "params": {}, "detail": "x" * 240000}}
        encoded = bt.protocol.encode_frame(payload)
        result = []
        sender_done = threading.Event()
        sender_exit = threading.Event()

        def submit():
            result.append(transport.send(payload))
            sender_done.set()
            sender_exit.wait(5)

        sender = threading.Thread(target=submit)
        sender.start()
        assert sender_done.wait(2), "WriteFile held the sender while shell stopped reading"
        assert result == ["pending"]
        assert transport.poll_write() == "pending"
        assert transport.send(frame(2)) == "busy"
        before = count[0]
        assert eventually(lambda: count[0] > before + 1000)
        received = bytearray()
        while len(received) < len(encoded):
            received.extend(pipe.read())
        assert bytes(received) == encoded
        assert eventually(lambda: transport.poll_write() == "complete")
        assert transport.send(frame(2)) in ("complete", "pending")
        assert pipe.read() == bt.protocol.encode_frame(frame(2))
    finally:
        sender_exit.set()
        sender.join(timeout=5)
        running.clear()
        worker.join(timeout=5)
        transport.close()


def test_cancel_pending_and_idempotent_close(pipe):
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    payload = {"t": "error", "epoch": "test", "produced_at_ms": 1,
               "sent_at_ms": 1, "error": {"code": "io", "message_key": "errors.io",
                                          "params": {}, "detail": "x" * 240000}}
    assert transport.send(payload) == "pending"
    transport.close()
    transport.close()
    assert transport._write_buffer is None


def test_disconnect_callback_can_close(pipe):
    completed = threading.Event()
    holder = []

    def shutdown():
        holder[0].close()
        completed.set()

    transport = bt.PipeTransport(pipe.client, os.getpid(), on_disconnect=shutdown)
    holder.append(transport)
    pipe.client = None
    pipe.close()
    assert completed.wait(5)


def test_partial_write_is_fatal(pipe, monkeypatch):
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None

    def short_write(_handle, _buf, length, count, _op):
        ctypes.cast(count, ctypes.POINTER(wintypes.DWORD)).contents.value = length - 1
        return 1

    try:
        monkeypatch.setattr(bt._blocking, "WriteFile", short_write)
        with pytest.raises(OSError, match="partial_pipe_write"):
            transport.send(frame(1))
        assert transport.disconnected
    finally:
        transport.close()


def test_pending_partial_write_is_fatal_and_not_replayed(pipe, monkeypatch):
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    submissions = []

    def pending_write(_handle, _buf, length, _count, _op):
        submissions.append(length)
        ctypes.set_last_error(bt.ERROR_IO_PENDING)
        return 0

    def short_completion(_handle, _op, count, _wait):
        ctypes.cast(count, ctypes.POINTER(wintypes.DWORD)).contents.value = submissions[0] - 1
        return 1

    try:
        monkeypatch.setattr(bt._blocking, "WriteFile", pending_write)
        monkeypatch.setattr(bt._immediate, "GetOverlappedResult", short_completion)
        assert transport.send(frame(1)) == "pending"
        with pytest.raises(OSError, match="partial_pipe_write"):
            transport.poll_write()
        assert transport.disconnected
        assert transport._write_buffer is None
        assert not transport._write_pending
        assert len(submissions) == 1
        with pytest.raises(BrokenPipeError):
            transport.send(frame(1))
        assert len(submissions) == 1
    finally:
        transport.close()


def test_receive_timeout_uses_whole_deadline(pipe):
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    try:
        started = time.monotonic()
        assert transport.receive(timeout=0.25) is None
        assert time.monotonic() - started >= 0.2
    finally:
        transport.close()


def test_pending_write_reaps_peer_close_error(pipe):
    transport = bt.PipeTransport(pipe.client, os.getpid())
    pipe.client = None
    payload = {"t": "error", "epoch": "test", "produced_at_ms": 1,
               "sent_at_ms": 1, "error": {"code": "io", "message_key": "errors.io",
                                          "params": {}, "detail": "x" * 240000}}
    try:
        assert transport.send(payload) == "pending"
        pipe.close()
        assert bt._blocking.WaitForSingleObject(transport._write_event, 5000) == 0
        # The reader may have marked the whole transport disconnected already;
        # inspect the submitted operation directly to prove write-side detection.
        with transport._write_lock:
            with pytest.raises(OSError) as failure:
                transport._finish_write()
        assert failure.value.errno in bt.PIPE_ERRORS
        assert transport._write_buffer is None
        assert transport.disconnected
    finally:
        transport.close()
