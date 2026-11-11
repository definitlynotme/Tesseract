"""P3-02 inherited gate: fakes only, never use production object names."""
import argparse
import os
import subprocess
import sys
import importlib.util
from types import SimpleNamespace
from pathlib import Path
import pytest
import bridge_cli
import bridge_main
import bridge_instance_scope as api
from tests.test_bridge_instance_scope import FakeWin32


def custom():
    return api.InstanceScope("custom", "c" * 24, api.names_for_scope("custom", "c" * 24), False)


def test_inherited_holds_gate_until_final_cleanup():
    scope = custom()
    win = FakeWin32()
    parent, _ = win.create_mutex(scope.names.legacy_mutex)
    result = api.acquire_instance(scope, legacy_gate="inherited", win32=win)
    assert result.status == "acquired"
    win.close(parent)
    assert scope.names.legacy_mutex.casefold() in win.objects
    result.lease.close()
    assert not win.handles and not win.objects


def test_inherited_without_parent_fails_closed():
    win = FakeWin32()
    result = api.acquire_instance(custom(), legacy_gate="inherited", win32=win)
    assert (result.status, result.reason_code) == ("error", "legacy_gate_not_held")
    assert not win.handles and not win.objects


def test_inherited_runtime_already_owned():
    scope = custom()
    win = FakeWin32()
    first = api.acquire_instance(scope, win32=win)
    before = dict(win.handles)
    second = api.acquire_instance(scope, legacy_gate="inherited", win32=win)
    assert second.status == "already_running" and second.lease is None
    assert win.handles == before
    first.lease.close()
    assert not win.handles


@pytest.mark.parametrize("gate", ["create", "inherited"])
def test_runtime_parser(gate):
    assert bridge_cli.parse_runtime_args(argparse.ArgumentParser(), ["--legacy-gate", gate]).legacy_gate == gate


def test_default_parser_and_invalid_gate():
    assert bridge_cli.parse_runtime_args(argparse.ArgumentParser(), []).legacy_gate == "create"
    win = FakeWin32()
    assert api.acquire_instance(custom(), legacy_gate="invalid", win32=win).reason_code == "invalid_legacy_gate"
    assert not win.handles


@pytest.mark.parametrize("gate", [None, "create", "inherited"])
def test_actual_main_parser_and_acquire_wiring(gate):
    calls = []
    win = FakeWin32()
    scope = custom()
    parent, _ = win.create_mutex(scope.names.legacy_mutex) if gate == "inherited" else (None, 0)

    def acquire(actual_scope, **kwargs):
        calls.append((actual_scope, kwargs))
        return api.acquire_instance(actual_scope, win32=win, **kwargs)

    class Transport:
        def send(self, frame):
            self.hello = frame

        def receive(self):
            return {key: value for key, value in dict(self.hello, t="hello_ack", accepted=True).items()
                    if key in ("t", "epoch", "proto", "accepted", "produced_at_ms", "sent_at_ms")}

        def close(self):
            calls.append("transport_closed")

    transport = Transport()
    steps = bridge_main.Steps(scope=lambda: scope, acquire=acquire,
        logging=lambda _: None, audit=lambda _: None,
        recovery=lambda _: bridge_main.unknown_recovery(),
        transport=lambda args: transport, loop=lambda *_: None)
    assert bridge_main.main([] if gate is None else ["--legacy-gate", gate], steps=steps) == 0
    assert calls == [(scope, {"legacy_gate": "inherited"} if gate == "inherited" else {}), "transport_closed"]
    if parent:
        win.close(parent)
    assert not win.handles and not win.objects


def test_runtime_process_entry_accepts_inherited_and_rejects_missing_parent(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir(); data.mkdir()
    env = dict(os.environ, TESSERACT_INSTANCE_SCOPE="p3-entry-" + tmp_path.name,
               TESSERACT_CONFIG_DIR=str(config), TESSERACT_DATA_DIR=str(data),
               PYTHONDONTWRITEBYTECODE="1", TEMP=str(tmp_path), TMP=str(tmp_path))
    env.pop("TESSERACT_ISOLATED_AUDIT_MANIFEST", None)
    entry = Path(__file__).resolve().parents[1] / "runtime_main.pyw"
    result = subprocess.run([sys.executable, "-B", str(entry), "--legacy-gate", "inherited", "--stdio"],
                            env=env, capture_output=True, timeout=15)
    assert result.returncode == 3
    assert b"legacy_gate_not_held" in result.stderr
    assert b"unrecognized arguments" not in result.stderr


def test_smoke_cleanup_continues_after_watcher_and_log_errors(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "shell/app/src/p3_instance_smoke.py"
    spec = importlib.util.spec_from_file_location("p3_instance_smoke", source)
    smoke = importlib.util.module_from_spec(spec); spec.loader.exec_module(smoke)
    calls = []; dialogs = []; boxes = {1: 1}  # shell A still shows one native failure box
    # T-P3-H2: cleanup closes boxes and asks the exit while the shell itself runs (pid + creation
    # time), not only while its watcher does; a shell known only from root.json (a "late shell"
    # whose launch gave up) is cleaned up too, and one that never exits is reported.
    exited = set()
    stuck = 9
    def close_window(pid):
        calls.append(("close", pid))
        if pid != stuck: exited.add(pid)
    def close_dialogs(pid):
        dialogs.append(pid)
        if boxes.get(pid):
            boxes[pid] -= 1; return 1
        return 0
    alive = []
    def shell_alive(pid, created):
        alive.append((pid, created)); return pid not in exited
    clock = [0.0]
    late_root = tmp_path / "E"  # written by the watcher only after the launch gave up
    def sleep(seconds):
        clock[0] += seconds
        if not late_root.exists(): late_root.write_text('{"pid": 6, "created_filetime": 456}')
    monkeypatch.setattr(smoke, "close_window", close_window)
    monkeypatch.setattr(smoke, "close_dialogs", close_dialogs)
    monkeypatch.setattr(smoke, "shell_alive", shell_alive)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))

    class Process:
        def __init__(self, pid, fails=False): self.pid, self.fails = pid, fails
        def poll(self): return None
        def wait(self, timeout):
            calls.append(("wait", self.pid))
            if self.fails: raise subprocess.TimeoutExpired("watcher", timeout)
            return 0
        def terminate(self): calls.append(("terminate", self.pid))

    class Log:
        def __init__(self, fails=False): self.fails = fails
        def close(self):
            if self.fails: raise OSError("log close failed")

    (tmp_path / "C").write_text('{"pid": 5, "created_filetime": 123}')
    shells = [SimpleNamespace(tag="E", pid=None, process=Process(6), ready=late_root, log=Log()),
              SimpleNamespace(tag="A", pid=1, process=Process(1, True), ready=tmp_path / "A", log=Log(True)),
              SimpleNamespace(tag="B", pid=2, process=Process(2), ready=tmp_path / "B", log=Log()),
              SimpleNamespace(tag="C", pid=None, process=Process(5), ready=tmp_path / "C", log=Log()),
              SimpleNamespace(tag="D", pid=stuck, process=Process(stuck), ready=tmp_path / "D", log=Log())]
    errors = smoke.cleanup(shells, [Process(3, True), Process(4)])
    # Every shell is handled before the helpers, and one failure never stops the rest.
    assert calls == [("close", 6), ("wait", 6), ("close", 1), ("wait", 1), ("close", 2), ("wait", 2), ("close", 5), ("wait", 5),
                     ("close", stuck), ("wait", stuck),
                     ("terminate", 3), ("wait", 3), ("terminate", 4), ("wait", 4)]
    assert dialogs[:4] == [6, 1, 1, 2]  # A's box first, then the exit request
    assert (6, 456) in alive and shells[0].pid == 6  # root.json appeared while cleanup waited
    assert (5, 123) in alive and shells[3].pid == 5  # the late shell, by pid and creation time
    assert [e["object"] for e in errors] == ["A", "A-log", "D", "helper"]
    assert errors[2] == {"object": "D", "error": "shell_still_running", "pid": stuck}
    assert clock[0] >= 45  # the stuck shell got its whole 45 s before it was reported


def test_smoke_close_targets_main_window_only(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "shell/app/src/p3_instance_smoke.py"
    spec = importlib.util.spec_from_file_location("p3_close_smoke", source)
    smoke = importlib.util.module_from_spec(spec); spec.loader.exec_module(smoke)
    posted = []

    class Function:
        def __init__(self, function): self.function = function
        def __call__(self, *args): return self.function(*args)

    def title(hwnd, buffer, size):
        buffer.value = {11: "Tao hidden control", 22: "Tesseract Shell (P3)", 33: "", 44: ""}[hwnd]

    classes = {11: "Tao", 22: "Tao Window", 33: "Other", 44: "TesseractShellTray"}
    def class_name(hwnd, buffer, size):
        buffer.value = classes[hwnd]

    visible = {11: False, 22: True, 33: True, 44: False, 55: True}
    user = SimpleNamespace(GetWindowTextW=Function(title), GetClassNameW=Function(class_name),
        PostMessageW=Function(lambda *args: posted.append(args) or True),
        IsWindowVisible=Function(lambda hwnd: visible[hwnd]))
    monkeypatch.setattr(smoke.C, "WinDLL", lambda *args, **kwargs: user)
    # Since P3-03 a window close hides to the tray: the real exit is the tray's
    # Quit command (WM_COMMAND, CMD_QUIT) posted to the tray window only.
    monkeypatch.setattr(smoke, "windows_for", lambda pid: [11, 22, 33, 44])
    smoke.close_window(123)
    assert posted == [(44, 0x111, 2, 0)]
    # Without a tray window the fallback is WM_CLOSE to the main window only.
    posted.clear()
    clock = iter([0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(smoke, "windows_for", lambda pid: [11, 22, 33])
    smoke.close_window(123)
    assert posted == [(22, 0x10, 0, 0)]
    # T-P3-H: without a tray, a visible native failure box (#32770) is closed first.
    posted.clear()
    classes[55] = "#32770"
    monkeypatch.setattr(smoke, "windows_for", lambda pid: [11, 33, 55])
    assert smoke.close_dialogs(123) == 1
    assert posted == [(55, 0x10, 0, 0)]


@pytest.mark.parametrize("missing", ["TESSERACT_INSTANCE_SCOPE", "TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR"])
def test_smoke_refuses_missing_scope_before_launch(tmp_path, missing):
    script = Path(__file__).resolve().parents[1] / "scripts/p3_single_instance_smoke.ps1"
    env = dict(os.environ, TESSERACT_INSTANCE_SCOPE="p3-refusal",
               TESSERACT_CONFIG_DIR=str(tmp_path), TESSERACT_DATA_DIR=str(tmp_path))
    env.pop(missing)
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode != 0 and b"custom_scope_required" in result.stderr
