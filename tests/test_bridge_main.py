"""Narrow sidecar bootstrap tests; all transports and ownership are in memory."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge_cli
import bridge_instance_scope
import bridge_main
import bridge_protocol


class MemoryTransport:
    def __init__(self, reply=None):
        self.sent = []
        self.reply = reply
        self.closed = False

    def send(self, frame):
        self.sent.append(frame)

    def receive(self):
        if callable(self.reply):
            reply, self.reply = self.reply(self.sent[-1]), None
            return reply
        reply, self.reply = self.reply, None
        return reply

    def close(self):
        self.closed = True


def _scope(tmp_path):
    return bridge_instance_scope.InstanceScope(
        "custom", "a" * 24, bridge_instance_scope.names_for_scope("custom", "a" * 24),
        False, tmp_path, tmp_path,
    )


def _steps(tmp_path, calls, transport, *, acquired=True):
    scope = _scope(tmp_path)
    lease = SimpleNamespace(close=lambda: calls.append("release"))

    def step(name, value):
        def run(*_args):
            calls.append(name)
            return value
        return run

    return bridge_main.Steps(
        parse=step("parse", SimpleNamespace(lang="en_US")),
        scope=step("scope", scope),
        acquire=step("acquire", SimpleNamespace(
            status="acquired" if acquired else "already_running",
            reason_code="runtime_owned", win32_error=None,
            lease=lease if acquired else None)),
        logging=step("logging", None), audit=step("audit", None),
        recovery=step("recovery", bridge_main.unknown_recovery()),
        transport=step("transport", transport),
        loop=step("loop", None),
        helper=step("helper", 0),
    )


@pytest.mark.parametrize("argv", [
    ["--multiprocessing-fork", "parent_pid=1"],
    ["--validator-helper-smoke"],
    ["--validator-helper-smoke=2", "--validator-helper-slots=4"],
    ["--validator-helper-slots", "2"],
])
def test_helper_diverts_before_all_startup(tmp_path, argv):
    calls = []
    steps = _steps(tmp_path, calls, MemoryTransport())
    assert bridge_main.main(argv, steps=steps) == 0
    assert calls == ["helper"]


def test_smoke_dispatches_existing_single_and_batch_paths(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "app_paths", SimpleNamespace(
        build_app_paths=lambda: SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setitem(sys.modules, "desktop_app_shell", SimpleNamespace(
        run_validator_helper_smoke=lambda paths, worker_count, slots_per_process:
        calls.append((paths.data_dir, worker_count, slots_per_process)) or 0))
    assert bridge_main.dispatch_helper(["--validator-helper-smoke"]) == 0
    assert bridge_main.dispatch_helper(["--validator-helper-smoke=2",
                                        "--validator-helper-slots=4"]) == 0
    assert calls == [(tmp_path, 1, 1), (tmp_path, 2, 4)]


def test_bootstrap_order_and_hello(tmp_path):
    calls = []
    transport = MemoryTransport(lambda hello: {
        "t": "hello_ack", "epoch": hello["epoch"], "proto": 1, "accepted": True,
        "produced_at_ms": 1, "sent_at_ms": 1,
    })
    steps = _steps(tmp_path, calls, transport)
    assert bridge_main.main([], steps=steps) == 0
    assert calls == ["parse", "scope", "acquire", "logging", "audit", "recovery",
                     "transport", "loop", "release"]
    hello = transport.sent[0]
    assert bridge_protocol.validate_frame(hello) == []
    assert hello["caps"] == {"commands": [], "topics": [], "features": []}
    assert hello["network_recovery"] == bridge_main.unknown_recovery()
    assert transport.closed


def test_single_instance_rejection(tmp_path, capsys):
    calls = []
    transport = MemoryTransport()
    assert bridge_main.main([], steps=_steps(tmp_path, calls, transport, acquired=False)) != 0
    assert calls == ["parse", "scope", "acquire"]
    assert transport.sent == []
    assert '"code":"single_instance_rejected"' in capsys.readouterr().err


def test_wrong_ack_version_rejected(tmp_path, capsys):
    calls = []
    transport = MemoryTransport(lambda hello: {
        "t": "hello_ack", "epoch": hello["epoch"], "proto": 2, "accepted": True,
        "produced_at_ms": 1, "sent_at_ms": 1,
    })
    assert bridge_main.main([], steps=_steps(tmp_path, calls, transport)) != 0
    assert "loop" not in calls
    assert transport.closed
    assert '"code":"hello_ack_rejected"' in capsys.readouterr().err


def test_default_scope_logging_uses_directory_overrides(tmp_path, monkeypatch):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    data.mkdir()
    monkeypatch.setenv("TESSERACT_CONFIG_DIR", str(config))
    monkeypatch.setenv("TESSERACT_DATA_DIR", str(data))
    monkeypatch.delenv("TESSERACT_INSTANCE_SCOPE", raising=False)
    monkeypatch.delenv("TESSERACT_ISOLATED_AUDIT_MANIFEST", raising=False)
    scope = bridge_main.resolve_effective_scope()
    assert scope.kind == "default"
    assert scope.config_dir == config
    assert scope.data_dir == data
    logger = bridge_main.init_logging(scope)
    assert (data / "bridge.log").is_file()
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def test_incomplete_directory_override_fails_before_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("TESSERACT_DATA_DIR", raising=False)
    with pytest.raises(bridge_main.StartupError, match="directory_override_pair_required"):
        bridge_main.resolve_effective_scope()


@pytest.mark.parametrize("bad_ack", [False, True])
def test_transport_close_failure_releases_lease(tmp_path, capsys, bad_ack):
    calls = []
    transport = MemoryTransport(lambda hello: {
        "t": "hello_ack", "epoch": hello["epoch"], "proto": 2 if bad_ack else 1,
        "accepted": True, "produced_at_ms": 1, "sent_at_ms": 1,
    })

    def close():
        calls.append("transport_close")
        raise OSError("close failed")

    transport.close = close
    assert bridge_main.main([], steps=_steps(tmp_path, calls, transport)) == 4
    assert calls[-2:] == ["transport_close", "release"]
    stderr = capsys.readouterr().err
    assert '"code":"transport_close_failed"' in stderr
    if bad_ack:
        assert stderr.index('"code":"hello_ack_rejected"') < stderr.index('"code":"transport_close_failed"')


def test_cli_subscribe_roundtrip():
    out = io.StringIO()
    hello = {"t": "hello", "epoch": "epoch1", "proto": 1,
             "produced_at_ms": 1, "sent_at_ms": 1, "app_version": "test",
             "runtime_pid": 1, "lang": "en_US",
             "caps": {"commands": [], "topics": [], "features": []},
             "scope": {"kind": "default", "id": "default"},
             "network_recovery": bridge_main.unknown_recovery()}
    transport = MemoryTransport(hello)
    original_send = transport.send

    def send(frame):
        original_send(frame)
        if frame["t"] == "subscribe":
            transport.reply = {
                "t": "subscribed", "epoch": frame["epoch"], "id": frame["id"],
                "topics": frame["topics"], "produced_at_ms": 1, "sent_at_ms": 1,
            }

    transport.send = send
    assert bridge_cli.main(["--id", "7", "subscribe", "jobs"],
                           transport=transport, output=out) == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [item["t"] for item in lines] == ["hello", "subscribed"]
    assert transport.sent[0]["t"] == "hello_ack"
    assert bridge_protocol.validate_control_reply(transport.sent[1], lines[1]) == []


def test_cli_drives_real_main_via_local_pair():
    out = io.StringIO()
    assert bridge_cli.main(["--local", "subscribe", "jobs"], output=out) == 0
    hello, reply = [json.loads(line) for line in out.getvalue().splitlines()]
    assert bridge_protocol.validate_frame(hello) == []
    assert reply["t"] == "error"
    assert reply["epoch"] == hello["epoch"]
    assert reply["error"]["code"] == "runtime_unavailable"


def test_cli_stdio_transport_uses_ndjson():
    request = {"t": "subscribe", "epoch": "epoch1", "id": 1,
               "topics": ["jobs"], "produced_at_ms": 1, "sent_at_ms": 1}
    reader = io.BytesIO(bridge_protocol.encode_frame(request))
    writer = io.BytesIO()
    transport = bridge_cli.StdioTransport(reader, writer)
    assert transport.receive() == request
    transport.send(request)
    assert writer.getvalue() == bridge_protocol.encode_frame(request)


def test_all_bridge_imports_without_qt():
    root = Path(__file__).resolve().parents[1]
    script = """import sys, importlib.util, importlib.machinery
sys.modules['PySide6'] = None
import bridge_protocol, bridge_instance_scope, bridge_jobs, bridge_main, bridge_cli
path = 'runtime_main.pyw'
loader = importlib.machinery.SourceFileLoader('runtime_main', path)
spec = importlib.util.spec_from_loader('runtime_main', loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
assert sys.modules['PySide6'] is None
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=root,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", ["ping", "snapshot_req", "service.stop", "publish_tick"])
@pytest.mark.parametrize("ordinary_write", ["complete", "pending", "busy"])
def test_run_bye_pending_is_a_permanent_last_frame_barrier(path, ordinary_write):
    from collections import deque
    from copy import deepcopy
    import logging
    import time
    from bridge_logs import LogCollector
    from bridge_publish import Publisher, SamplingWorker
    from bridge_state import PublicationState
    from bridge_traffic import TrafficCollector
    from tests.test_bridge_shutdown import setup

    shutdown, runtime, ownership = setup()
    session = bridge_main.BridgeSession.__new__(bridge_main.BridgeSession)
    session.epoch = shutdown.executor.epoch
    session.executor, session.shutdown = shutdown.executor, shutdown
    session.recorder = session.executor.recorder
    session.state = PublicationState(session.epoch)
    session.state.subscribe(["logs.batch", "jobs"])
    session.pending_acks, session.outgoing = deque(), deque()
    session._init_dispatch()
    session.subscribed_topics = {"logs.batch", "jobs"}
    session.exit_code = None
    session.diagnostics_closed = session._diagnostics_stopping = False
    session._diagnostics_flush_thread = None
    session._diagnostics_flush_done = False
    session.sampler = SamplingWorker(session.state, object())
    session.traffic = TrafficCollector(session.state, object())
    session.logs = LogCollector(session.state, logger=logging.Logger("bye-tail"), batch_limit=1)
    # More than one flush batch must be published, even with no periodic tick due.
    for n in range(3):
        assert session.logs.put_nowait({"message": f"final log {n}", "source": "TESSERACT"})
    quit_request = session.envelope("req", id=1, cmd="app.quit", args={}, **{"from": "shell"})
    late_request = {
        "ping": session.envelope("ping", n=7),
        "snapshot_req": session.envelope("snapshot_req", id=2, topic="core.info"),
        "service.stop": session.envelope("req", id=2, cmd="service.stop", args={}, **{"from": "shell"}),
        "publish_tick": None,
    }[path]
    assert not bridge_protocol.validate_frame(quit_request)
    assert late_request is None or not bridge_protocol.validate_frame(late_request)
    submitted, written = [], []
    deadline = time.monotonic() + 5

    class AsyncTransport:
        disconnected = False
        pending = None
        late_reads = 0
        busy_returned = False

        def send(self, frame):
            if self.pending is not None:
                return "busy"
            if ordinary_write == "busy" and frame["t"] != "bye" and not self.busy_returned:
                self.busy_returned = True
                return "busy"
            submitted.append(deepcopy(frame))
            if frame["t"] == "bye":
                assert session.diagnostics_closed
                assert session.logs.metrics()["queue_depth"] == 0
                assert not session.recorder.pending and not session.state.pending()
                assert not session.pending_acks and not session.outgoing
                self.pending = deepcopy(frame)
                return "pending"
            if ordinary_write == "pending":
                self.pending = deepcopy(frame)
                return "pending"
            written.append(deepcopy(frame))
            return "complete"

        def poll_write(self):
            if self.pending is None:
                return "complete"
            if self.pending["t"] == "bye" and self.late_reads < 2:
                assert "lease" not in ownership
                return "pending"
            written.append(self.pending)
            self.pending = None
            return "complete"

        def receive(self, timeout):
            assert time.monotonic() < deadline, "session did not finish"
            if quit_request:
                frame = dict(quit_request)
                quit_request.clear()
                return frame
            if shutdown.bye_submitted:
                self.late_reads += 1
                assert len(submitted) == submission_count[0]
                # Force the periodic path due while bye still owns the write.
                session.publisher.next_tick = 0
                return late_request if self.late_reads == 1 else None
            time.sleep(.001)
            return None

    session.transport = transport = AsyncTransport()
    session.publisher = Publisher(session.state, transport, jobs=session.recorder)
    session.publisher.next_tick = time.monotonic() + 100
    shutdown.send, shutdown.poll_write = transport.send, transport.poll_write
    shutdown.close_diagnostics = session.close_diagnostics
    shutdown.drain_publications = session.drain_publications
    shutdown.exit_process = lambda code: setattr(session, "exit_code", code)
    submission_count = [0]
    original_send = shutdown.send

    def send(frame):
        outcome = original_send(frame)
        if frame["t"] == "bye" and outcome == "pending":
            submission_count[0] = len(submitted)
        return outcome

    shutdown.send = send
    # Prove run skips ordinary entry points, even when poll_write could complete bye.
    for name in ("dispatch", "pump"):
        original = getattr(session, name)
        def guarded(*args, _original=original, **kwargs):
            assert not shutdown.bye_submitted
            return _original(*args, **kwargs)
        setattr(session, name, guarded)
    original_tick = session.publisher.tick
    def tick():
        assert not shutdown.bye_submitted
        return original_tick()
    session.publisher.tick = tick
    session._start_ingress()
    try:
        assert session.run() == 0, (submitted, session.publisher.diagnostics,
            session.pending_acks, session.outgoing, session.recorder.pending,
            session.logs.metrics(), ownership)
        assert shutdown.bye_submitted and transport.late_reads == 2
        assert [frame["t"] for frame in submitted].count("bye") == 1
        assert written == submitted and written[-1]["t"] == "bye"
        assert all(not bridge_protocol.validate_frame(frame) for frame in written)
        items = [item for frame in written for item in frame.get("items", [])]
        logs = [row["raw"] for item in items if item["topic"] == "logs.batch"
                for row in item["data"]["records"]]
        assert logs == [f"final log {n}" for n in range(3)]
        assert any(item["topic"] == "jobs" and item["data"]["name"] == "app.quit"
                   and item["data"]["state"] == "done" for item in items)
        assert ownership.count("lease") == 1
        assert transport.busy_returned == (ordinary_write == "busy")
        assert runtime.calls == [("request", {"actor": "app_exit"}), ("stop", runtime.intent)]
    finally:
        session.close_diagnostics(1)
