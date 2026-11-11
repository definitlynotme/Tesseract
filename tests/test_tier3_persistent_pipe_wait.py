from __future__ import annotations

from contextlib import contextmanager
import multiprocessing
import multiprocessing.connection
import os
import threading

import pytest

from tier3_pipe_wait import Tier3PipeWaitSet


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows overlapped Pipe ownership")


@contextmanager
def pipes(count=1):
    pairs = [multiprocessing.Pipe() for _ in range(count)]
    owner = Tier3PipeWaitSet()
    try:
        yield owner, pairs
    finally:
        owner.close()
        for reader, writer in pairs:
            reader.close()
            writer.close()


def test_idle_ticks_keep_one_pending_read_per_pipe(monkeypatch):
    import tier3_pipe_wait as module
    read = module._winapi.ReadFile
    calls = []

    def recorded(handle, size, overlapped):
        calls.append((handle, size, overlapped))
        return read(handle, size, overlapped)

    with pipes(16) as (owner, pairs):
        readers = [pair[0] for pair in pairs]
        monkeypatch.setattr(module._winapi, "ReadFile", recorded)
        for _ in range(32):
            assert owner.wait(readers, 0) == []
        assert calls == [(reader.fileno(), 0, True) for reader in readers]
        assert len(owner._pending) == 16
        owner.close()
        assert owner._pending == {}
        pairs[0][1].send_bytes(b"after-close")
        assert readers[0].recv_bytes() == b"after-close"


@pytest.mark.parametrize("armed", [False, True])
def test_empty_messages_and_order_match_stdlib(armed):
    with pipes(4) as (owner, pairs):
        readers = [pair[0] for pair in pairs]
        if armed:
            assert owner.wait(readers, 0) == []
        for _, writer in pairs:
            for payload in (b"", b"phase", b"result", b"", b"next"):
                writer.send_bytes(payload)
        for payload in (b"", b"phase", b"result", b"", b"next"):
            assert owner.wait(readers, 1) == readers
            assert not any(reader in owner._pending for reader in readers)
            for reader in readers:
                assert reader.recv_bytes() == payload
        assert owner.wait(readers, 0) == []


def test_delayed_message_wakes_same_pending_operation():
    with pipes() as (owner, pairs):
        reader, writer = pairs[0]
        assert owner.wait([reader], 0) == []
        pending = owner._pending[reader]
        thread = threading.Thread(target=lambda: writer.send_bytes(b"delivered"))
        thread.start()
        try:
            assert owner.wait([reader], 1) == [reader]
            assert reader not in owner._pending
            assert reader.recv_bytes() == b"delivered"
            assert pending.GetOverlappedResult(False)[1] != 997
        finally:
            thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("payload", [b"", b"result"])
def test_release_preserves_arrival_racing_deadline_or_close(payload):
    with pipes() as (owner, pairs):
        reader, writer = pairs[0]
        assert owner.wait([reader], 0) == []
        writer.send_bytes(payload)
        assert owner.release(reader)
        assert not owner.release(reader)
        assert reader.recv_bytes() == payload
        assert owner.wait([reader], 0) == []
        assert not owner.release(reader)
        assert owner._pending == {}


def test_removed_generation_is_released_before_handle_close():
    with pipes(2) as (owner, pairs):
        old, new = pairs[0][0], pairs[1][0]
        assert owner.wait([old], 0) == []
        assert owner.wait([new], 0) == []
        assert old not in owner._pending
        old.close()
        pairs[1][1].send_bytes(b"current-generation")
        assert owner.wait([new], 1) == [new]
        assert new.recv_bytes() == b"current-generation"


@pytest.mark.parametrize("armed", [False, True])
def test_broken_peer_is_ready_for_ordinary_eof(armed):
    with pipes() as (owner, pairs):
        reader, writer = pairs[0]
        if armed:
            assert owner.wait([reader], 0) == []
        writer.close()
        assert owner.wait([reader], 1) == [reader]
        assert owner._pending == {}
        with pytest.raises((EOFError, OSError)):
            reader.recv_bytes()


def test_ready_pipe_does_not_hide_signalled_process_handle():
    process = multiprocessing.get_context("spawn").Process()
    process.start()
    try:
        process.join(5)
        assert not process.is_alive()
        event = process.sentinel
        with pipes(2) as (owner, pairs):
            first, second = pairs[0][0], pairs[1][0]
            assert owner.wait([first, event, second], 0) == [event]
            pairs[0][1].send_bytes(b"first")
            pairs[1][1].send_bytes(b"")
            assert owner.wait([first, event, second], 1) == [first, event, second]
            assert first.recv_bytes() == b"first"
            assert second.recv_bytes() == b""
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()


def test_one_thousand_cancel_delivery_and_empty_message_cycles():
    with pipes(16) as (owner, pairs):
        readers = [pair[0] for pair in pairs]
        for index in range(1000):
            assert owner.wait(readers, 0) == []
            reader, writer = pairs[index % 16]
            payload = b"" if index % 3 == 0 else str(index).encode("ascii")
            if index % 4 == 0:
                assert not owner.release(reader)
            writer.send_bytes(payload)
            if index % 4 == 1:
                assert owner.release(reader)
            else:
                assert owner.wait(readers, 1) == [reader]
            assert reader.recv_bytes() == payload
        owner.close()
        assert not owner._pending


def test_wait_exception_closes_outstanding_operations(monkeypatch):
    with pipes(2) as (owner, pairs):
        readers = [pair[0] for pair in pairs]
        assert owner.wait(readers, 0) == []

        def broken(*args, **kwargs):
            raise RuntimeError("native wait failed")

        monkeypatch.setattr(multiprocessing.connection, "wait", broken)
        with pytest.raises(RuntimeError, match="native wait failed"):
            owner.wait(readers, 0)
        assert owner._pending == {}
        pairs[0][1].send_bytes(b"still-readable")
        assert readers[0].recv_bytes() == b"still-readable"
