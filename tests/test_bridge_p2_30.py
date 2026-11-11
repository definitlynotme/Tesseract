"""RT-2 source acceptance: fake registry/firewall only, no host takeover."""
import atexit
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import killswitch
import system_proxy
from bridge_killswitch import KillSwitchAdapter
from desktop_runtime import DesktopRuntime
from tests.test_bridge_p2_29 import env, exit_session, _stopped_quit, ORIGINAL, pending


@pytest.fixture(autouse=True)
def firewall_authority(monkeypatch):
    monkeypatch.setattr(killswitch, "_FIREWALL_AUTHORITY", killswitch.FirewallAuthority())
    # A missing fake must fail rather than read/write the real user's registry.
    def forbidden(*args, **kwargs):
        raise AssertionError("real registry backend forbidden")
    monkeypatch.setattr(system_proxy.WinRegProxyBackend, "read", forbidden)
    monkeypatch.setattr(system_proxy.WinRegProxyBackend, "write", forbidden)


@pytest.fixture
def rt2(env, exit_session, monkeypatch):
    calls, rules = [], []
    monkeypatch.setattr(killswitch.KillSwitchManager, "_detect_admin", lambda self: True)
    ks = killswitch.KillSwitchManager({"enabled": True}, [],
        command_runner=lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, "", ""),
        event_capacity=32)
    ks.platform_name = "windows"
    ks.active, ks.state = True, "triggered"
    adapter = KillSwitchAdapter(ks)
    adapter.snapshot()
    rule = {"name": "{00000000-0000-0000-0000-000000000001}",
            "display_name": killswitch.KILL_SWITCH_RULE_NAMES["block"]}
    rules.append(rule)

    def firewall(cmd, **kwargs):
        assert kwargs["timeout"] > 0
        calls.append("firewall-delete" if "env" in kwargs else "firewall-read")
        if "env" in kwargs:
            identity = json.loads(kwargs["env"]["TESSERACT_RECOVERY_RULE_IDENTITY"])
            assert identity == {"rule_id": rule["name"], "display_name": rule["display_name"]}
            rules.remove(rule)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rules), "")

    monkeypatch.setattr(killswitch, "recovery_command_runner", firewall)
    runtime = exit_session.session.runtime
    runtime.kill_switch = ks
    runtime._runtime_intent_lock = threading.RLock()
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime.stop_event = threading.Event()
    runtime._runtime_post_dial_observer_lock = threading.Lock()
    runtime._runtime_post_dial_observer = SimpleNamespace(
        stop=lambda reason: calls.append(("observer-stop", reason)),
        join=lambda *_: pytest.fail("emergency observer stop must not join"))
    runtime._runtime_post_dial_observer_lease = object()
    runtime._runtime_post_dial_tap = object()
    runtime._stop_runtime_post_dial_observer = lambda *a, **kw: (
        DesktopRuntime._stop_runtime_post_dial_observer(runtime, *a, **kw))

    def core_stop():
        assert runtime._runtime_post_dial_observer is None
        assert runtime._runtime_post_dial_observer_lease is None
        assert runtime._runtime_post_dial_tap is None
        assert calls[-1] == ("observer-stop", "session_end")
        calls.append("core-stop")

    runtime.core_runner = SimpleNamespace(request_stop=core_stop)
    runtime.begin_session_end = lambda **kw: DesktopRuntime.begin_session_end(runtime, **kw)
    shutdown = exit_session.shutdown
    shutdown.executor.register("app.sessionEnd", shutdown.session_end.handle)
    env.manager.enable_transaction(env.store)
    env.registry.hook = lambda event: calls.append("proxy-write") if event == "before_write" else None
    yield SimpleNamespace(env=env, session=exit_session.session, shutdown=shutdown,
                          runtime=runtime, ks=ks, calls=calls, rules=rules,
                          adapter=adapter, events=exit_session.events)
    atexit.unregister(ks.cleanup)
    assert shutdown.session_end.done.wait(2) if shutdown.session_end.started else True


def submit(rt2, budget=500, ident="session-end-1"):
    return rt2.shutdown.executor.submit("app.sessionEnd",
        {"reason": "shutdown", "budget_ms": budget}, req_id=ident, owner="shell")


def test_long_network_and_lifecycle_locks_do_not_block_emergency(rt2):
    entered = threading.Event()
    lifecycle = threading.Lock()
    original_stop = rt2.session.access.stop

    def blocked_stop(**kwargs):
        entered.set()
        with rt2.session.network_lock, lifecycle:
            return original_stop(**kwargs)

    rt2.session.access.stop = blocked_stop
    rt2.session.network_lock.acquire()
    lifecycle.acquire()
    try:
        stop = rt2.shutdown.request_stop()
        assert entered.wait(1)
        assert rt2.shutdown.executor.wait(stop.op, .01) is None
        start = time.monotonic()
        receipt = submit(rt2)
        response = receipt.wait(.8)
        assert response.ok, response
        assert time.monotonic() - start < .8
        data = response.data
        assert data["writes_revoked"] and data["workers_pending"]
        assert set(data["recovery"].values()) == {"restored"}
        assert rt2.calls == ["proxy-write", "firewall-read", "firewall-delete",
                             "firewall-read", ("observer-stop", "session_end"), "core-stop"]
        assert rt2.runtime._session_end_requested
        assert rt2.runtime.stop_event.is_set()
        assert not rt2.runtime._desired_running
        assert rt2.shutdown.executor.wait(stop.op, .01) is None
        assert not rt2.shutdown.executor.submit_normal(lambda: None)
    finally:
        lifecycle.release()
        rt2.session.network_lock.release()


@pytest.mark.parametrize("has_core", [True, False])
def test_emergency_core_exit_detaches_observer_without_join(rt2, has_core):
    runtime = rt2.runtime
    if not has_core:
        runtime.core_runner = None
    runtime.begin_session_end()
    assert runtime._runtime_post_dial_observer is not None
    assert rt2.calls == []
    runtime.begin_session_end(request_core_exit=True)
    assert runtime._runtime_post_dial_observer is None
    assert runtime._runtime_post_dial_observer_lease is None
    assert runtime._runtime_post_dial_tap is None
    assert rt2.calls == [("observer-stop", "session_end"), *(["core-stop"] if has_core else [])]


def test_late_writers_credentials_and_killswitch_edges(rt2):
    assert submit(rt2).wait(1).data["writes_revoked"]
    writes = list(rt2.env.registry.writes)
    assert rt2.env.manager.enable_transaction(rt2.env.store)["state"] == "writes_revoked"
    assert rt2.env.manager.restore_transaction(rt2.env.store)["state"] == "writes_revoked"
    assert rt2.env.manager.emergency_restore_transaction(rt2.env.store, object())["state"] == "writes_revoked"
    assert rt2.env.registry.writes == writes
    assert not rt2.ks.activate()
    assert not rt2.ks.force_on()
    rt2.ks.reconfigure({"enabled": True}, [])
    with pytest.raises(killswitch.FirewallWritesRevoked):
        rt2.ks._add_rule("name=late")
    with pytest.raises(killswitch.FirewallWritesRevoked):
        rt2.ks._delete_rule(killswitch.KILL_SWITCH_RULE_NAMES["block"])
    assert killswitch.emergency_restore_firewall(object(), manager=rt2.ks,
        deadline=time.monotonic() + 1)["error_code"] == "writes_revoked"
    replacement = killswitch.KillSwitchManager({"enabled": True}, [],
        command_runner=lambda *_: pytest.fail("late manager wrote firewall"))
    atexit.unregister(replacement.cleanup)
    assert not replacement.activate()
    batch = rt2.ks.drain(0)
    assert [e.reason for e in batch.events] == ["writes_revoked", "emergency_restore"]
    frames = rt2.adapter.drain()
    assert [f["kind"] for f in frames] == ["edge", "edge"]
    assert [f["data"]["active"] for f in frames] == [True, False]
    assert not rt2.adapter.drain()


def test_proxy_timeout_retains_gate_and_repeated_request_does_not_recover_twice(rt2):
    entered, release = threading.Event(), threading.Event()
    def block(event):
        if event == "before_write":
            entered.set()
            assert release.wait(2)
    rt2.env.registry.hook = block
    try:
        response = submit(rt2, 60).wait(1)
        assert entered.is_set()
        assert response.ok and response.data["writes_revoked"]
        assert all(v == "unknown" for v in response.data["recovery"].values())
        with pytest.raises(RuntimeError, match="still running"):
            rt2.shutdown.session_end.gate.require_idle()
        credential = system_proxy._PROXY_EMERGENCY_CREDENTIAL
        duplicate = rt2.shutdown.session_end.handle({"reason": "logoff", "budget_ms": 99999})
        assert duplicate == response.data
        assert system_proxy._PROXY_EMERGENCY_CREDENTIAL is credential
        assert not rt2.shutdown.finish_quit(deadline_seconds=.02)
        assert "lease" not in rt2.events and "core-stop" not in rt2.calls
    finally:
        release.set()
    assert rt2.shutdown.session_end.done.wait(1)
    assert len(rt2.env.registry.writes) == 2  # enable + the original in-flight restore
    assert pending(rt2.env)  # no post-deadline journal cleanup
    assert rt2.calls == []  # no new firewall transaction after timeout
    assert rt2.shutdown.finish_quit(deadline_seconds=1)
    assert rt2.events[-2:] == ["lease", ("exit", 0)]


def test_zero_budget_never_claims_revocation(rt2):
    data = submit(rt2, 0).wait(1).data
    assert not data["writes_revoked"]
    assert all(v == "unknown" for v in data["recovery"].values())
    assert rt2.calls == []
    assert len(rt2.env.registry.writes) == 1


def test_intent_failure_is_unknown_and_does_not_touch_os(rt2, monkeypatch):
    def fail(**_):
        raise RuntimeError("short intent failed")
    monkeypatch.setattr(rt2.runtime, "begin_session_end", fail)
    data = submit(rt2).wait(1).data
    assert not data["writes_revoked"] and data["workers_pending"]
    assert rt2.calls == []


def test_superseded_proxy_credential_is_reported_as_unknown(rt2, monkeypatch):
    original = rt2.env.manager.emergency_restore_transaction
    def supersede(store, credential, **kwargs):
        rt2.env.manager.revoke_writes()
        return original(store, credential, **kwargs)
    monkeypatch.setattr(rt2.env.manager, "emergency_restore_transaction", supersede)
    response = submit(rt2).wait(1)
    assert response.ok and response.data["writes_revoked"]
    assert response.data["recovery"]["proxy"] == "unknown"
    assert response.data["recovery"]["firewall"] == "restored"
    assert pending(rt2.env)


@pytest.mark.parametrize("fsync_number", [1, 2])
def test_journal_fsync_wait_uses_original_deadline(rt2, fsync_number):
    entered, release = threading.Event(), threading.Event()
    count = 0
    def block(event):
        nonlocal count
        if event == "fsync":
            count += 1
            if count == fsync_number:
                entered.set()
                assert release.wait(2)
    rt2.env.fs.hook = block
    try:
        data = submit(rt2, 60).wait(1).data
        assert entered.is_set()
        assert data["recovery"]["proxy"] == "unknown"
        assert not rt2.shutdown.finish_quit(deadline_seconds=.02)
    finally:
        release.set()
    assert rt2.shutdown.session_end.done.wait(1)
    assert len(rt2.env.registry.writes) == fsync_number
    assert pending(rt2.env)


def test_isolated_session_end_never_calls_registry_or_firewall(rt2):
    rt2.shutdown.session_end.allowed = False
    data = submit(rt2).wait(1).data
    assert data["writes_revoked"]
    assert all(v == "not_needed" for v in data["recovery"].values())
    assert rt2.calls == [("observer-stop", "session_end"), "core-stop"]
    assert len(rt2.env.registry.writes) == 1


def test_final_barrier_reproduces_p2_29b_r_and_closes_future_entries(env, exit_session):
    env.manager.enable_transaction(env.store)
    credential = env.manager.revoke_writes()["recovery_credential"]
    entered, release = threading.Event(), threading.Event()
    threads, results = [], []
    def block(event):
        if event == "before_write":
            entered.set()
            assert release.wait(2)
    env.registry.hook = block
    def diagnostics(_remaining):
        if not threads:
            assert system_proxy.system_proxy_writes_idle()
            worker = threading.Thread(target=lambda: results.append(
                env.manager.emergency_restore_transaction(env.store, credential)))
            threads.append(worker)
            worker.start()
            assert entered.wait(1)
        return True
    shutdown = _stopped_quit(exit_session)
    shutdown.close_diagnostics = diagnostics
    try:
        assert not shutdown.finish_quit(deadline_seconds=.05)
        assert exit_session.events == ["stop"]
        assert not system_proxy.system_proxy_writes_idle()
        denied = env.manager.emergency_restore_transaction(env.store, credential)
        assert denied["error_code"] == "recovery_closed"
    finally:
        release.set()
        for worker in threads:
            worker.join(1)
    assert results[0]["state"] == "restored"
    assert shutdown.finish_quit(deadline_seconds=1)
    assert exit_session.events[1]["t"] == "bye"
    assert exit_session.events[2:] == ["lease", ("exit", 0)]
    later = system_proxy.SystemProxyManager(backend=env.registry, platform_name="windows")
    assert later.emergency_gate is env.manager.emergency_gate
    credential = later.revoke_writes()["recovery_credential"]
    assert later.emergency_restore_transaction(env.store, credential)["error_code"] == "recovery_closed"


def test_final_barrier_waits_for_independent_firewall_recovery(rt2, exit_session, monkeypatch):
    credential = killswitch.revoke_killswitch_writes(rt2.ks)
    entered, release = threading.Event(), threading.Event()
    threads, results = [], []
    original = killswitch.recovery_command_runner

    def block(cmd, **kwargs):
        if "env" in kwargs:
            entered.set()
            assert release.wait(2)
        return original(cmd, **kwargs)

    monkeypatch.setattr(killswitch, "recovery_command_runner", block)

    def recover():
        results.append(killswitch.emergency_restore_firewall(
            credential, manager=rt2.ks, deadline=time.monotonic() + 2))
        rt2.events.append("firewall-finished")

    def diagnostics(_remaining):
        if not threads:
            killswitch._FIREWALL_AUTHORITY.gate.require_idle()
            worker = threading.Thread(target=recover)
            threads.append(worker)
            worker.start()
            assert entered.wait(1)
        return True

    shutdown = _stopped_quit(exit_session)
    shutdown.close_diagnostics = diagnostics
    try:
        assert not shutdown.finish_quit(deadline_seconds=.05)
        assert not shutdown.session_end.started  # Independent of the RT-2 worker gate.
        assert rt2.events == ["stop"]
        assert threads[0].is_alive()
        with pytest.raises(RuntimeError, match="still running"):
            killswitch._FIREWALL_AUTHORITY.gate.require_idle()
        denied = killswitch.emergency_restore_firewall(
            credential, manager=rt2.ks, deadline=time.monotonic() + 1)
        assert denied["error_code"] == "recovery_closed"
        assert "firewall-delete" not in rt2.calls
    finally:
        release.set()
        for worker in threads:
            worker.join(1)
    assert not threads[0].is_alive()
    assert results[0]["state"] == "restored"
    assert rt2.events == ["stop", "firewall-finished"]
    assert rt2.calls.count("firewall-delete") == 1
    assert shutdown.finish_quit(deadline_seconds=1)
    assert rt2.events[2]["t"] == "bye"
    assert rt2.events[3:] == ["lease", ("exit", 0)]
    finished = list(rt2.events)
    assert shutdown.finish_quit(deadline_seconds=1)
    assert rt2.events == finished
    assert len(threads) == 1
    denied = killswitch.emergency_restore_firewall(
        killswitch.revoke_killswitch_writes(rt2.ks), manager=rt2.ks,
        deadline=time.monotonic() + 1)
    assert denied["error_code"] == "recovery_closed"
    assert rt2.calls.count("firewall-delete") == 1


def test_final_barrier_counts_transactions_waiting_for_proxy_lock(env, exit_session):
    env.manager.enable_transaction(env.store)
    credential = env.manager.revoke_writes()["recovery_credential"]
    with system_proxy._proxy_transaction():
        thread = threading.Thread(target=env.manager.emergency_restore_transaction,
                                  args=(env.store, credential))
        thread.start()
        deadline = time.monotonic() + 1
        while not env.manager.emergency_gate._active and time.monotonic() < deadline:
            time.sleep(.001)
        assert not env.manager.emergency_gate.close_and_wait(.01)
    thread.join(1)
    assert env.manager.emergency_gate.close_and_wait(.1)


def test_firewall_timeout_remains_active_and_rejects_second_recovery(rt2, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = killswitch.recovery_command_runner
    def block(cmd, **kw):
        if "env" in kw:
            entered.set()
            assert release.wait(2)
        return original(cmd, **kw)
    monkeypatch.setattr(killswitch, "recovery_command_runner", block)
    try:
        data = submit(rt2, 60).wait(1).data
        assert entered.is_set()
        assert data["recovery"]["proxy"] == "restored"
        assert data["recovery"]["firewall"] == "unknown"
        credential = killswitch.revoke_killswitch_writes()
        result = killswitch.emergency_restore_firewall(credential,
            manager=rt2.ks, deadline=time.monotonic() + 1)
        assert result["error_code"] == "recovery_in_progress"
        assert not rt2.shutdown.finish_quit(deadline_seconds=.02)
    finally:
        release.set()
    assert rt2.shutdown.session_end.done.wait(1)
    assert rt2.calls.count("firewall-delete") == 1
    assert "core-stop" not in rt2.calls


def test_revocation_does_not_wait_for_active_killswitch_command(rt2, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def block(cmd, **kwargs):
        if "add" in cmd:
            entered.set()
            assert release.wait(2)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    rt2.ks.active = False
    rt2.ks.command_runner = block
    thread = threading.Thread(target=rt2.ks.activate)
    thread.start()
    assert entered.wait(1)
    try:
        data = submit(rt2, 60).wait(1).data
        assert data["writes_revoked"]
        assert data["recovery"]["firewall"] == "unknown"
        assert killswitch._FIREWALL_AUTHORITY.revoked
        # The recovery attempt can finish its deadline wait while the OLD
        # ordinary OS call remains active. Final release must still wait.
        assert rt2.shutdown.session_end.done.wait(1)
        assert not rt2.shutdown._close_recovery_entries(time.monotonic() + .02)
    finally:
        release.set()
        thread.join(1)


def test_real_emergency_ingress_preserves_first_reply_and_rejects_overflow(rt2):
    from tests.test_bridge_p2_03 import Pipe, spin
    s = rt2.session
    s.shutdown = rt2.shutdown
    s.transport = Pipe()
    s.publisher = SimpleNamespace(pending_write=False, next_tick=time.monotonic() + 100,
                                  tick=lambda: None)
    def frame(ident):
        return s.envelope("req", id=ident, cmd="app.sessionEnd",
            args={"reason": "logoff", "budget_ms": 500}, req_id=f"session-{ident}",
            owner="shell", **{"from": "shell"})
    s.enqueue_input(frame(100))
    s.enqueue_input(frame(101))
    s._start_ingress()
    try:
        spin(s, lambda: any(f.get("id") == 100 for f in s.transport.sent))
        replies = {f["id"]: f for f in s.transport.sent if f.get("id") in {100, 101}}
        assert replies[100]["ok"]
        assert replies[100]["data"]["writes_revoked"]
        assert replies[101]["error"]["code"] == "conflict"
        assert replies[101]["error"]["message_key"] == "errors.bridge.session_end_in_progress"
        assert rt2.calls.count("proxy-write") == 1
        assert rt2.calls.count("firewall-delete") == 1
        assert rt2.calls.count("core-stop") == 1
    finally:
        s._ingress_stop.set()
        for worker in s._ingress_threads:
            worker.join(1)


def test_ingress_queue_time_consumes_first_budget_and_duplicates_do_not_extend_it(rt2):
    from tests.test_bridge_p2_03 import Pipe, spin
    s = rt2.session
    s.shutdown = rt2.shutdown
    s.transport = Pipe()
    s.publisher = SimpleNamespace(pending_write=False, next_tick=time.monotonic() + 100,
                                  tick=lambda: None)
    def frame(ident, budget=20, epoch=None):
        return {**s.envelope("req", id=ident, cmd="app.sessionEnd",
            args={"reason": "logoff", "budget_ms": budget}, req_id=f"session-{ident}",
            owner="shell", **{"from": "shell"}), "epoch": epoch or s.epoch}
    s._record_ingress(frame(99, epoch="stale-epoch"))
    assert s.shutdown.session_end._received_deadline is None
    s.enqueue_input(frame(100))
    deadline = s.shutdown.session_end._received_deadline
    s.enqueue_input(frame(101, budget=60000))
    assert s.shutdown.session_end._received_deadline == deadline
    # Deliberately delay the emergency ingress consumer, not an OS operation.
    threading.Event().wait(.04)
    s._start_ingress()
    try:
        spin(s, lambda: any(f.get("id") == 100 for f in s.transport.sent))
        response = next(f for f in s.transport.sent if f.get("id") == 100)
        assert response["ok"] and not response["data"]["writes_revoked"]
        assert response["data"]["recovery"] == {"proxy": "unknown", "firewall": "unknown"}
        assert rt2.calls == []
        assert len(rt2.env.registry.writes) == 1
    finally:
        s._ingress_stop.set()
        for worker in s._ingress_threads:
            worker.join(1)


# Real child processes, file-backed FAKE registry and isolated journal. No
# process state, locks or credentials are reset to pretend a restart occurred.
MATRIX_WORKER = r'''
import json, os, sys
from dataclasses import asdict, replace
from pathlib import Path
import runtime_state
from runtime_state import RuntimeStateStore
from system_proxy import SystemProxyManager, ProxySettings
from runtime_network_recovery import recover_startup_system_proxy
root, mode, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
registry_path = root / "registry.json"
original = ProxySettings(False, "old:8080", "old-bypass")
def crash():
    os._exit(73)
class Registry:
    armed = False
    def read(self):
        return ProxySettings(**json.loads(registry_path.read_bytes()))
    def persist(self, value):
        registry_path.write_bytes(json.dumps(asdict(value)).encode())
    def checkpoint(self, name):
        if self.armed and point == name:
            crash()
    def write(self, value):
        self.checkpoint("before_registry")
        current = self.read()
        self.persist(replace(current, enabled=value.enabled))
        self.checkpoint("partial_enabled")
        self.persist(replace(value, override=current.override))
        self.checkpoint("partial_server")
        self.persist(value)
        self.checkpoint("after_registry")
registry = Registry()
store = RuntimeStateStore(root / "journal.json")
if mode == "recover":
    report = recover_startup_system_proxy(store, may_take_over_host_network=True,
                                          backend=registry, deadline_seconds=2)
    report.gate.require_idle()
    print(json.dumps(dict(report=report, registry=asdict(registry.read()),
                          journal=store.load_proxy_journal())))
else:
    registry.persist(original)
    manager = SystemProxyManager({"bypass": ["localhost"]}, backend=registry,
                                 platform_name="windows")
    assert manager.enable_transaction(store)["state"] == "applied"
    credential = manager.revoke_writes()["recovery_credential"]
    registry.armed = True
    raw_fsync, raw_replace = os.fsync, os.replace
    counts = {"fsync": 0, "replace": 0}
    def checkpoint(kind, side, number):
        phase = "restoring" if number == 1 else "clearing"
        if point == phase + "_" + side + "_" + kind:
            crash()
    def fsync(fd):
        counts["fsync"] += 1
        checkpoint("fsync", "before", counts["fsync"])
        raw_fsync(fd)
        checkpoint("fsync", "after", counts["fsync"])
    def replace_file(a, b):
        counts["replace"] += 1
        checkpoint("replace", "before", counts["replace"])
        raw_replace(a, b)
        checkpoint("replace", "after", counts["replace"])
    os.fsync, os.replace = fsync, replace_file
    manager.emergency_restore_transaction(store, credential)
    raise AssertionError("crash checkpoint was not reached")
'''


@pytest.mark.parametrize("point", [
    "before_registry", "partial_enabled", "partial_server", "after_registry",
    *[f"{phase}_{side}_{operation}" for phase in ("restoring", "clearing")
      for operation in ("fsync", "replace") for side in ("before", "after")],
])
def test_crash_takeover_matrix_uses_new_process(tmp_path, point):
    command = [sys.executable, "-B", "-c", MATRIX_WORKER, str(tmp_path)]
    crashed = subprocess.run([*command, "crash", point], capture_output=True, text=True,
                             timeout=10, cwd=Path(__file__).resolve().parents[1])
    assert crashed.returncode == 73, crashed.stderr
    recovered = subprocess.run([*command, "recover", point], capture_output=True, text=True,
                               timeout=10, cwd=Path(__file__).resolve().parents[1])
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    partial = point.startswith("partial_")
    state = ("not_owned" if partial else "not_needed" if point == "clearing_after_replace"
             else "restored")
    assert result["report"]["state"] == state
    assert ("system_proxy" in result["journal"]) is partial
    if not partial:
        assert result["registry"] == asdict(ORIGINAL)
    else:
        assert result["registry"] != asdict(ORIGINAL)
