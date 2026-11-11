from __future__ import annotations

import multiprocessing
import os
import threading

import pytest

from tier3_pipe_wait import Tier3PipeWaitSet, receive_ready_pipe_message


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows message-mode Pipe")


@pytest.mark.parametrize("size", [0, 1, 127, 128, 129, 4096, 65536, 65537, 1048576])
@pytest.mark.parametrize("armed", [False, True])
def test_ready_receive_preserves_frame_boundaries(size, armed):
    reader, writer = multiprocessing.Pipe()
    owner = Tier3PipeWaitSet()
    frames = [b"x" * size, b"", b"next"]
    errors = []

    def send():
        try:
            for frame in frames:
                writer.send_bytes(frame)
        except BaseException as exc:
            errors.append(exc)

    sender = threading.Thread(target=send)
    try:
        if armed:
            assert owner.wait([reader], 0) == []
        sender.start()
        for frame in frames:
            assert owner.wait([reader], 3) == [reader]
            owner.release(reader)
            assert receive_ready_pipe_message(reader) == frame
        sender.join(3)
        assert not sender.is_alive()
        assert not errors
    finally:
        owner.close()
        reader.close()
        writer.close()
        sender.join(3)


def test_typical_endpoint_result_needs_only_one_native_read(monkeypatch):
    import tier3_pipe_wait as module

    reader, writer = multiprocessing.Pipe()
    calls = []
    original = module._winapi.ReadFile

    def read(handle, size, *args, **kwargs):
        calls.append(size)
        return original(handle, size, *args, **kwargs)

    try:
        payload = b"x" * 4096
        writer.send_bytes(payload)
        monkeypatch.setattr(module._winapi, "ReadFile", read)
        assert receive_ready_pipe_message(reader) == payload
        assert calls == [65536]
    finally:
        reader.close()
        writer.close()


def test_receive_cancel_joins_native_operation_before_return(monkeypatch):
    import tier3_pipe_wait as module

    reader, writer = multiprocessing.Pipe()
    operations = []
    original = module._winapi.ReadFile

    def read(*args, **kwargs):
        operation, error = original(*args, **kwargs)
        operations.append(operation)
        return operation, error

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    try:
        monkeypatch.setattr(module._winapi, "ReadFile", read)
        monkeypatch.setattr(module._winapi, "WaitForMultipleObjects", interrupted)
        with pytest.raises(KeyboardInterrupt):
            receive_ready_pipe_message(reader)
        assert len(operations) == 1
        assert operations[0].GetOverlappedResult(False)[1] == module._winapi.ERROR_OPERATION_ABORTED
        writer.send_bytes(b"after-cancel")
        assert receive_ready_pipe_message(reader) == b"after-cancel"
    finally:
        reader.close()
        writer.close()


def test_broken_pipe_and_closed_handle_keep_connection_errors():
    reader, writer = multiprocessing.Pipe()
    writer.close()
    try:
        with pytest.raises(EOFError):
            receive_ready_pipe_message(reader)
    finally:
        reader.close()
    with pytest.raises(OSError, match="closed"):
        receive_ready_pipe_message(reader)


def test_one_thousand_frames_after_readiness_cancel_arrival_race():
    reader, writer = multiprocessing.Pipe()
    owner = Tier3PipeWaitSet()
    try:
        for index in range(1000):
            assert owner.wait([reader], 0) == []
            payload = b"" if index % 3 == 0 else str(index).encode() * 100
            writer.send_bytes(payload)
            assert owner.release(reader)
            assert receive_ready_pipe_message(reader) == payload
    finally:
        owner.close()
        reader.close()
        writer.close()
