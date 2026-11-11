"""B-39 transactions; every registry and filesystem operation is a fake."""
import io
import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import runtime_state
import system_proxy
from runtime_network_recovery import recover_startup_system_proxy
from runtime_state import RuntimeStateStore
from system_proxy import ProxySettings, SystemProxyManager


ORIGINAL = ProxySettings(False, "old:8080", "old-bypass")
TARGET = ProxySettings(True, "127.0.0.1:7890", "localhost")
OTHER = ProxySettings(True, "other:1080", "other-bypass")


class Crash(BaseException):
    pass


class FakeFS:
    def __init__(self):
        self.files = {}
        self.handles = {}
        self.events = []
        self.hook = lambda event: None

    def event(self, event):
        self.events.append(event)
        self.hook(event)

    def path(self, name):
        fs = self

        class Path:
            def __str__(self):
                return name

            @property
            def name(self):
                return name.rsplit("/", 1)[-1]

            @property
            def parent(self):
                return fs.path("fake:")

            def mkdir(self, **kwargs):
                pass

            def read_bytes(self):
                if name not in fs.files:
                    raise FileNotFoundError(name)
                return fs.files[name]

            def unlink(self):
                if name not in fs.files:
                    raise FileNotFoundError(name)
                del fs.files[name]

            def stat(self):
                return SimpleNamespace(st_size=len(self.read_bytes()), st_mtime_ns=1)

        return Path()

    def mkstemp(self, **kwargs):
        fd = len(self.handles) + 10
        name = f"fake:/temp-{fd}"
        self.handles[fd] = (name, None)
        self.files[name] = b""
        return fd, name

    def fdopen(self, fd, mode, **kwargs):
        base = io.BytesIO if "b" in mode else io.StringIO

        class Handle(base):
            def fileno(self):
                return fd

            def close(self):
                pass

        handle = Handle()
        name, _ = self.handles[fd]
        self.handles[fd] = (name, handle)
        return handle

    def fsync(self, fd):
        self.event("fsync")

    def atomic_replace(self, source, target):
        self.event("before_replace")
        handle = next(h for n, h in self.handles.values() if n == source)
        value = handle.getvalue()
        self.files[str(target)] = value.encode() if isinstance(value, str) else value
        self.files.pop(source, None)
        self.event("after_replace")


class Registry:
    def __init__(self, fs):
        self.current = replace(ORIGINAL)
        self.writes = []
        self.fs = fs
        self.hook = lambda event: None

    def read(self):
        self.hook("read")
        return replace(self.current)

    def write(self, settings):
        self.fs.event("registry_write")
        self.hook("before_write")
        self.writes.append(replace(settings))
        self.current = replace(self.current, enabled=settings.enabled)
        self.hook("enable_written")
        self.current = replace(self.current, server=settings.server)
        self.hook("server_written")
        self.current = replace(settings)
        self.hook("written")


@pytest.fixture
def env(monkeypatch):
    # Each fixture models a new process; no production reset API exists.
    monkeypatch.setattr(system_proxy, "_PROXY_RUNTIME_CONSTRUCTED", False)
    monkeypatch.setattr(system_proxy, "_PROXY_RT1_PHASE", None)
    monkeypatch.setattr(system_proxy, "_PROXY_CONTEXT", threading.local())
    monkeypatch.setattr(system_proxy, "_PROXY_EMERGENCY_CREDENTIAL", None)
    fs = FakeFS()
    store = RuntimeStateStore("unused")
    store.path = fs.path("fake:/journal.json")
    monkeypatch.setattr(runtime_state, "Path", fs.path)
    monkeypatch.setattr(runtime_state.tempfile, "mkstemp", fs.mkstemp)
    monkeypatch.setattr(runtime_state.os, "fdopen", fs.fdopen)
    monkeypatch.setattr(runtime_state.os, "fsync", fs.fsync)
    monkeypatch.setattr(runtime_state.os, "replace", fs.atomic_replace)
    monkeypatch.setattr(runtime_state.os, "unlink", lambda n: fs.files.pop(n, None))
    monkeypatch.setattr(system_proxy, "_PROXY_WRITES_REVOKED", threading.Event())
    registry = Registry(fs)
    manager = SystemProxyManager({"bypass": ["localhost"]}, backend=registry, platform_name="windows")
    return SimpleNamespace(fs=fs, store=store, registry=registry, manager=manager, monkeypatch=monkeypatch)


def recover(env):
    # Crash recovery runs in a NEW process, carrying only registry/journal.
    assert system_proxy.system_proxy_writes_idle()
    with env.monkeypatch.context() as process:
        process.setattr(system_proxy, "_PROXY_RUNTIME_CONSTRUCTED", False)
        process.setattr(system_proxy, "_PROXY_WRITES_REVOKED", threading.Event())
        process.setattr(system_proxy, "_PROXY_EMERGENCY_CREDENTIAL", None)
        result = recover_startup_system_proxy(
            env.store, may_take_over_host_network=True, backend=env.registry)
        result.gate.require_idle()
        return result


def pending(env):
    return "system_proxy" in env.store.load_proxy_journal()


@pytest.mark.parametrize("point,expected,state,retained", [
    ("before_journal", ORIGINAL, "not_needed", False),
    ("after_journal", ORIGINAL, "restored", False),
    ("partial_enable", replace(ORIGINAL, enabled=True), "not_owned", True),
    ("partial_server", ORIGINAL, "restored", False),
    ("before_readback", ORIGINAL, "restored", False),
    ("before_clear", ORIGINAL, "restored", False),
])
def test_crash_matrix(env, point, expected, state, retained):
    def fs_hook(event):
        if (point == "before_journal" and event == "before_replace"
                or point == "after_journal" and event == "after_replace"):
            raise Crash(point)

    def registry_hook(event):
        if {"partial_enable": "enable_written", "partial_server": "server_written",
            "before_readback": "written"}.get(point) == event:
            raise Crash(point)

    env.fs.hook = fs_hook
    env.registry.hook = registry_hook
    with pytest.raises(Crash):
        env.manager.enable_transaction(env.store)
        if point == "before_clear":
            # No restoration journal mutation here: fail only after original
            # has actually been written and read back.
            def fs_clear(event):
                if event == "before_replace":
                    raise Crash(point)
            env.registry.hook = lambda event: setattr(env.fs, "hook", fs_clear) if event == "written" else None
            env.manager.restore_transaction(env.store)
    env.fs.hook = lambda event: None
    env.registry.hook = lambda event: None
    result = recover(env)
    assert result["state"] == state
    assert env.registry.current == expected
    assert pending(env) is retained


def test_durable_order_phases_and_duplicate_enable(env):
    assert env.manager.enable_transaction(env.store)["state"] == "applied"
    events = env.fs.events
    assert events.index("fsync") < events.index("after_replace") < events.index("registry_write")
    entry = env.store.load_proxy_journal()["system_proxy"]
    assert entry["phase"] == "applied"
    assert entry["owner"].startswith("tesseract:")
    # Reconstructing a manager cannot recapture our current proxy as original.
    another = SystemProxyManager({"bypass": ["localhost"]}, backend=env.registry, platform_name="windows")
    assert another.enable_transaction(env.store)["state"] == "applied"
    assert env.store.load_proxy_journal()["system_proxy"] == entry
    assert another.restore_transaction(env.store)["state"] == "restored"
    assert env.registry.current == ORIGINAL
    assert not pending(env)


@pytest.mark.parametrize("failure", ["fsync", "before_replace"])
def test_journal_failure_never_writes_registry(env, failure):
    def hook(event):
        if event == failure:
            raise OSError("disk")
    env.fs.hook = hook
    assert env.manager.enable_transaction(env.store)["error_code"] == "journal_write_failed"
    assert env.registry.writes == []
    assert env.registry.current == ORIGINAL
    assert not pending(env)
    assert not any("temp" in name for name in env.fs.files)


@pytest.mark.parametrize("operation", ["enable_transaction", "restore_transaction", "restore", "disable", "restore_settings"])
def test_other_client_is_never_overwritten_or_cleared(env, operation):
    env.manager.enable_transaction(env.store)
    env.registry.current = OTHER
    before = len(env.registry.writes)
    args = [env.store] if operation.endswith("transaction") else [ORIGINAL] if operation == "restore_settings" else []
    assert getattr(env.manager, operation)(*args)["state"] == "not_owned"
    env.store.clear()  # Legacy DesktopRuntime finally must be harmless.
    assert env.registry.current == OTHER
    assert len(env.registry.writes) == before
    assert pending(env)
    assert recover(env)["state"] == "not_owned"


@pytest.mark.parametrize("operation", ["enable", "enable_transaction", "restore", "disable", "restore_settings", "restore_transaction", "apply_proxy_override"])
def test_revocation_blocks_all_manager_writers(env, operation):
    env.manager.enable_transaction(env.store)
    env.manager.bypass = ["printer.local"]
    env.manager.revoke_writes()
    args = [env.store] if operation.endswith("transaction") else [ORIGINAL] if operation == "restore_settings" else []
    result = getattr(env.manager, operation)(*args)
    assert result["error_code"] == "writes_revoked"
    assert len(env.registry.writes) == 1
    assert env.registry.current == TARGET
    assert pending(env)
    other_manager = SystemProxyManager(backend=env.registry, platform_name="windows")
    assert other_manager.enable()["error_code"] == "writes_revoked"


def test_revoked_while_journal_write_in_flight_blocks_late_write(env):
    entered, release = threading.Event(), threading.Event()
    answers = []

    def hook(event):
        if event == "fsync":
            entered.set()
            assert release.wait(2)

    env.fs.hook = hook
    thread = threading.Thread(target=lambda: answers.append(env.manager.enable_transaction(env.store)))
    thread.start()
    try:
        assert entered.wait(2)
        env.manager.revoke_writes()  # Does not wait for the transaction lock.
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert answers[0]["error_code"] == "writes_revoked"
    assert env.registry.writes == []
    assert pending(env)
    env.fs.hook = lambda event: None
    assert recover(env)["state"] == "restored"


@pytest.mark.parametrize("operation", ["enable_transaction", "restore_transaction"])
def test_takeover_during_journal_fsync_is_rechecked(env, operation):
    if operation == "restore_transaction":
        env.manager.enable_transaction(env.store)
    before = len(env.registry.writes)
    env.fs.hook = lambda event: setattr(env.registry, "current", OTHER) if event == "fsync" else None
    assert getattr(env.manager, operation)(env.store)["state"] == "not_owned"
    assert env.registry.current == OTHER
    assert len(env.registry.writes) == before
    assert pending(env)


def test_read_failure_and_restorable_false_do_not_clear(env):
    env.manager.enable_transaction(env.store)
    env.manager.original_settings = None

    def hook(event):
        if event == "read":
            raise OSError("registry unreadable")
    env.registry.hook = hook
    assert env.manager.restore_transaction(env.store)["state"] == "unknown"
    env.store.clear()
    assert pending(env)
    assert recover(env)["state"] == "unknown"


def test_phase_persistence_failure_retains_prepared_snapshot(env):
    def hook(event):
        if event == "fsync" and env.registry.writes:
            raise OSError("disk")
    env.fs.hook = hook
    assert env.manager.enable_transaction(env.store)["error_code"] == "journal_phase_write_failed"
    assert env.registry.current == TARGET
    assert env.store.load_proxy_journal()["system_proxy"]["phase"] == "prepared"
    env.fs.hook = lambda event: None
    assert recover(env)["state"] == "restored"


@pytest.mark.parametrize("failure,phase,state", [("write", "write_failed", "failed"), ("read", "unknown", "unknown"), ("mismatch", "verify_failed", "failed")])
def test_actual_enable_failure_is_journaled(env, failure, phase, state):
    def hook(event):
        if failure == "write" and event == "server_written":
            raise OSError("partial write")
        if failure == "read" and event == "read" and env.registry.writes:
            raise OSError("read denied")
        if failure == "mismatch" and event == "written":
            env.registry.current = replace(TARGET, override="unexpected")
    env.registry.hook = hook
    assert env.manager.enable_transaction(env.store)["state"] == state
    assert env.store.load_proxy_journal()["system_proxy"]["phase"] == phase
    env.registry.hook = lambda event: None
    assert recover(env)["state"] == "restored"


def test_clear_failure_keeps_journal_and_next_rt1_verifies_original(env):
    env.manager.enable_transaction(env.store)

    def hook(event):
        if event == "before_replace" and env.registry.current == ORIGINAL:
            raise OSError("clear denied")
    env.fs.hook = hook
    assert env.manager.restore_transaction(env.store)["error_code"] == "journal_clear_failed"
    assert pending(env)
    assert env.registry.current == ORIGINAL
    env.fs.hook = lambda event: None
    writes = len(env.registry.writes)
    assert recover(env)["state"] == "restored"
    assert len(env.registry.writes) == writes
    assert not pending(env)


@pytest.mark.parametrize("legacy", [True, False])
def test_rt1_compatibility_and_unrelated_entries(env, legacy):
    entry = {"active": True, "target": TARGET.server, "original": asdict(ORIGINAL)}
    if not legacy:
        entry.update(owner="tesseract:previous-process", phase="applied")
    env.fs.files[str(env.store.path)] = json.dumps({"system_proxy": entry, "other": {"keep": True}}).encode()
    env.registry.current = TARGET
    assert recover(env)["state"] == "restored"
    assert env.registry.current == ORIGINAL
    assert env.store.load_proxy_journal() == {"other": {"keep": True}}


@pytest.mark.parametrize("payload", [b"{bad", b'{"system_proxy": {"active": true}}'])
def test_corrupt_journal_cannot_be_overwritten(env, payload):
    env.fs.files[str(env.store.path)] = payload
    assert env.manager.enable_transaction(env.store)["state"] == "unknown"
    assert env.manager.restore_transaction(env.store)["state"] == "unknown"
    env.store.clear()
    assert env.fs.files[str(env.store.path)] == payload
    assert env.registry.writes == []


def test_runtime_allowed_entrypoints_and_legacy_restore(env):
    from desktop_runtime import DesktopRuntime

    runtime = object.__new__(DesktopRuntime)
    runtime.system_proxy = env.manager
    runtime.runtime_state = env.store
    assert runtime.enable_system_proxy()["state"] == "applied"
    env.registry.current = OTHER
    assert runtime.restore_system_proxy()["state"] == "not_owned"
    assert pending(env)
    assert runtime.disable_system_proxy()["state"] == "not_owned"
    assert runtime.recover_stale_system_proxy() is False
    assert pending(env)
    env.registry.current = TARGET
    assert runtime.restore_system_proxy()["state"] == "restored"
    assert not pending(env)


def test_revocation_exposes_inflight_write_gate(env):
    entered, release = threading.Event(), threading.Event()
    answers = []

    def hook(event):
        if event == "before_write":
            entered.set()
            assert release.wait(2)
    env.registry.hook = hook
    thread = threading.Thread(target=lambda: answers.append(env.manager.enable_transaction(env.store)))
    thread.start()
    try:
        assert entered.wait(2)
        env.manager.revoke_writes()
        assert not system_proxy.system_proxy_writes_idle()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert system_proxy.system_proxy_writes_idle()
    assert answers[0]["state"] == "applied"  # Already admitted OS call.
    assert env.manager.enable_transaction(env.store)["error_code"] == "writes_revoked"
    assert recover(env)["state"] == "restored"


def test_inherited_journal_is_not_deleted_by_legacy_clear(env):
    env.manager.enable_transaction(env.store)
    inherited = RuntimeStateStore("fake:/journal.json")
    inherited.clear()
    assert pending(env)


def test_restore_verification_mismatch_retains_original_and_journal(env):
    env.manager.enable_transaction(env.store)
    env.registry.hook = lambda event: setattr(env.registry, "current", OTHER) if event == "written" else None
    assert env.manager.restore_transaction(env.store)["error_code"] == "registry_verify_mismatch"
    assert env.manager.original_settings == ORIGINAL
    assert pending(env)
    env.store.clear()
    assert pending(env)
    env.registry.hook = lambda event: None
    assert recover(env)["state"] == "not_owned"


def test_unrelated_entries_survive_transaction_and_legacy_clear(env):
    env.fs.files[str(env.store.path)] = b'{"other": {"keep": true}}'
    env.manager.enable_transaction(env.store)
    assert env.manager.restore()["state"] == "restored"
    env.store.clear()
    assert env.store.load_proxy_journal()["other"] == {"keep": True}


@pytest.mark.parametrize("method", ["disable", "restore", "restore_settings"])
def test_unbound_legacy_restore_checks_ownership(env, method):
    env.manager.enable()
    env.registry.current = OTHER
    args = [ORIGINAL] if method == "restore_settings" else []
    assert getattr(env.manager, method)(*args)["state"] == "not_owned"
    assert env.registry.current == OTHER
    assert env.manager.original_settings == ORIGINAL


def test_journal_target_conflict_preserves_old_snapshot(env):
    env.manager.enable_transaction(env.store)
    before = env.store.path.read_bytes()
    env.manager.port = 7891
    assert env.manager.enable_transaction(env.store)["error_code"] == "journal_target_conflict"
    assert env.store.path.read_bytes() == before
    assert env.manager.restore_transaction(env.store)["state"] == "restored"


def test_corrupt_new_owner_or_phase_is_preserved(env):
    env.manager.enable_transaction(env.store)
    for key, value in [("owner", "another-app"), ("phase", "invalid")]:
        payload = env.store.load_proxy_journal()
        payload["system_proxy"][key] = value
        env.fs.files[str(env.store.path)] = json.dumps(payload).encode()
        assert recover(env)["error_code"] == "journal_corrupt"
        assert pending(env)


def test_defense_in_depth_journal_changed_before_clear_is_not_erased(env):
    # 纵深防御，并非并发保证：不覆盖比较完成与 replace 之间的窗口。
    env.manager.enable_transaction(env.store)

    def hook(event):
        if event == "read" and env.registry.current == ORIGINAL:
            payload = env.store.load_proxy_journal()
            payload["system_proxy"]["owner"] = "tesseract:another-transaction"
            env.fs.files[str(env.store.path)] = json.dumps(payload).encode()
    env.registry.hook = hook
    assert env.manager.restore_transaction(env.store)["error_code"] == "journal_changed"
    assert pending(env)
    assert env.store.load_proxy_journal()["system_proxy"]["owner"] == "tesseract:another-transaction"


def test_revoked_idle_gate_waits_for_phase_commit(env):
    entered, release = threading.Event(), threading.Event()

    def hook(event):
        if event == "fsync" and env.registry.writes:
            entered.set()
            assert release.wait(2)
    env.fs.hook = hook
    thread = threading.Thread(target=lambda: env.manager.enable_transaction(env.store))
    thread.start()
    try:
        assert entered.wait(2)
        env.manager.revoke_writes()
        assert system_proxy._PROXY_WRITES_IN_FLIGHT == 0
        assert not system_proxy.system_proxy_writes_idle()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert system_proxy.system_proxy_writes_idle()
    env.fs.hook = lambda event: None
    assert recover(env)["state"] == "restored"


@pytest.mark.parametrize("value", [[], {}, 1])
def test_nonstring_phase_is_corrupt_not_internal_recovery_error(env, value):
    env.manager.enable_transaction(env.store)
    payload = env.store.load_proxy_journal()
    payload["system_proxy"]["phase"] = value
    env.fs.files[str(env.store.path)] = json.dumps(payload).encode()
    assert recover(env)["error_code"] == "journal_corrupt"
    assert pending(env)


@pytest.mark.parametrize("legacy", [True, False])
def test_stop_before_first_enable_preserves_inherited_journal(env, legacy):
    from desktop_runtime import DesktopRuntime

    entry = {"active": True, "target": TARGET.server, "original": asdict(ORIGINAL)}
    if not legacy:
        entry.update(owner="tesseract:previous-process", phase="applied")
    raw = json.dumps({"system_proxy": entry}).encode()
    env.fs.files[str(env.store.path)] = raw
    env.registry.current = TARGET
    runtime = object.__new__(DesktopRuntime)
    runtime.system_proxy = env.manager
    runtime.runtime_state = env.store

    result = runtime.restore_system_proxy()
    assert result["state"] == "unknown"
    assert result["error_code"] == "original_missing"
    assert env.registry.writes == []
    assert env.registry.current == TARGET
    assert env.store.path.read_bytes() == raw
    assert recover(env)["state"] == "restored"
    assert env.registry.current == ORIGINAL
    assert not pending(env)


@pytest.mark.parametrize("current", [TARGET, OTHER])
def test_explicit_unbound_disable_keeps_legacy_behavior(env, current):
    env.registry.current = current
    result = env.manager.disable()
    if current == TARGET:
        assert result["state"] == "restored"
        assert env.registry.current == ProxySettings(False, "", "")
    else:
        assert result["state"] == "not_owned"
        assert env.registry.current == current
        assert env.registry.writes == []


@pytest.mark.parametrize("point", ["before_phase", "fsync", "after_replace", "before_write"])
@pytest.mark.parametrize("field", ["owner", "target", "original", "all"])
def test_defense_in_depth_restore_rejects_observed_replacement(env, point, field):
    # 纵深防御，并非并发保证；单写者不变量排除真实的并发替换。
    env.manager.enable_transaction(env.store)
    replacement = env.store.load_proxy_journal()["system_proxy"]
    changes = {"owner": "tesseract:new-transaction", "target": "127.0.0.1:7891",
               "original": asdict(OTHER)}
    replacement.update(changes if field == "all" else {field: changes[field]})
    raw = json.dumps({"system_proxy": replacement, "other": "keep"}).encode()
    writes = list(env.registry.writes)

    def change():
        env.fs.files[str(env.store.path)] = raw

    reads = 0

    def registry_hook(event):
        nonlocal reads
        if event == "read":
            reads += 1
            if (point == "before_phase" and reads == 1
                    or point == "before_write" and reads == 2):
                change()

    env.registry.hook = registry_hook
    env.fs.hook = lambda event: change() if event == point else None
    result = env.manager.restore_transaction(env.store)
    assert result["error_code"] == "journal_changed"
    assert result["state"] == "failed"
    assert env.store.path.read_bytes() == raw
    assert env.registry.current == TARGET
    assert env.registry.writes == writes


def test_same_thread_idle_gate_covers_journal_commits_and_clear(env):
    observations = []

    def hook(event):
        if event == "fsync":
            observations.append(system_proxy.system_proxy_writes_idle())
    env.fs.hook = hook
    assert system_proxy.system_proxy_writes_idle()
    assert env.manager.enable_transaction(env.store)["state"] == "applied"
    assert system_proxy.system_proxy_writes_idle()
    assert env.manager.restore()["state"] == "restored"
    assert observations == [False, False, False, False]
    assert system_proxy.system_proxy_writes_idle()


@pytest.mark.parametrize("point", ["fsync", "after_replace", "registry_write"])
def test_transaction_idle_counter_unwinds_after_crash(env, point):
    def hook(event):
        if event == point:
            assert not system_proxy.system_proxy_writes_idle()
            raise Crash(point)
    env.fs.hook = hook
    with pytest.raises(Crash):
        env.manager.enable_transaction(env.store)
    assert system_proxy.system_proxy_writes_idle()


@pytest.mark.parametrize("operation", ["save", "clear_snapshot", "legacy_clear"])
def test_journal_mutations_require_current_thread_authority(env, operation):
    with system_proxy._proxy_transaction():
        entry = env.store.save_proxy_snapshot(original=ORIGINAL, target=TARGET.server)
    raw = env.store.path.read_bytes()

    def write():
        if operation == "save":
            env.store.save_proxy_snapshot(original=OTHER, target=TARGET.server)
        elif operation == "clear_snapshot":
            env.store.clear_proxy_snapshot(entry)
        else:
            env.store.clear()

    with pytest.raises(RuntimeError, match="requires a transaction or RT-1"):
        write()
    errors = []

    def stranger():
        try:
            write()
        except RuntimeError as exc:
            errors.append(str(exc))

    # A different thread holding the transaction lock grants us no authority.
    with system_proxy._proxy_transaction():
        worker = threading.Thread(target=stranger)
        worker.start()
        worker.join(1)
        assert not worker.is_alive()
    assert len(errors) == 1
    assert env.store.path.read_bytes() == raw


def test_rt1_is_rejected_after_runtime_construction_or_revocation(env, monkeypatch):
    with pytest.raises(RuntimeError, match="pre-runtime"):
        recover_startup_system_proxy(env.store, may_take_over_host_network=True, backend=env.registry)
    monkeypatch.setattr(system_proxy, "_PROXY_RUNTIME_CONSTRUCTED", False)
    env.manager.revoke_writes()
    with pytest.raises(RuntimeError, match="writes_revoked"):
        recover_startup_system_proxy(env.store, may_take_over_host_network=True, backend=env.registry)
    assert env.registry.writes == []
    assert env.fs.events == []


def test_isolated_recovery_never_enters_rt1_phase_even_after_runtime_construction(env):
    result = recover_startup_system_proxy(
        env.store, may_take_over_host_network=False, backend=env.registry)
    assert result["error_code"] == "scope_isolated"
    assert system_proxy._PROXY_RT1_PHASE is None
    env.manager.revoke_writes()
    with pytest.raises(RuntimeError, match="writes_revoked"):
        recover_startup_system_proxy(env.store, may_take_over_host_network=False, backend=env.registry)
    assert env.registry.writes == []
    assert env.fs.events == []


def test_rt1_timeout_retains_exclusive_phase_until_worker_exits(env, monkeypatch):
    env.manager.enable_transaction(env.store)
    monkeypatch.setattr(system_proxy, "_PROXY_RUNTIME_CONSTRUCTED", False)
    entered, release = threading.Event(), threading.Event()

    def slow_read(event):
        if event == "read":
            system_proxy.assert_proxy_journal_writer()
            entered.set()
            assert release.wait(2)

    env.registry.hook = slow_read
    result = recover_startup_system_proxy(
        env.store, may_take_over_host_network=True, backend=env.registry, deadline_seconds=0.03)
    try:
        assert entered.is_set()
        assert result["error_code"] == "deadline_exceeded"
        assert not system_proxy.system_proxy_writes_idle()
        with pytest.raises(RuntimeError, match="still running"):
            SystemProxyManager(backend=env.registry, platform_name="windows")
        with pytest.raises(RuntimeError, match="overlap RT-1"):
            env.manager.restore_transaction(env.store)
        with pytest.raises(RuntimeError, match="overlap RT-1"):
            env.manager.revoke_writes()
        with pytest.raises(RuntimeError, match="requires a transaction or RT-1"):
            env.store.clear_proxy_snapshot(env.store.load_proxy_journal()["system_proxy"])
    finally:
        release.set()
        assert result.gate.done.wait(2)
    assert system_proxy._PROXY_RT1_PHASE is None
    assert system_proxy.system_proxy_writes_idle()
    assert len(env.registry.writes) == 1
    assert pending(env)


@pytest.mark.parametrize("current", [ORIGINAL, TARGET, OTHER])
@pytest.mark.parametrize("operation", ["restore", "restore_transaction", "restore_settings", "disable"])
def test_revoked_restore_rejects_even_no_write_and_takeover_paths(env, current, operation):
    env.manager.enable_transaction(env.store)
    env.registry.current = current
    raw = env.store.path.read_bytes()
    env.manager.revoke_writes()
    args = [env.store] if operation == "restore_transaction" else [ORIGINAL] if operation == "restore_settings" else []
    assert getattr(env.manager, operation)(*args)["state"] == "writes_revoked"
    assert env.registry.current == current
    assert len(env.registry.writes) == 1
    assert env.store.path.read_bytes() == raw


@pytest.mark.parametrize("operation", ["restore", "restore_settings", "disable", "restore_transaction"])
def test_revoked_unbound_restore_is_rejected(env, operation):
    env.manager.revoke_writes()
    args = [env.store] if operation == "restore_transaction" else [ORIGINAL] if operation == "restore_settings" else []
    assert getattr(env.manager, operation)(*args)["state"] == "writes_revoked"
    assert env.registry.writes == []
    assert env.fs.events == []


@pytest.mark.parametrize("already_restored", [False, True])
def test_emergency_credential_restores_verifies_clears_and_expires(env, already_restored):
    env.fs.files[str(env.store.path)] = b'{"other": "keep"}'
    env.manager.enable_transaction(env.store)
    if already_restored:
        env.registry.current = ORIGINAL
    credential = env.manager.revoke_writes()["recovery_credential"]
    assert env.manager.restore_transaction(env.store)["state"] == "writes_revoked"
    assert env.manager.emergency_restore_transaction(env.store, credential)["state"] == "restored"
    assert env.registry.current == ORIGINAL
    assert len(env.registry.writes) == (1 if already_restored else 2)
    assert env.store.load_proxy_journal() == {"other": "keep", "generated_at": env.store.load_proxy_journal()["generated_at"]}
    assert not pending(env)
    assert env.manager.emergency_restore_transaction(env.store, credential)["state"] == "writes_revoked"
    assert env.manager.enable_transaction(env.store)["state"] == "writes_revoked"


@pytest.mark.parametrize("bad", [None, object(), {"recovery_credential": "forged"}])
def test_forged_emergency_credential_is_denied(env, bad):
    env.manager.enable_transaction(env.store)
    credential = env.manager.revoke_writes()["recovery_credential"]
    raw = env.store.path.read_bytes()
    assert env.manager.emergency_restore_transaction(env.store, bad)["state"] == "writes_revoked"
    assert env.store.path.read_bytes() == raw
    assert len(env.registry.writes) == 1
    assert env.manager.emergency_restore_transaction(env.store, credential)["state"] == "restored"


def test_superseded_emergency_credential_is_denied(env):
    env.manager.enable_transaction(env.store)
    old = env.manager.revoke_writes()["recovery_credential"]
    current = env.manager.revoke_writes()["recovery_credential"]
    assert env.manager.emergency_restore_transaction(env.store, old)["state"] == "writes_revoked"
    assert len(env.registry.writes) == 1
    assert env.manager.emergency_restore_transaction(env.store, current)["state"] == "restored"


def test_emergency_credential_cannot_reenter_an_inflight_transaction(env):
    env.manager.enable_transaction(env.store)
    credential = env.manager.revoke_writes()["recovery_credential"]
    attempts = []

    def hook(event):
        if event == "fsync":
            attempts.append(env.manager.emergency_restore_transaction(env.store, credential))

    env.fs.hook = hook
    assert env.manager.emergency_restore_transaction(env.store, credential)["state"] == "restored"
    assert len(attempts) == 2
    assert all(item["state"] == "writes_revoked" for item in attempts)
    assert env.registry.current == ORIGINAL
    assert len(env.registry.writes) == 2
    assert not pending(env)


@pytest.mark.parametrize("failure,expected", [("takeover", "not_owned"), ("read", "unknown"),
                                              ("write", "failed"), ("verify", "failed")])
def test_emergency_failure_preserves_journal_and_consumes_credential(env, failure, expected):
    env.manager.enable_transaction(env.store)
    if failure == "takeover":
        env.registry.current = OTHER

    def hook(event):
        if (failure == "read" and event == "read"
                or failure == "write" and event == "before_write"):
            raise OSError("controlled failure")
        if failure == "verify" and event == "written":
            env.registry.current = OTHER

    env.registry.hook = hook
    credential = env.manager.revoke_writes()["recovery_credential"]
    assert env.manager.emergency_restore_transaction(env.store, credential)["state"] == expected
    assert pending(env)
    assert env.manager.emergency_restore_transaction(env.store, credential)["state"] == "writes_revoked"


def _qt_stop_fixture(env, monkeypatch):
    import desktop_runtime

    runtime = object.__new__(desktop_runtime.DesktopRuntime)
    runtime.system_proxy, runtime.runtime_state = env.manager, env.store
    runtime.last_error = None
    runtime.logger = Mock()
    runtime.stop_event = threading.Event()
    runtime._last_runtime_stop, runtime._last_core_stop = {}, {}
    runtime.manager = runtime.monitor_thread = runtime.subscription_server = None
    runtime.running = True
    runtime.core_runner = SimpleNamespace(request_stop=Mock())
    runtime.stop_core = Mock(return_value={"stopped": True})
    runtime._write_runtime_lifecycle_journal = Mock()
    runtime._validator_stop_incomplete = lambda _: False
    runtime._wait_for_runtime_background_work = lambda **_: {}
    runtime._cleanup_kill_switch_bounded = lambda **_: True
    runtime._clear_runtime_audit_export_overlay = Mock()
    runtime._clear_session_artifacts = Mock()
    runtime.status = lambda: {"last_error": runtime.last_error}
    logs = []
    monkeypatch.setattr(desktop_runtime, "try_log_nowait", lambda *a, **kw: logs.append((a, kw)))
    monkeypatch.setattr(desktop_runtime, "log_event", Mock())
    return runtime, logs


@pytest.mark.parametrize("caller", ["_preempt_runtime_io_for_stop", "_stop_locked"])
@pytest.mark.parametrize("state", ["exception", "failed", "unknown", "not_owned", "restored", "not_needed"])
def test_qt_structured_failure_matches_old_exception_feedback(env, monkeypatch, caller, state):
    runtime, logs = _qt_stop_fixture(env, monkeypatch)

    def restore():
        if state == "exception":
            raise OSError("controlled restore error")
        return {"state": state, "error_code": "controlled restore error" if state in {"failed", "unknown", "not_owned"} else None}

    runtime.restore_system_proxy = restore
    if caller == "_stop_locked":
        result = runtime._stop_locked(actor="user", reason="test")
        runtime.stop_core.assert_called_once()
        assert result["core_stop_status"] == "STOPPED"
        assert runtime.running is False
    else:
        runtime._preempt_runtime_io_for_stop()
        runtime.core_runner.request_stop.assert_called_once()
    errors = [(args, kwargs) for args, kwargs in logs if args[1] == "error"]
    if state in {"exception", "failed", "unknown", "not_owned"}:
        assert runtime.last_error == "controlled restore error"
        assert len(errors) == 1
        args, kwargs = errors[0]
        suffix = " preemption" if caller == "_preempt_runtime_io_for_stop" else ""
        assert args[2] == f"Failed to restore system proxy during stop{suffix}: %s"
        assert str(args[3]) == runtime.last_error
        assert kwargs["extra"] == {"event_key": "", "event_match_kind": "raw_only", "is_key_event": False}
    else:
        assert runtime.last_error is None
        assert errors == []


@pytest.mark.parametrize("caller", ["_preempt_runtime_io_for_stop", "_stop_locked"])
def test_qt_real_transaction_write_failure_remains_visible(env, monkeypatch, caller):
    env.manager.enable_transaction(env.store)
    runtime, logs = _qt_stop_fixture(env, monkeypatch)

    def fail(event):
        if event == "before_write":
            raise OSError("registry denied")

    env.registry.hook = fail
    if caller == "_stop_locked":
        runtime._stop_locked(actor="user", reason="test")
        runtime.stop_core.assert_called_once()
    else:
        runtime._preempt_runtime_io_for_stop()
        runtime.core_runner.request_stop.assert_called_once()
    assert runtime.last_error == "registry_write_failed"
    assert any(args[1] == "error" for args, _ in logs)
    assert env.registry.current == TARGET
    assert pending(env)


@pytest.fixture
def exit_session(env, monkeypatch):
    """Real session/recover/coordinator; stopped lifecycle is only test input.

    No recovery callback/result is replaced. Registry, journal and firewall
    subprocess backends are fakes; lease/transport record owner-thread output.
    """
    import subprocess
    import time
    import killswitch
    import runtime_network_recovery as recovery
    from bridge_main import BridgeSession
    from bridge_shutdown import ShutdownCoordinator

    events, firewall_calls = [], []
    owner = threading.get_ident()

    def firewall(command, **kwargs):
        firewall_calls.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(killswitch.subprocess, "run", firewall)

    # The host's persistent rule store is never read here: unreadable, so the
    # shipped inventory falls back to the faked PowerShell backend above.
    def no_registry(**_kwargs):
        raise OSError("registry rule store faked out")

    monkeypatch.setattr(killswitch, "registry_firewall_rules", no_registry)
    monkeypatch.setattr(recovery.ctypes.windll.shell32, "IsUserAnAdmin", lambda: True)
    gate = recovery.ProxyRecoveryGate(time.monotonic() + 2)
    gate.done.set()
    startup = recovery.RecoveryReport({}, gate)
    intent = SimpleNamespace(accepted=True, generation=1, lifecycle_revision=2, skipped_reason="")

    def stop(*, stop_intent):
        assert stop_intent is intent
        events.append("stop")
        return {"shutdown_exit_code": 0}

    runtime = SimpleNamespace(
        system_proxy=env.manager, runtime_state=env.store,
        request_stop=lambda **_: intent, stop=stop,
        lifecycle_status=lambda: {"intent": {}, "readiness": {}, "freshness": {},
            "runtime": {"startup_phase": "stopped", "shutdown_cleanup_pending": False},
            "core": {"cleanup_pending": False}},
    )

    def close_lease():
        assert threading.get_ident() == owner
        events.append("lease")

    session = BridgeSession(
        SimpleNamespace(kind="default", may_take_over_host_network=True), startup,
        SimpleNamespace(close=close_lease), "exit-test", runtime_factory=lambda *_: runtime)
    shutdown = ShutdownCoordinator(
        session.executor, session.access, lease=session.lease, startup_recovery=startup,
        recover_network=session.recover,
        send=lambda frame: (events.append(frame), "complete")[1],
        exit_process=lambda code: events.append(("exit", code)))
    yield SimpleNamespace(session=session, shutdown=shutdown, events=events,
                          firewall_calls=firewall_calls, startup_gate=gate)
    session.executor.shutdown(time.monotonic() + 2)


def _stopped_quit(exit_session):
    shutdown = exit_session.shutdown
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert exit_session.events == ["stop"]
    return shutdown


@pytest.mark.parametrize("journal", ["active", "restored", "absent"])
def test_default_exit_real_recovery_wiring_releases_lease_once(env, exit_session, journal):
    from bridge_protocol import validate_frame

    if journal != "absent":
        env.fs.files[str(env.store.path)] = b'{"other": "keep"}'
        assert env.manager.enable_transaction(env.store)["state"] == "applied"
        if journal == "restored":
            env.registry.current = ORIGINAL
    shutdown = _stopped_quit(exit_session)
    assert shutdown.finish_quit(deadline_seconds=1)
    assert shutdown.finish_quit(deadline_seconds=1)
    bye = exit_session.events[1]
    assert not validate_frame(bye)
    assert bye["network_recovery"]["proxy"]["state"] == (
        "not_needed" if journal == "absent" else "restored")
    assert exit_session.events[2:] == ["lease", ("exit", 0)]
    assert len(exit_session.firewall_calls) == 1
    assert 0 < exit_session.firewall_calls[0] <= 1
    assert system_proxy._PROXY_RT1_PHASE is None
    assert system_proxy._PROXY_RUNTIME_CONSTRUCTED
    assert not pending(env)
    if journal != "absent":
        assert env.store.load_proxy_journal()["other"] == "keep"
        assert env.registry.current == ORIGINAL
        assert env.manager.original_settings is None
        assert not env.manager.active


@pytest.mark.parametrize("failure,state,code", [
    ("read", "unknown", "registry_read_failed"),
    ("write", "failed", "registry_write_failed"),
    ("verify", "failed", "registry_verify_mismatch"),
    ("takeover", "not_owned", "proxy_taken_over"),
    ("revoked", "unknown", "writes_revoked"),
])
def test_default_exit_reports_real_recovery_failure(env, exit_session, failure, state, code):
    env.manager.enable_transaction(env.store)
    if failure == "takeover":
        env.registry.current = OTHER
    if failure == "revoked":
        env.manager.revoke_writes()

    def hook(event):
        if (failure == "read" and event == "read"
                or failure == "write" and event == "before_write"):
            raise OSError("controlled exit failure")
        if failure == "verify" and event == "written":
            env.registry.current = OTHER

    env.registry.hook = hook
    shutdown = _stopped_quit(exit_session)
    assert shutdown.finish_quit(deadline_seconds=1)
    proxy = exit_session.events[1]["network_recovery"]["proxy"]
    assert (proxy["state"], proxy["error_code"]) == (state, code)
    assert exit_session.events[2:] == ["lease", ("exit", 0)]
    assert pending(env)


@pytest.mark.parametrize("point", ["read", "before_write", "fsync", "before_replace"])
def test_default_exit_deadline_holds_gate_and_lease_until_writer_finishes(env, exit_session, point):
    import time

    env.manager.enable_transaction(env.store)
    entered, release = threading.Event(), threading.Event()

    def block(event):
        if event == point:
            system_proxy.assert_proxy_journal_writer()
            entered.set()
            assert release.wait(3)

    env.registry.hook = block
    env.fs.hook = block
    shutdown = _stopped_quit(exit_session)
    try:
        started = time.monotonic()
        assert not shutdown.finish_quit(deadline_seconds=.1)
        assert entered.wait(1)
        assert time.monotonic() - started < 1
        assert shutdown._recovery_done.wait(1)
        assert shutdown._recovery_error is None
        gate = shutdown._recovery_report.gate
        with pytest.raises(RuntimeError, match="still running"):
            gate.require_idle()
        assert not shutdown.finish_quit(deadline_seconds=.01)
        assert exit_session.events == ["stop"]
        assert not system_proxy.system_proxy_writes_idle()
    finally:
        release.set()
        if shutdown._recovery_report is not None:
            assert shutdown._recovery_report.gate.gates[0].done.wait(2)
    assert shutdown.finish_quit(deadline_seconds=1)
    proxy = exit_session.events[1]["network_recovery"]["proxy"]
    assert proxy["state"] == "unknown"
    assert proxy["error_code"] == ("commit_pending" if point == "before_replace" else "deadline_exceeded")
    assert exit_session.events[2:] == ["lease", ("exit", 0)]
    assert system_proxy.system_proxy_writes_idle()
    assert pending(env) is (point != "before_replace")
    assert len(env.registry.writes) == (1 if point == "read" else 2)
    assert exit_session.firewall_calls == []  # Shared budget was exhausted.


def test_default_exit_waiting_for_proxy_transaction_cannot_write_after_deadline(env, exit_session):
    env.manager.enable_transaction(env.store)
    shutdown = _stopped_quit(exit_session)
    with system_proxy._proxy_transaction():
        assert not shutdown.finish_quit(deadline_seconds=.05)
        assert shutdown._recovery_done.wait(1)
        assert exit_session.events == ["stop"]
    assert shutdown._recovery_report.gate.gates[0].done.wait(1)
    assert shutdown.finish_quit(deadline_seconds=1)
    assert len(env.registry.writes) == 1
    assert pending(env)


def test_default_exit_checks_original_startup_gate_before_recovery(env, exit_session):
    shutdown = _stopped_quit(exit_session)
    exit_session.startup_gate.done.clear()
    assert not shutdown.finish_quit(deadline_seconds=.01)
    assert shutdown._recovery_thread is None
    assert exit_session.events == ["stop"]
    assert env.fs.events == []
    assert exit_session.firewall_calls == []
    exit_session.startup_gate.done.set()
    assert shutdown.finish_quit(deadline_seconds=1)


def test_isolated_exit_has_no_backend_io_even_after_revocation(env, exit_session):
    exit_session.session.scope.may_take_over_host_network = False
    env.manager.revoke_writes()
    shutdown = _stopped_quit(exit_session)
    assert shutdown.finish_quit(deadline_seconds=1)
    result = exit_session.events[1]["network_recovery"]
    assert all(item["error_code"] == "scope_isolated" for item in result.values())
    assert env.fs.events == []
    assert env.registry.writes == []
    assert exit_session.firewall_calls == []


def test_default_exit_network_lock_wait_consumes_original_budget(env, exit_session):
    env.manager.enable_transaction(env.store)
    shutdown = _stopped_quit(exit_session)
    with exit_session.session.network_lock:
        assert not shutdown.finish_quit(deadline_seconds=.05)
        assert exit_session.events == ["stop"]
        # The worker starts its budget on dispatch; hold beyond that budget,
        # rather than racing the coordinator's slightly earlier timeout.
        assert not shutdown._recovery_done.wait(.1)
    assert shutdown.finish_quit(deadline_seconds=1)
    assert len(env.registry.writes) == 1
    assert pending(env)
    assert exit_session.firewall_calls == []
    assert all(item["error_code"] == "deadline_exceeded"
               for item in exit_session.events[1]["network_recovery"].values())


def test_default_exit_firewall_backend_failure_is_preserved_in_bye(env, exit_session, monkeypatch):
    import killswitch

    def fail(*args, **kwargs):
        raise OSError("controlled firewall failure")

    monkeypatch.setattr(killswitch.subprocess, "run", fail)
    shutdown = _stopped_quit(exit_session)
    assert shutdown.finish_quit(deadline_seconds=1)
    result = exit_session.events[1]["network_recovery"]
    assert result["proxy"]["state"] == "not_needed"
    assert result["firewall"] == {"state": "unknown", "error_code": "inspection_failed", "remaining_count": None}
    assert exit_session.events[2:] == ["lease", ("exit", 0)]
