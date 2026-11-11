"""RT-7 unit coverage: synthetic layouts and fake winreg only."""
import json
import os
import sys
from pathlib import Path

import pytest

import autostart
from bridge_protocol import validate_frame
from bridge_autostart import autostart_state, sidecar_autostart_manager


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 3

    def __init__(self):
        self.value = None
        self.kind = self.REG_SZ
        self.reads, self.writes = [], []
        self.read_error = self.write_error = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def OpenKey(self, root, path, reserved, access):
        assert root is self.HKEY_CURRENT_USER and path == autostart.REG_KEY_PATH
        if access == self.KEY_READ:
            self.reads.append(("open", access))
            if self.read_error:
                raise self.read_error
        else:
            self.writes.append(("open", access))
            if self.write_error:
                raise self.write_error
        return self

    def QueryValueEx(self, key, name):
        assert key is self and name == autostart.DEFAULT_REG_VALUE_NAME
        self.reads.append(("query", name))
        if self.value is None:
            raise FileNotFoundError("synthetic missing value")
        return self.value, self.kind

    def CreateKeyEx(self, root, path, reserved, access):
        assert root is self.HKEY_CURRENT_USER and path == autostart.REG_KEY_PATH
        assert access == self.KEY_SET_VALUE
        self.writes.append(("create", access))
        if self.write_error:
            raise self.write_error
        return self

    def SetValueEx(self, key, name, reserved, kind, command):
        assert key is self and name == autostart.DEFAULT_REG_VALUE_NAME
        self.writes.append(("set", command))
        if self.write_error:
            raise self.write_error
        self.value, self.kind = command, kind

    def DeleteValue(self, key, name):
        assert key is self and name == autostart.DEFAULT_REG_VALUE_NAME
        self.writes.append(("delete", name))
        self.value = None


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    registry = FakeWinreg()
    monkeypatch.setattr(autostart, "_import_winreg", lambda: registry)
    return registry


@pytest.fixture
def layout(tmp_path, monkeypatch):
    directory = tmp_path / "Program Files 中文"
    directory.mkdir()
    sidecar = directory / "tesseract-runtime.exe"
    sidecar.touch()
    monkeypatch.setattr(sys, "executable", str(sidecar))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return directory, sidecar


def make_shell(layout, name="Tesseract.exe"):
    shell = layout[0] / name
    shell.touch()
    return shell


@pytest.mark.parametrize("name", ["Tesseract.exe", "tesseract-shell.exe"])
def test_frozen_sidecar_registers_quoted_shell_with_autostart(layout, registry, name):
    shell = make_shell(layout, name)
    manager = sidecar_autostart_manager()
    assert manager.supported
    assert manager.launch_command() == f'"{shell}" --autostart'
    result = manager.set_enabled(True)
    assert result["enabled"] and result["error"] is None
    assert registry.value == f'"{shell}" --autostart'
    assert str(layout[1]) not in registry.value
    registry.writes.clear()
    assert manager.status() == {
        "supported": True, "enabled": True, "target": "shell",
        "mismatch": False, "error": None,
    }
    assert not registry.writes


def test_source_mode_never_registers_python_even_if_shell_exists(layout, registry, monkeypatch):
    shell = make_shell(layout)
    interpreter = layout[0] / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "frozen", False)
    monkeypatch.setattr(sys, "argv", [str(layout[0] / "runtime_main.pyw")])
    manager = sidecar_autostart_manager(shell_executable=shell)
    assert not manager.supported and not manager.launch_command()
    assert manager.enable()["error"] == "development_mode"
    state = autostart_state(manager)
    assert not state["supported"] and state["target"] == "none"
    assert state["error"]["message_key"] == "errors.autostart.development_mode"
    assert not registry.writes


def test_missing_shell_does_not_fall_back_to_sidecar(layout, registry):
    manager = sidecar_autostart_manager()
    assert not manager.supported and not manager.launch_command()
    assert manager.enable()["error"] == "shell_missing"
    assert manager.status()["error"]["message_key"] == "errors.autostart.shell_missing"
    assert not registry.writes


@pytest.mark.parametrize("bad_path", ["different_directory", "wrong_name", "relative", "sidecar", "directory"])
def test_bootstrap_rejects_untrusted_shell_paths(layout, registry, bad_path):
    shell = make_shell(layout)
    if bad_path == "different_directory":
        shell = layout[0].parent / "Tesseract.exe"
        shell.touch()
    elif bad_path == "wrong_name":
        shell = layout[0] / "arbitrary.exe"
        shell.touch()
    elif bad_path == "relative":
        shell = Path("Tesseract.exe")
    elif bad_path == "sidecar":
        shell = layout[1]
    else:
        shell.unlink()
        shell.mkdir()
    manager = sidecar_autostart_manager(shell_executable=shell)
    assert not manager.supported
    assert manager.enable()["error"] == "shell_path_invalid"
    assert not registry.writes


def test_hardlink_to_sidecar_is_rejected(layout, registry):
    shell = layout[0] / "Tesseract.exe"
    os.link(layout[1], shell)
    assert sidecar_autostart_manager().enable()["error"] == "shell_path_invalid"
    assert not registry.writes


def test_shell_removed_after_bootstrap_cannot_be_registered(layout, registry):
    shell = make_shell(layout)
    manager = sidecar_autostart_manager()
    shell.unlink()
    assert not manager.supported
    assert manager.enable()["error"] == "shell_missing"
    assert not registry.writes


def test_default_layout_prefers_release_shell_and_ignores_environment(layout, registry, monkeypatch):
    release = make_shell(layout)
    make_shell(layout, "tesseract-shell.exe")
    monkeypatch.setenv("TESSERACT_SHELL_EXE", str(layout[0].parent / "arbitrary.exe"))
    assert sidecar_autostart_manager().enable()["command"] == f'"{release}" --autostart'


@pytest.mark.parametrize("registration,target,mismatch", [
    ("shell", "shell", False),
    ("shell_no_flag", "shell", True),
    ("shell_wrong_flag", "shell", True),
    ("sidecar", "other", True),
    ("old_qt", "legacy_qt", True),
    ("old_dev_shell", "other", True),
    ("other", "other", True),
    ("malformed", "other", True),
    ("unquoted_spaces", "other", True),
    ("missing", "none", False),
    ("empty", "none", False),
])
def test_target_classification_and_query_is_read_only(layout, registry, registration, target, mismatch):
    shell = make_shell(layout)
    commands = {
        "shell": f'"{shell}" --autostart',
        "shell_no_flag": f'"{shell}"',
        "shell_wrong_flag": f'"{shell}" --autostart --extra',
        "sidecar": f'"{layout[1]}"',
        "old_qt": f'"{layout[0].parent / "Old Qt" / "Tesseract.exe"}"',
        "old_dev_shell": f'"{layout[0].parent / "Old Shell" / "tesseract-shell.exe"}" --autostart',
        "other": '"C:\\other\\utility.exe" --autostart',
        "malformed": '"unterminated command',
        "unquoted_spaces": f'{shell} --autostart',
        "missing": None, "empty": "",
    }
    registry.value = commands[registration]
    manager = sidecar_autostart_manager()
    for read in (lambda: autostart_state(manager), manager.status):
        state = read()
        assert state == {"supported": True, "enabled": bool(registry.value),
                         "target": target, "mismatch": mismatch, "error": None}
        assert set(state) == {"supported", "enabled", "target", "mismatch", "error"}
        assert str(layout[0]) not in json.dumps(state, ensure_ascii=False)
    assert registry.value == commands[registration]
    assert not registry.writes
    assert registry.reads == [("open", registry.KEY_READ),
                              ("query", autostart.DEFAULT_REG_VALUE_NAME)] * 2


def test_case_and_separator_variations_identify_same_verified_shell(layout, registry):
    shell = make_shell(layout)
    registry.value = f'"{str(shell).upper().replace(chr(92), "/")}" --autostart'
    assert sidecar_autostart_manager().status()["target"] == "shell"
    assert not registry.writes


def test_missing_shell_still_reports_old_sidecar_registration(layout, registry):
    registry.value = f'"{layout[1]}"'
    state = sidecar_autostart_manager().status()
    assert not state["supported"] and state["enabled"]
    assert state["target"] == "other" and state["mismatch"]
    assert state["error"]["message_key"] == "errors.autostart.shell_missing"
    assert not registry.writes


def test_query_read_error_is_safe_and_never_repaired(layout, registry):
    make_shell(layout)
    registry.read_error = PermissionError("C:/Private/RegistrySecret")
    state = sidecar_autostart_manager().status()
    assert state["error"] == {"code": "io", "message_key": "errors.autostart.read_failed", "params": {}}
    assert "RegistrySecret" not in json.dumps(state)
    assert not registry.writes


def test_non_string_registration_is_reported_without_paths(layout, registry):
    make_shell(layout)
    registry.value, registry.kind = 123, 99
    state = sidecar_autostart_manager().status()
    assert state["target"] == "other" and state["mismatch"]
    assert state["error"]["message_key"] == "errors.autostart.registration_invalid"
    assert not registry.writes


def test_registry_unavailable_is_unsupported(layout, registry, monkeypatch):
    make_shell(layout)
    monkeypatch.setattr(autostart, "_import_winreg", lambda: None)
    manager = sidecar_autostart_manager()
    assert not manager.supported
    assert not manager.enable()["supported"]
    assert manager.status()["error"]["message_key"] == "errors.autostart.registry_unavailable"
    assert not registry.writes


def test_write_failure_preserves_registration_until_explicit_settings_action(layout, registry):
    shell = make_shell(layout)
    registry.value = f'"{layout[1]}"'
    registry.write_error = PermissionError("synthetic write denied")
    manager = sidecar_autostart_manager()
    result = manager.enable()
    assert result["supported"] and not result["enabled"]
    assert result["error"] == "synthetic write denied"
    assert registry.value == f'"{layout[1]}"'
    assert manager.status()["mismatch"]
    registry.write_error = None
    assert manager.set_enabled(True)["enabled"]
    assert registry.value == f'"{shell}" --autostart'
    assert not manager.status()["mismatch"]
    assert not manager.set_enabled(False)["enabled"]
    assert manager.status()["target"] == "none"


def test_qt_frozen_default_factory_behavior_is_unchanged(layout, registry, monkeypatch):
    qt = make_shell(layout)
    monkeypatch.setattr(sys, "executable", str(qt))
    manager = autostart.default_autostart_manager()
    assert manager.launch_target == "legacy_qt"
    assert manager.launch_command() == f'"{qt}"'
    assert manager.enable()["command"] == f'"{qt}"'
    # The Qt executable must not validate itself as a sidecar's shell.
    assert sidecar_autostart_manager().enable()["error"] == "shell_path_invalid"


def test_qt_source_default_factory_still_launches_interpreter_and_script(tmp_path, monkeypatch):
    script = tmp_path / "desktop_app.pyw"
    script.touch()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])
    manager = autostart.default_autostart_manager()
    assert manager.launch_command() == f'"{sys.executable}" "{script}"'
    assert "--autostart" not in manager.launch_command()


def test_explicit_shell_target_never_appends_source_script(tmp_path, monkeypatch):
    script = tmp_path / "runtime_main.pyw"
    script.touch()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])
    manager = autostart.shell_autostart_manager("C:/Program Files/Tesseract/Tesseract.exe")
    assert manager.launch_command() == '"C:/Program Files/Tesseract/Tesseract.exe" --autostart'


def test_invalid_launch_target_and_empty_shell_are_rejected():
    with pytest.raises(ValueError):
        autostart.AutostartManager(launch_target="sidecar")
    with pytest.raises(ValueError):
        autostart.shell_autostart_manager("")


@pytest.mark.parametrize("condition", ["shell", "missing", "source", "registry_unavailable", "read_failed"])
def test_state_response_obeys_frozen_bridge_protocol(layout, registry, monkeypatch, condition):
    if condition != "missing":
        shell = make_shell(layout)
        registry.value = f'"{shell}" --autostart'
    if condition == "source":
        monkeypatch.setattr(sys, "frozen", False)
    elif condition == "registry_unavailable":
        monkeypatch.setattr(autostart, "_import_winreg", lambda: None)
    elif condition == "read_failed":
        registry.read_error = PermissionError("synthetic registry read denied")
    state = sidecar_autostart_manager().status()
    frame = {
        "t": "res", "epoch": "synthetic-p2-28",
        "produced_at_ms": 1, "sent_at_ms": 2,
        "id": 1, "req_id": "autostart-query", "owner": "window-1",
        "cmd": "autostart.state", "ok": True,
        "data": state,
    }
    assert not validate_frame(frame)
    assert not registry.writes
