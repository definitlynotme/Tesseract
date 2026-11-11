"""P1 real source entry/Windows pipe integration, always temporary custom scope."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import threading
from collections import deque
from types import SimpleNamespace

import pytest

import bridge_instance_scope as scope_module
import bridge_main
import bridge_protocol as protocol
import bridge_transport as bt

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows integration")
# T-P2-H2 (决定_第五波裁定 T-P4-C2 #10): frame wait of the test shell (seconds);
# raise it on a loaded machine with TESSERACT_TEST_RECEIVE_TIMEOUT.
RECEIVE_TIMEOUT = float(os.environ.get("TESSERACT_TEST_RECEIVE_TIMEOUT") or 10)
# The first frame waits for interpreter start plus the sidecar's imports, the
# part a load spike stretches most; only that wait gets the longer default.
HELLO_TIMEOUT = max(RECEIVE_TIMEOUT, float(os.environ.get("TESSERACT_TEST_HELLO_TIMEOUT") or 30))
# Template one-shot source migration markers: with them the per-start
# bootstrap_user_files (T-P2-H2, 33-#10) never merges the default public
# source portfolio into the empty sandbox sources.yaml.
SOURCE_MARKERS = (b"migrations:\n  tier3_source_portfolio_defaults_v1: true\n"
                  b"  tier3_source_portfolio_defaults_v2: true\n"
                  b"  tier3_source_default_delivery_v1: true\n")
_WARM_CACHE = None


def warm_bytecode_cache():
    """T-P2-H2 (C2 #10): the real sidecar runs with -B, so without a cache every
    start compiles desktop_runtime.py (~2 MB) and the rest of its closure again,
    which can pass the hello window on a loaded machine. As in the p3 smokes,
    one child imports the sidecar's whole (Qt-free) closure once per test
    process, writing bytecode into a temporary PYTHONPYCACHEPREFIX (never the
    source tree); every sidecar of this process only reads it (it keeps -B and
    PYTHONDONTWRITEBYTECODE). The directory is removed at exit."""
    global _WARM_CACHE
    if _WARM_CACHE is None:
        import atexit
        import shutil
        import tempfile
        from sidecar_import_graph import sidecar_modules
        cache = tempfile.mkdtemp(prefix="tesseract-it-pycache-")
        atexit.register(shutil.rmtree, cache, True)
        # Imports never write a profile; should one, it lands in a throwaway
        # directory, never the real APPDATA/LOCALAPPDATA (as in p2_33).
        profile = tempfile.mkdtemp(prefix="tesseract-it-warm-profile-")
        atexit.register(shutil.rmtree, profile, True)
        script = ("import importlib, json, sys\n"
                  "for name in json.loads(sys.argv[1]):\n"
                  "    try:\n        importlib.import_module(name)\n"
                  "    except Exception:\n        pass\n")
        env = dict(os.environ, PYTHONPYCACHEPREFIX=cache, APPDATA=profile, LOCALAPPDATA=profile,
                   PYTHONPATH=str(Path(sys.prefix) / "Lib" / "site-packages"))
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("TESSERACT_ISOLATED_AUDIT_MANIFEST", None)
        try:
            subprocess.run([sys._base_executable, "-c", script, json.dumps(sidecar_modules())],
                           env=env, cwd=ROOT, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            pass    # a partial cache still helps; the sidecar compiles the rest
        _WARM_CACHE = cache
    return _WARM_CACHE


def isolated_env(directory):
    config, data = directory / "config", directory / "data"
    config.mkdir(parents=True)
    data.mkdir()
    (config / "config.yaml").write_bytes((ROOT / "config.example.yaml").read_bytes())
    (config / "sources.yaml").write_bytes(SOURCE_MARKERS + b"github_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n")
    env = dict(os.environ, TESSERACT_CONFIG_DIR=str(config), TESSERACT_DATA_DIR=str(data),
               TESSERACT_INSTANCE_SCOPE="p1-integration", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(Path(sys.prefix) / "Lib" / "site-packages"),
               PYTHONPYCACHEPREFIX=warm_bytecode_cache())
    env.pop("TESSERACT_ISOLATED_AUDIT_MANIFEST", None)
    return env


class Shell:
    def __init__(self, env, entry_code=None):
        self.frames = []
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p]
        api.CreateNamedPipeW.restype = wintypes.HANDLE
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        name = r"\\.\pipe\tesseract-integration-" + uuid.uuid4().hex
        server = api.CreateNamedPipeW(name, 3 | 0x40000000 | 0x80000, 8, 1, 65536, 65536, 0, None)
        assert server != wintypes.HANDLE(-1).value
        client = api.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
        assert client != wintypes.HANDLE(-1).value
        assert bt._immediate.SetHandleInformation(client, 1, 1)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [client]}
        entry = ["-c", entry_code] if entry_code else [str(ROOT / "runtime_main.pyw")]
        self.process = subprocess.Popen([sys._base_executable, "-B", *entry,
            "--ipc-handle", str(client), "--shell-pid", str(os.getpid())], env=env, cwd=ROOT,
            startupinfo=startup, close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bt._immediate.CloseHandle(client)
        self.transport = bt.PipeTransport(server, os.getpid())
        self.epoch = None

    def send(self, kind, **fields):
        now = time.time_ns() // 1_000_000
        frame = dict(t=kind, epoch=self.epoch, produced_at_ms=now, sent_at_ms=now, **fields)
        assert not protocol.validate_frame(frame)
        outcome = self.transport.send(frame)
        deadline = time.monotonic() + 5
        while outcome == "pending":
            assert time.monotonic() < deadline
            time.sleep(.005)
            outcome = self.transport.poll_write()
        assert outcome == "complete"

    def receive(self, timeout=None):
        frame = self.transport.receive(timeout=RECEIVE_TIMEOUT if timeout is None else timeout)
        if frame is None:
            raise AssertionError(f"frame missing; exit={self.process.poll()}")
        assert not protocol.validate_frame(frame), protocol.validate_frame(frame)
        self.frames.append(frame)
        return frame

    def until(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.receive(max(.01, deadline-time.monotonic()))
            if predicate(frame):
                return frame
        raise AssertionError("deadline")

    def hello(self):
        hello = self.receive(HELLO_TIMEOUT)
        assert hello["t"] == "hello"
        assert hello["runtime_pid"] == self.process.pid
        self.epoch = hello["epoch"]
        assert hello["scope"]["kind"] == "custom"
        assert hello["network_recovery"] == {
            key: dict(state="not_needed", error_code="scope_isolated", remaining_count=0)
            for key in ("proxy", "firewall")}
        self.send("hello_ack", proto=1, accepted=True)
        return hello

    def subscribe(self):
        self.send("subscribe", id=1, topics=sorted(protocol.P1_TOPICS))
        self.until(lambda frame: frame["t"] == "subscribed")
        seen = set()
        while seen != protocol.P1_SNAPSHOT_TOPICS:
            for item in events(self.receive()):
                if item["kind"] == "snapshot":
                    seen.add(item["topic"])
        return seen

    def job(self, name, request_id):
        self.send("req", id=request_id, cmd=name, args={}, **{"from": "shell"})
        ack = self.until(lambda frame: frame["t"] == "ack" and frame["id"] == request_id)
        assert ack["accepted"]
        terminal = self.until(lambda frame: any(item["topic"] == "jobs" and
            item["data"]["op"] == ack["op"] and item["data"]["state"] in
            {"done", "failed", "cancelled", "superseded"} for item in events(frame)))
        return next(item["data"] for item in events(terminal) if item["topic"] == "jobs" and
                    item["data"]["op"] == ack["op"] and item["data"]["state"] in {"done", "failed", "cancelled", "superseded"})

    def close(self):
        self.transport.close()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


def events(frame):
    return frame["items"] if frame["t"] == "batch" else [frame] if frame["t"] == "evt" else []


def assert_script(frames):
    assert frames[0]["t"] == "hello"
    assert all(not protocol.validate_frame(frame) for frame in frames)
    flattened = [item for frame in frames for item in events(frame)]
    assert {item["topic"] for item in flattened if item["kind"] == "snapshot"} == protocol.P1_SNAPSHOT_TOPICS
    assert any(item["topic"] == "service.state" and item["data"]["runtime"]["startup_phase"] == "collecting" for item in flattened)
    jobs = [item["data"] for item in flattened if item["topic"] == "jobs"]
    start = next(job for job in jobs if job["name"] == "service.start" and job["state"] == "failed")
    assert start["error"]["code"] in {"no_sources", "source_supply_no_egress", "needs_route", "internal"}
    assert all(any(job["name"] == name and job["state"] == "done" for job in jobs) for name in ("service.stop", "app.quit"))
    assert sum(item["topic"] == "service.state" and item["data"]["intent"]["stop_requested"]
               and item["data"]["runtime"]["startup_phase"] == "stopping" for item in flattened) >= 2
    assert frames[-1]["t"] == "bye" and frames[-1]["exit_code"] == 0
    assert all(part["error_code"] == "scope_isolated" for part in frames[-1]["network_recovery"].values())
    return dict(frames=len(frames), snapshots=sorted(protocol.P1_SNAPSHOT_TOPICS),
                start_failure=start["error"]["code"], bye=frames[-1])


def test_source_script(tmp_path):
    shell = Shell(isolated_env(tmp_path))
    try:
        shell.hello()
        shell.subscribe()
        assert shell.job("service.start", 2)["state"] == "failed"
        assert shell.job("service.stop", 3)["state"] == "done"
        assert shell.job("app.quit", 4)["state"] == "done"
        shell.until(lambda frame: frame["t"] == "bye")
        assert shell.process.wait(timeout=10) == 0
        assert_script(shell.frames)
    finally:
        shell.close()


def test_disconnect_stops_then_releases_instance(tmp_path):
    env = isolated_env(tmp_path)
    shell = Shell(env)
    try:
        shell.hello()
        shell.subscribe()
        shell.transport.close()
        assert shell.process.wait(timeout=10) == 0
        restart = Shell(env)
        try:
            restart.hello()
            assert restart.job("app.quit", 2)["state"] == "done"
            restart.until(lambda frame: frame["t"] == "bye")
        finally:
            restart.close()
    finally:
        shell.close()


def test_kill_restart_reports_rt1_without_touching_journal(tmp_path):
    env = isolated_env(tmp_path)
    shell = Shell(env)
    try:
        shell.hello()
        shell.process.kill()
        shell.process.wait(timeout=5)
    finally:
        shell.close()
    journal = Path(env["TESSERACT_DATA_DIR"]) / "runtime-state.json"
    content = b'{"system_proxy":{"active":true,"target":"127.0.0.1:7890","original":{"enabled":false,"server":"","override":""}}}'
    journal.write_bytes(content)
    restart = Shell(env)
    try:
        restart.hello()
        assert journal.read_bytes() == content
        assert restart.job("app.quit", 2)["state"] == "done"
        restart.until(lambda frame: frame["t"] == "bye")
        assert journal.read_bytes() == content
    finally:
        restart.close()


def test_second_sidecar_rejected(tmp_path):
    env = isolated_env(tmp_path)
    first = Shell(env)
    try:
        first.hello()
        second = subprocess.run([sys._base_executable, "-B", "runtime_main.pyw", "--stdio"],
            env=env, cwd=ROOT, capture_output=True, timeout=10)
        assert second.returncode == 3
        assert b'"code":"single_instance_rejected"' in second.stderr
        assert not second.stdout
        assert first.job("app.quit", 2)["state"] == "done"
        first.until(lambda frame: frame["t"] == "bye")
    finally:
        first.close()



def test_configured_secret_and_refresh_are_redacted_on_real_pipe(tmp_path):
    env = isolated_env(tmp_path)
    secret = "opaque-p1-synthetic-credential-814709"
    replacement = "opaque-p1-refreshed-credential-296314"
    (Path(env["TESSERACT_CONFIG_DIR"]) / "config.local.yaml").write_bytes(
        ("alerts:\n  generic_webhook:\n    enabled: false\n    headers:\n      X-Synthetic: " + secret + "\n").encode())
    # Controlled producer only: use the actual access extraction, registration,
    # logger, collector, publication, encoding, and process/pipe entry path.
    code = f"""import bridge_main, runpy
original = bridge_main.BridgeSession.refresh_secrets
def refresh(self):
    original(self)
    self.runtime.logger.info(self.runtime.config['alerts']['generic_webhook']['headers']['X-Synthetic'])
    self.runtime.config['alerts']['generic_webhook']['headers']['X-Synthetic'] = {replacement!r}
    original(self)
    self.runtime.logger.info({replacement!r})
bridge_main.BridgeSession.refresh_secrets = refresh
runpy.run_path('runtime_main.pyw', run_name='__main__')
"""
    shell = Shell(env, code)
    try:
        shell.hello()
        shell.subscribe()
        assert shell.job("service.start", 2)["state"] == "failed"
        shell.job("service.stop", 3)
        shell.job("app.quit", 4)
        shell.until(lambda frame: frame["t"] == "bye")
        encoded = json.dumps(shell.frames)
        assert secret not in encoded and replacement not in encoded
        rows = [row for frame in shell.frames for item in events(frame)
                if item["topic"] == "logs.batch" and item["kind"] == "edge"
                for row in item["data"]["records"]]
        assert sum("[redacted:" in row["raw"] for row in rows) >= 2
    finally:
        shell.close()


def test_active_original_gate_prevents_runtime_construction(tmp_path):
    from runtime_network_recovery import RecoveryReport, ProxyRecoveryGate, RecoveryStillRunning
    gate = ProxyRecoveryGate(time.monotonic()+1)
    calls = []
    report = RecoveryReport(bridge_main.unknown_recovery(), gate)
    scope = scope_module.InstanceScope("custom", "a"*24, scope_module.names_for_scope("custom", "a"*24), False, tmp_path, tmp_path)
    with pytest.raises(RecoveryStillRunning):
        bridge_main.BridgeSession(scope, report, object(), "test", runtime_factory=lambda *args: calls.append(args))
    assert calls == []



@pytest.mark.parametrize("name", ["start", "restart"])
@pytest.mark.parametrize("flag", ["superseded", "cancelled"])
def test_preempted_change_cannot_execute_after_ack_or_network_lock(name, flag):
    from bridge_jobs import JobContext, JobFailure
    session = bridge_main.BridgeSession.__new__(bridge_main.BridgeSession)
    context = JobContext("job-test")
    session.network_lock = threading.RLock()
    session.record_service = lambda: None
    session.access = SimpleNamespace(start=lambda: pytest.fail("old start executed"),
                                     restart_core=lambda: pytest.fail("old restart executed"))
    # The executor now owns acceptance; this callback checks preemption again
    # after acquiring the runtime serialization lock.
    errors = []
    def run():
        try:
            getattr(session, name)({}, context)
        except JobFailure as exc:
            errors.append(exc.code)
    with session.network_lock:
        worker = threading.Thread(target=run)
        worker.start()
        getattr(context, flag).set()
    worker.join(timeout=2)
    assert not worker.is_alive() and errors == [flag]


def test_pending_output_is_bounded_and_stamp_is_actual_submission():
    session = bridge_main.BridgeSession.__new__(bridge_main.BridgeSession)
    session.epoch = "test"
    session.pending_acks, session.outgoing = deque(), deque()
    session.change_receipts = {}
    session.publisher = SimpleNamespace(pending_write=True, next_tick=time.monotonic()+100)
    sent = []
    session.transport = SimpleNamespace(poll_write=lambda: "pending",
        send=lambda frame: sent.append(dict(frame)) or "complete")
    for n in range(64):
        session.dispatch(dict(t="ping", epoch="test", produced_at_ms=1, sent_at_ms=1, n=n))
        session.pump()
    with pytest.raises(bridge_main.StartupError, match="control_capacity"):
        session.dispatch(dict(t="ping", epoch="test", produced_at_ms=1, sent_at_ms=1, n=65))
    assert len(session.outgoing) == 64 and not sent
    time.sleep(.03)
    resumed_at = time.time_ns() // 1_000_000
    session.transport.poll_write = lambda: "complete"
    session.pump()
    assert len(sent) == 64
    assert all(frame["sent_at_ms"] >= resumed_at for frame in sent)
    assert all(frame["produced_at_ms"] < frame["sent_at_ms"] for frame in sent)


def test_shutdown_retains_ownership_until_diagnostics_are_closed():
    from tests.test_bridge_shutdown import setup
    shutdown, _, calls = setup()
    try:
        ack = shutdown.request_quit()
        assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
        shutdown.close_diagnostics = lambda budget: False
        assert not shutdown.finish_quit(deadline_seconds=.1)
        assert "lease" not in calls
        shutdown.close_diagnostics = lambda budget: True
        assert shutdown.finish_quit(deadline_seconds=.1)
    finally:
        shutdown.executor.shutdown(time.monotonic()+1)


def test_unsubscribed_snapshot_rejected_without_ending_real_session(tmp_path):
    shell = Shell(isolated_env(tmp_path))
    try:
        shell.hello()
        for request_id in (10, 20):
            shell.send("snapshot_req", id=request_id, topic="core.info")
            rejected = shell.until(lambda frame: frame["t"] == "snapshot_res" and frame["id"] == request_id)
            assert rejected["topic"] == "core.info" and rejected["error"]["code"] == "not_allowed"
            assert not protocol.validate_control_reply(dict(t="snapshot_req", epoch=shell.epoch,
                produced_at_ms=1, sent_at_ms=1, id=request_id, topic="core.info"), rejected)
        shell.send("subscribe", id=1, topics=["service.state"])
        shell.until(lambda frame: frame["t"] == "subscribed")
        shell.send("snapshot_req", id=11, topic="core.info")
        assert shell.until(lambda frame: frame["t"] == "snapshot_res" and frame["id"] == 11)["error"]["code"] == "not_allowed"
        shell.send("ping", n=7)
        shell.until(lambda frame: frame["t"] == "pong" and frame["n"] == 7)
        shell.subscribe()
        shell.send("snapshot_req", id=12, topic="core.info")
        assert "snapshot" in shell.until(lambda frame: frame["t"] == "snapshot_res" and frame["id"] == 12)
        assert shell.job("service.stop", 13)["state"] == "done"
        assert shell.job("app.quit", 14)["state"] == "done"
        shell.until(lambda frame: frame["t"] == "bye")
        assert shell.process.wait(timeout=10) == 0
    finally:
        shell.close()


@pytest.mark.parametrize("blocked", ["sampler", "traffic", "logs", "all_workers", "stream_close", "handler_close", "handler_emit", "final_flush"])
def test_real_diagnostics_tasks_retain_ownership_until_complete(blocked):
    from tests.test_bridge_shutdown import setup
    from bridge_publish import SamplingWorker
    from bridge_state import PublicationState
    from bridge_logs import LogCollector, BoundedMihomoStream
    from bridge_traffic import TrafficCollector
    shutdown, _, calls = setup()
    state = PublicationState("test-epoch")
    session = bridge_main.BridgeSession.__new__(bridge_main.BridgeSession)
    session.diagnostics_closed = False
    session._diagnostics_stopping = False
    session._diagnostics_flush_thread = None
    session._diagnostics_flush_done = False
    session.executor = shutdown.executor
    session.sampler = SamplingWorker(state, object())
    session.traffic = TrafficCollector(state, object())
    session.logs = LogCollector(state, logger=logging.Logger("blocked-cleanup"))
    entered, release = threading.Event(), threading.Event()
    entered_count = 0
    emitter = None
    entered_lock = threading.Lock()
    def block():
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if blocked != "all_workers" or entered_count == 3:
                entered.set()
        assert release.wait(5), "test failed to release diagnostic task"
    if blocked in {"sampler", "traffic", "logs"}:
        worker = getattr(session, blocked)
        if blocked == "sampler":
            worker.sample = block
        elif blocked == "traffic":
            worker.poll = block
        elif blocked == "logs":
            worker.flush = block
        worker.start()
    elif blocked == "all_workers":
        session.sampler.sample = session.traffic.poll = session.logs.flush = block
        for worker in (session.sampler, session.traffic, session.logs):
            worker.start()
    elif blocked == "stream_close":
        session.logs.stream = BoundedMihomoStream(lambda: None, "/logs", lambda _: None)
        session.logs.stream._response = SimpleNamespace(close=block)
    elif blocked == "handler_close":
        session.logs.start()
        session.logs._handler.close = block
    elif blocked == "handler_emit":
        class Message:
            def __str__(self):
                block()
                return "in-flight integration log"
        session.logs.start()
        emitter = threading.Thread(target=lambda: session.logs.logger.info(Message()))
        emitter.start()
    else:
        session.logs.flush = block
    shutdown.close_diagnostics = session.close_diagnostics
    try:
        if blocked in {"sampler", "traffic", "logs", "all_workers", "handler_emit"}:
            assert entered.wait(1)
        ack = shutdown.request_quit()
        assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
        began = time.monotonic()
        assert not shutdown.finish_quit(deadline_seconds=.05)
        assert time.monotonic() - began < .3
        assert entered.wait(1)
        assert not session.diagnostics_closed
        assert "lease" not in calls and not any(isinstance(item, dict) for item in calls)
        if blocked == "handler_emit":
            assert emitter.is_alive()
            assert session._diagnostics_flush_thread is None
            assert session.logs.metrics()["queue_depth"] == 0
        if blocked in {"sampler", "traffic", "logs"}:
            assert getattr(session, blocked)._thread.is_alive()
        elif blocked == "stream_close":
            assert session.logs.stream._close_thread.is_alive()
            closer = session.logs.stream._close_thread
        assert not shutdown.finish_quit(deadline_seconds=.01)
        if blocked == "stream_close":
            assert session.logs.stream._close_thread is closer
        release.set()
        assert shutdown.finish_quit(deadline_seconds=1)
        assert session.diagnostics_closed
        assert "lease" in calls and any(isinstance(item, dict) and item["t"] == "bye" for item in calls)
        assert all(worker._thread is None or not worker._thread.is_alive()
                   for worker in (session.sampler, session.traffic, session.logs))
        assert not session._diagnostics_flush_thread.is_alive()
        if emitter:
            emitter.join(1)
            assert not emitter.is_alive()
            assert session.logs._seq == 1 and session.logs.metrics()["queue_depth"] == 0
    finally:
        release.set()
        session.close_diagnostics(1)
        if emitter:
            emitter.join(1)


@pytest.mark.parametrize("stage", ["stop_blocked", "bye_busy"])
def test_quitting_rejects_first_subscription_and_snapshots_without_restarting_collection(stage):
    from tests.test_bridge_shutdown import setup
    from bridge_publish import Publisher, SamplingWorker
    from bridge_state import PublicationState
    from bridge_logs import LogCollector
    from bridge_traffic import TrafficCollector
    shutdown, runtime, calls = setup()
    state = PublicationState("test-epoch")
    session = bridge_main.BridgeSession.__new__(bridge_main.BridgeSession)
    session.epoch = "test-epoch"
    session.runtime = SimpleNamespace(logger=logging.Logger("quit-subscription"))
    session.executor, session.shutdown = shutdown.executor, shutdown
    session.pending_acks, session.outgoing = deque(), deque()
    session.subscribed_topics = set()
    session.diagnostics_closed = session._diagnostics_stopping = False
    session._diagnostics_flush_thread = None
    session._diagnostics_flush_done = False
    session.sampler = SamplingWorker(state, object())
    session.logs = LogCollector(state, logger=session.runtime.logger)
    session.traffic = TrafficCollector(state, object())
    session.publisher = Publisher(state, object())
    shutdown.close_diagnostics = session.close_diagnostics
    attempts = []
    def busy(frame):
        attempts.append(frame)
        return "busy"
    shutdown.send = busy
    release = threading.Event()
    if stage == "stop_blocked":
        runtime.stop_gate = release
    try:
        ack = shutdown.request_quit()
        assert shutdown.quitting
        if stage == "bye_busy":
            assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
            assert not shutdown.finish_quit(deadline_seconds=.05)
            assert attempts and all(frame["t"] == "bye" for frame in attempts)
            assert session.diagnostics_closed
        else:
            assert shutdown.executor.wait(ack.op, .01) is None
            assert not session.diagnostics_closed
        assert "lease" not in calls
        requests = [session.envelope("subscribe", id=41, topics=["traffic.history", "logs.batch"]),
                    session.envelope("snapshot_req", id=42, topic="core.info")]
        # Reject even a snapshot for a topic subscribed before quit began.
        session.subscribed_topics.add("core.info")
        for request in requests:
            session.dispatch(request)
            reply = session.outgoing[-1][0]
            assert reply["error"]["code"] == "not_allowed"
            assert reply["error"]["params"] == {"reason": "shutting_down"}
            assert not protocol.validate_control_reply(request, reply)
        assert session.subscribed_topics == {"core.info"}
        assert not session.publisher._controls
        assert not session.start_diagnostics()
        assert all(worker._thread is None for worker in (session.sampler, session.logs, session.traffic))
        assert not session.runtime.logger.handlers
        assert session.diagnostics_closed == (stage == "bye_busy")
        release.set()
        shutdown.send = lambda frame: (calls.append(frame), "complete")[1]
        assert shutdown.finish_quit(deadline_seconds=1)
        assert session.diagnostics_closed and shutdown.quitting
        flush_thread = session._diagnostics_flush_thread
        session.dispatch(session.envelope("subscribe", id=43, topics=["service.state"]))
        assert session.outgoing[-1][0]["error"]["code"] == "not_allowed"
        assert session.close_diagnostics(0)
        assert session._diagnostics_flush_thread is flush_thread and session.diagnostics_closed
        assert "lease" in calls and sum(isinstance(item, dict) and item["t"] == "bye" for item in calls) == 1
    finally:
        release.set()
        session.close_diagnostics(1)


def test_disconnect_during_blocked_start_and_cleanup_preserves_order_and_mutex(tmp_path):
    env = isolated_env(tmp_path)
    trace = tmp_path / "shutdown.jsonl"
    start_release, worker_release = tmp_path / "start.release", tmp_path / "worker.release"
    # Controlled blocking only, with the real entry, executor, sampler, stop,
    # isolated RT-1, shutdown coordinator, Windows pipe and cross-process mutex.
    code = f"""import bridge_main, bridge_instance_scope, runpy, time, threading, json
from pathlib import Path
trace = Path({str(trace)!r})
lock = threading.Lock()
def note(stage):
    with lock:
        with trace.open('a', encoding='utf-8') as out:
            out.write(json.dumps(stage) + '\\n')
def wait(path):
    deadline = time.monotonic() + 20
    while not Path(path).exists():
        if time.monotonic() >= deadline: raise RuntimeError('fixture_timeout')
        time.sleep(.01)
original_init = bridge_main.BridgeSession.__init__
def init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self.runtime.logger.disabled = True
    original_request, original_stop = self.runtime.request_stop, self.runtime.stop
    def request(**kwargs):
        note('intent')
        return original_request(**kwargs)
    def stop(**kwargs):
        note('stop.begin')
        result = original_stop(**kwargs)
        note('stop.end')
        return result
    self.runtime.request_stop, self.runtime.stop = request, stop
    original_recovery = self.recovery
    def recovery(*args, **kwargs):
        note('recovery.begin')
        result = original_recovery(*args, **kwargs)
        note('recovery.end')
        return result
    self.recovery = recovery
bridge_main.BridgeSession.__init__ = init
def start(self, args, context):
    with self.network_lock:
        note('start.entered')
        try:
            wait({str(start_release)!r})
            self.require_current(context)
        finally:
            note('start.finished')
bridge_main.BridgeSession.start = start
original_connect = bridge_main.BridgeSession.connect
def connect(self, *args, **kwargs):
    original_connect(self, *args, **kwargs)
    def sample():
        note('worker.entered')
        wait({str(worker_release)!r})
        note('worker.finished')
    self.sampler.sample = sample
bridge_main.BridgeSession.connect = connect
original_close = bridge_main.BridgeSession.close_diagnostics
def close(self, budget):
    if not getattr(self, '_test_cleanup_entered', False):
        self._test_cleanup_entered = True
        note('diagnostics.begin')
    closed = original_close(self, budget)
    if closed and not getattr(self, '_test_cleanup_finished', False):
        self._test_cleanup_finished = True
        note('diagnostics.end')
    return closed
bridge_main.BridgeSession.close_diagnostics = close
original_lease_close = bridge_instance_scope.InstanceLease.close
def lease_close(self):
    if self._registered: note('lease.release')
    return original_lease_close(self)
bridge_instance_scope.InstanceLease.close = lease_close
result = runpy.run_path('runtime_main.pyw', run_name='__main__')
"""
    shell = Shell(env, code)
    def stages():
        return [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
    def await_stage(stage):
        deadline = time.monotonic() + 10
        while stage not in stages():
            assert shell.process.poll() is None, "sidecar exited before cleanup completed"
            assert time.monotonic() < deadline, stages()
            time.sleep(.01)
    def second_rejected():
        second = subprocess.run([sys._base_executable, "-B", "runtime_main.pyw", "--stdio"],
            env=env, cwd=ROOT, capture_output=True, timeout=10)
        assert second.returncode == 3 and b'"code":"single_instance_rejected"' in second.stderr
    try:
        shell.hello()
        shell.send("subscribe", id=1, topics=["jobs"])
        shell.until(lambda frame: frame["t"] == "subscribed")
        await_stage("worker.entered")
        shell.send("req", id=2, cmd="service.start", args={}, **{"from": "shell"})
        shell.until(lambda frame: frame["t"] == "ack" and frame["id"] == 2)
        await_stage("start.entered")
        shell.transport.close()
        await_stage("intent")
        assert "stop.begin" not in stages()
        second_rejected()
        start_release.touch()
        await_stage("diagnostics.begin")
        assert "lease.release" not in stages()
        second_rejected()
        worker_release.touch()
        assert shell.process.wait(timeout=10) == 0
        recorded = stages()
        ordered = ["start.entered", "intent", "start.finished", "stop.begin", "stop.end",
                   "recovery.begin", "recovery.end", "diagnostics.begin", "worker.finished",
                   "diagnostics.end", "lease.release"]
        assert [recorded.index(stage) for stage in ordered] == sorted(recorded.index(stage) for stage in ordered)
        restart = Shell(env)
        try:
            restart.hello()
            assert restart.job("app.quit", 3)["state"] == "done"
            restart.until(lambda frame: frame["t"] == "bye")
        finally:
            restart.close()
    finally:
        start_release.touch()
        worker_release.touch()
        shell.close()


def test_ping_cancel_running_start_and_quit_use_control_worker(tmp_path):
    code = """import bridge_main, runpy, time
original = bridge_main.BridgeSession.start
def slow(self, args, context):
    time.sleep(.4)
    return original(self, args, context)
bridge_main.BridgeSession.start = slow
runpy.run_path('runtime_main.pyw', run_name='__main__')
"""
    shell = Shell(isolated_env(tmp_path), code)
    try:
        shell.hello()
        shell.subscribe()
        shell.send("req", id=2, cmd="service.start", args={}, **{"from":"shell"})
        ack = shell.until(lambda frame: frame["t"] == "ack" and frame["id"] == 2)
        shell.send("ping", n=1)
        shell.until(lambda frame: frame["t"] == "pong" and frame["n"] == 1)
        shell.send("cancel", id=3, op=ack["op"])
        cancelled = shell.until(lambda frame: frame["t"] == "ack" and frame["id"] == 3)
        assert cancelled["accepted"]
        shell.until(lambda frame: any(item["topic"] == "jobs" and item["data"]["op"] == ack["op"]
            and item["data"]["state"] == "superseded" for item in events(frame)))
        shell.job("app.quit", 4)
        shell.until(lambda frame: frame["t"] == "bye")
        assert shell.process.wait(timeout=10) == 0
    finally:
        shell.close()


if __name__ == "__main__":
    print(json.dumps(assert_script([json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]), ensure_ascii=False))
