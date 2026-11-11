"""P1 instance scope and Win32 ownership contract; no production object names are opened."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

import bridge_instance_scope as scope_api


VECTORS = Path(__file__).resolve().parents[1] / "bridge_schema" / "instance_scope_vectors.json"


def _names(scope: scope_api.InstanceScope) -> dict[str, str]:
    return vars(scope.names)


def _temp_scope(tmp_path: Path, label: str = "dev") -> scope_api.InstanceScope:
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    env = {
        "TESSERACT_INSTANCE_SCOPE": label,
        "TESSERACT_CONFIG_DIR": str(config),
        "TESSERACT_DATA_DIR": str(data),
    }
    return scope_api.resolve_scope(environ=env, config_dir=config, data_dir=data)


def _audit_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    manifest = {
        "schema": 1,
        "audit_id": "0123456789abcdef0123456789abcdef",
        "interface": "Ethernet",
        "interface_index": 1,
        "dns_controller_port": 53001,
        "dns_listener_port": 53002,
        "config_dir": str(config),
        "data_dir": str(data),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "TESSERACT_ISOLATED_AUDIT_MANIFEST": str(path),
        "TESSERACT_CONFIG_DIR": str(config),
        "TESSERACT_DATA_DIR": str(data),
    }


def test_cross_language_name_vectors() -> None:
    vectors = json.loads(VECTORS.read_bytes())
    assert vectors["version"] == 3
    assert vectors["custom_identity"]["domain_prefix"] == "tesseract-instance-scope-v1\0"
    rules = vectors["custom_identity"]["rules"]
    assert "LCMapStringEx" in rules["case_mapping"]
    assert "Win32::Globalization::LCMapStringEx" in rules["rust"]
    for case in vectors["cases"]:
        kind, scope_id = case["kind"], case["id"]
        if kind == "custom":
            assert scope_api.custom_scope_id(case["config_dir"], case["data_dir"]) == scope_id
            if "normalized_config_dir" in case:
                assert scope_api._identity_path(case["config_dir"]) == case["normalized_config_dir"]
                assert scope_api._identity_path(case["data_dir"]) == case["normalized_data_dir"]
        assert vars(scope_api.names_for_scope(kind, scope_id)) == case["names"]
    default = scope_api.resolve_scope(environ={})
    assert _names(default) == vectors["cases"][0]["names"]
    assert default.hello_scope() == {"kind": "default", "id": "default"}
    assert default.may_take_over_host_network


def test_custom_resolves_real_paths_and_label_cannot_bypass_identity(tmp_path: Path) -> None:
    first = _temp_scope(tmp_path)
    config_alias = first.config_dir / ".." / "config"
    env = {
        "TESSERACT_INSTANCE_SCOPE": "another-label",
        "TESSERACT_CONFIG_DIR": str(config_alias),
        "TESSERACT_DATA_DIR": str(first.data_dir),
    }
    second = scope_api.resolve_scope(environ=env)
    assert first == second
    assert first.id == scope_api.custom_scope_id(first.config_dir, first.data_dir)
    assert first.hello_scope() == {"kind": "custom", "id": first.id}
    assert not first.may_take_over_host_network


@pytest.mark.parametrize("bad_field,bad_value,code", [
    ("config", "missing", "directory_invalid"),
    ("data", "missing", "directory_invalid"),
    ("config", "relative", "directory_relative"),
    ("data", "file", "directory_not_dir"),
])
def test_custom_rejects_invalid_directories(tmp_path: Path, bad_field: str,
                                            bad_value: str, code: str) -> None:
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    file_path = tmp_path / "regular-file"
    file_path.write_bytes(b"x")
    replacement = {"missing": tmp_path / "absent", "relative": Path("relative"),
                   "file": file_path}[bad_value]
    env = {
        "TESSERACT_INSTANCE_SCOPE": "dev",
        "TESSERACT_CONFIG_DIR": str(replacement if bad_field == "config" else config),
        "TESSERACT_DATA_DIR": str(replacement if bad_field == "data" else data),
    }
    with pytest.raises(scope_api.ScopeError) as exc:
        scope_api.resolve_scope(environ=env)
    assert exc.value.code == code


def test_custom_rejects_unwritable_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = scope_api.tempfile.NamedTemporaryFile

    def denied(*args: object, **kwargs: object) -> object:
        if Path(kwargs["dir"]).name.casefold() == "data":
            raise PermissionError(5, "simulated ACL denial")
        return original(*args, **kwargs)

    monkeypatch.setattr(scope_api.tempfile, "NamedTemporaryFile", denied)
    with pytest.raises(scope_api.ScopeError) as exc:
        _temp_scope(tmp_path)
    assert exc.value.code == "directory_not_writable"


def test_custom_requires_both_overrides_and_excludes_default_paths(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(scope_api.ScopeError, match="both directory overrides") as exc:
        scope_api.resolve_scope(environ={"TESSERACT_INSTANCE_SCOPE": "dev",
                                         "TESSERACT_CONFIG_DIR": str(config)})
    assert exc.value.code == "directory_override_required"
    roaming = tmp_path / "roaming"
    normal_config = roaming / "Tesseract"
    data = tmp_path / "data"
    normal_config.mkdir(parents=True)
    data.mkdir()
    env = {"APPDATA": str(roaming), "TESSERACT_INSTANCE_SCOPE": "dev",
           "TESSERACT_CONFIG_DIR": str(normal_config), "TESSERACT_DATA_DIR": str(data)}
    with pytest.raises(scope_api.ScopeError) as exc:
        scope_api.resolve_scope(environ=env)
    assert exc.value.code == "default_directory_forbidden"


def test_audit_manifest_and_directory_match(tmp_path: Path) -> None:
    env = _audit_env(tmp_path)
    result = scope_api.resolve_scope(environ=env)
    assert result.kind == "audit"
    assert result.id == "0123456789abcdef0123456789abcdef"
    assert result.names.legacy_mutex.endswith("-instance")
    assert not result.may_take_over_host_network
    assert result.hello_scope() == {"kind": "audit", "id": result.id}
    env["TESSERACT_INSTANCE_SCOPE"] = "ignored-under-audit"
    assert scope_api.resolve_scope(environ=env) == result
    other = tmp_path / "other"
    other.mkdir()
    env["TESSERACT_DATA_DIR"] = str(other)
    with pytest.raises(scope_api.ScopeError) as exc:
        scope_api.resolve_scope(environ=env)
    assert exc.value.code == "audit_directory_mismatch"
    manifest_path = Path(env["TESSERACT_ISOLATED_AUDIT_MANIFEST"])
    manifest = json.loads(manifest_path.read_bytes())
    manifest["data_dir"] = str(other)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(scope_api.ScopeError) as exc:
        scope_api.resolve_scope(environ=env)
    assert exc.value.code == "audit_directory_mismatch"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended-path identity")
def test_extended_path_alias_uses_same_identity(tmp_path: Path) -> None:
    result = _temp_scope(tmp_path)
    prefix = "\\\\?\\"
    env = {
        "TESSERACT_INSTANCE_SCOPE": "different-label",
        "TESSERACT_CONFIG_DIR": prefix + str(result.config_dir),
        "TESSERACT_DATA_DIR": prefix + str(result.data_dir),
    }
    alias = scope_api.resolve_scope(environ=env)
    assert alias.id == result.id
    assert alias.names == result.names
    assert alias.config_dir == result.config_dir
    assert alias.data_dir == result.data_dir


def test_bad_audit_never_falls_back_to_default(tmp_path: Path) -> None:
    env = _audit_env(tmp_path)
    manifest = Path(env["TESSERACT_ISOLATED_AUDIT_MANIFEST"])
    value = json.loads(manifest.read_bytes())
    value["audit_id"] = "not-an-id"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(scope_api.ScopeError) as exc:
        scope_api.resolve_scope(environ=env)
    assert exc.value.code == "audit_id_invalid"
    env["TESSERACT_ISOLATED_AUDIT_MANIFEST"] = "relative-manifest.json"
    with pytest.raises(scope_api.ScopeError) as exc:
        scope_api.resolve_scope(environ=env)
    assert exc.value.code == "audit_manifest_invalid"


def test_result_is_frozen(tmp_path: Path) -> None:
    result = scope_api.AcquisitionResult("error", "test", _temp_scope(tmp_path))
    with pytest.raises(FrozenInstanceError):
        result.status = "acquired"


@dataclass
class _FakeObject:
    name: str
    owner: int | None = None
    depth: int = 0


class FakeWin32:
    def __init__(self) -> None:
        self.objects: dict[str, _FakeObject] = {}
        self.handles: dict[int, _FakeObject] = {}
        self.next_handle = 1
        self.create_errors: dict[str, int] = {}
        self.success_create_errors: dict[str, int] = {}
        self.wait_error: int | None = None
        self.close_error: int | None = None
        self.abandon_next = False

    def create_mutex(self, name: str) -> tuple[int | None, int]:
        if name in self.create_errors:
            return None, self.create_errors[name]
        key = name.casefold()
        existed = key in self.objects
        obj = self.objects.setdefault(key, _FakeObject(name))
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = obj
        return handle, self.success_create_errors.get(
            name, scope_api.ERROR_ALREADY_EXISTS if existed else 0)

    def wait(self, handle: int, _timeout_ms: int) -> tuple[int, int]:
        if self.wait_error is not None:
            return scope_api.WAIT_FAILED, self.wait_error
        obj = self.handles[handle]
        current = threading.get_ident()
        if obj.owner not in (None, current):
            return scope_api.WAIT_TIMEOUT, 0
        obj.owner = current
        obj.depth += 1
        if self.abandon_next:
            self.abandon_next = False
            return scope_api.WAIT_ABANDONED, 0
        return scope_api.WAIT_OBJECT_0, 0

    def release(self, handle: int) -> tuple[bool, int]:
        obj = self.handles[handle]
        if obj.owner != threading.get_ident():
            return False, 288
        obj.depth -= 1
        if not obj.depth:
            obj.owner = None
        return True, 0

    def close(self, handle: int) -> tuple[bool, int]:
        if self.close_error is not None:
            return False, self.close_error
        obj = self.handles.pop(handle)
        if obj not in self.handles.values() and obj.owner is None:
            self.objects.pop(obj.name.casefold(), None)
        return True, 0


def _unique_scope() -> scope_api.InstanceScope:
    token = uuid.uuid4().hex
    prefix = f"Local\\TesseractScopeTest-{token}"
    names = scope_api.InstanceNames(prefix + "-legacy", prefix + "-restore",
                                     prefix + "-runtime", prefix + "-shell", prefix + "-quitting")
    return scope_api.InstanceScope("custom", token[:24], names, False)


def test_fake_legacy_gate_and_unowned_runtime_object() -> None:
    api = FakeWin32()
    scope = _unique_scope()
    runtime_handle, _ = api.create_mutex(scope.names.runtime_mutex)
    gate_handle, _ = api.create_mutex(scope.names.legacy_mutex)
    result = scope_api.acquire_instance(scope, win32=api)
    assert (result.status, result.reason_code) == ("legacy_instance_present", "legacy_gate_exists")
    assert result.lease is None
    assert len(api.handles) == 2
    api.close(gate_handle)
    acquired = scope_api.acquire_instance(scope, win32=api)
    assert acquired.status == "acquired"
    assert not acquired.may_take_over_host_network
    acquired.lease.close()
    acquired.lease.close()
    api.close(runtime_handle)
    assert not api.handles


def test_fake_abandoned_mutex_and_shell_death_cleanup_window() -> None:
    api = FakeWin32()
    scope = _unique_scope()
    api.abandon_next = True
    acquired = scope_api.acquire_instance(scope, win32=api)
    assert (acquired.status, acquired.reason_code) == ("acquired", "acquired_after_abandonment")
    shell_handle, _ = api.create_mutex(scope.names.legacy_mutex)
    api.close(shell_handle)  # shell died; sidecar still owns runtime and keeps the old gate
    old_qt_handle, gate_error = api.create_mutex(scope.names.legacy_mutex)
    assert gate_error == scope_api.ERROR_ALREADY_EXISTS
    api.close(old_qt_handle)
    second = scope_api.acquire_instance(scope, win32=api)
    assert second.status == "already_running"
    acquired.lease.close()
    restarted = scope_api.acquire_instance(scope, win32=api)
    assert restarted.status == "acquired"
    restarted.lease.close()


@pytest.mark.parametrize("which,error,status,reason", [
    ("runtime", 5, "access_denied", "object_access_denied"),
    ("runtime", 87, "error", "runtime_create_failed"),
    ("legacy", 5, "access_denied", "object_access_denied"),
    ("legacy", 87, "error", "legacy_create_failed"),
])
def test_fake_create_errors_fail_closed(which: str, error: int, status: str, reason: str) -> None:
    api = FakeWin32()
    scope = _unique_scope()
    name = scope.names.runtime_mutex if which == "runtime" else scope.names.legacy_mutex
    api.create_errors[name] = error
    result = scope_api.acquire_instance(scope, win32=api)
    assert (result.status, result.reason_code, result.win32_error) == (status, reason, error)
    assert not api.handles


@pytest.mark.parametrize("error,status,reason", [
    (5, "access_denied", "object_access_denied"),
    (87, "error", "runtime_wait_failed"),
])
def test_fake_wait_errors_fail_closed(error: int, status: str, reason: str) -> None:
    api = FakeWin32()
    api.wait_error = error
    result = scope_api.acquire_instance(_unique_scope(), win32=api)
    assert (result.status, result.reason_code, result.win32_error) == (status, reason, error)
    assert not api.handles


def test_fake_close_failure_returns_retryable_cleanup_handle() -> None:
    api = FakeWin32()
    api.wait_error = 87
    api.close_error = 6
    result = scope_api.acquire_instance(_unique_scope(), win32=api)
    assert (result.status, result.reason_code, result.win32_error) == (
        "error", "runtime_close_failed", 6)
    assert result.lease is not None and not result.lease.closed
    api.close_error = None
    result.lease.close()
    assert not api.handles


def test_fake_unexpected_success_error_refuses_start() -> None:
    api = FakeWin32()
    scope = _unique_scope()
    api.success_create_errors[scope.names.runtime_mutex] = 87
    result = scope_api.acquire_instance(scope, win32=api)
    assert (result.status, result.reason_code, result.win32_error) == (
        "error", "runtime_create_unexpected_error", 87)
    assert not api.handles


def test_lease_requires_same_thread() -> None:
    acquired = scope_api.acquire_instance(_unique_scope(), win32=FakeWin32())
    assert acquired.status == "acquired"
    errors: list[Exception] = []

    def wrong_thread() -> None:
        try:
            acquired.lease.close()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join(timeout=5)
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert not acquired.lease.closed
    acquired.lease.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 named mutexes")
def test_real_win32_legacy_gate_and_release(tmp_path: Path, monkeypatch) -> None:
    api = scope_api.Win32MutexAPI()
    scope = _temp_scope(tmp_path, uuid.uuid4().hex)
    for key, value in (("TESSERACT_INSTANCE_SCOPE", scope.id),
                       ("TESSERACT_CONFIG_DIR", str(scope.config_dir)),
                       ("TESSERACT_DATA_DIR", str(scope.data_dir))):
        monkeypatch.setenv(key, value)
    gate, error = api.create_mutex(scope.names.legacy_mutex)
    assert gate and error == 0
    try:
        blocked = scope_api.acquire_instance(scope, win32=api)
        assert blocked.status == "legacy_instance_present"
    finally:
        assert api.close(gate)[0]
    acquired = scope_api.acquire_instance(scope, win32=api)
    assert acquired.status == "acquired"
    gate_check, error = api.create_mutex(scope.names.legacy_mutex)
    assert gate_check and error == scope_api.ERROR_ALREADY_EXISTS
    api.close(gate_check)

    first_wait_done = threading.Event()
    released = threading.Event()
    waiter_statuses: list[int] = []
    waiter_error: list[Exception] = []

    def independent_waiter() -> None:
        runtime_check = None
        try:
            runtime_check, create_error = api.create_mutex(scope.names.runtime_mutex)
            assert runtime_check and create_error == scope_api.ERROR_ALREADY_EXISTS
            waiter_statuses.append(api.wait(runtime_check, 0)[0])
            first_wait_done.set()
            assert released.wait(5)
            waiter_statuses.append(api.wait(runtime_check, 5000)[0])
            assert api.release(runtime_check)[0]
        except Exception as exc:
            waiter_error.append(exc)
            first_wait_done.set()
        finally:
            if runtime_check:
                api.close(runtime_check)

    waiter = threading.Thread(target=independent_waiter)
    waiter.start()
    assert first_wait_done.wait(5)
    assert not waiter_error
    assert waiter_statuses == [scope_api.WAIT_TIMEOUT]
    acquired.lease.close()
    released.set()
    waiter.join(timeout=5)
    assert not waiter.is_alive() and not waiter_error
    assert waiter_statuses == [scope_api.WAIT_TIMEOUT, scope_api.WAIT_OBJECT_0]
    reacquired = scope_api.acquire_instance(scope, win32=api)
    assert reacquired.status == "acquired"
    reacquired.lease.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 named mutexes")
def test_real_win32_runtime_owned_on_another_thread(tmp_path: Path, monkeypatch) -> None:
    api = scope_api.Win32MutexAPI()
    scope = _temp_scope(tmp_path, uuid.uuid4().hex)
    for key, value in (("TESSERACT_INSTANCE_SCOPE", scope.id),
                       ("TESSERACT_CONFIG_DIR", str(scope.config_dir)),
                       ("TESSERACT_DATA_DIR", str(scope.data_dir))):
        monkeypatch.setenv(key, value)
    ready = threading.Event()
    stop = threading.Event()
    worker_error: list[Exception] = []

    def owner() -> None:
        handle = None
        try:
            handle, error = api.create_mutex(scope.names.runtime_mutex)
            assert handle and error == 0
            assert api.wait(handle, 0)[0] == scope_api.WAIT_OBJECT_0
            ready.set()
            assert stop.wait(5)
            assert api.release(handle)[0]
        except Exception as exc:
            worker_error.append(exc)
            ready.set()
        finally:
            if handle:
                api.close(handle)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert ready.wait(5)
        assert not worker_error
        blocked = scope_api.acquire_instance(scope, win32=api)
        assert (blocked.status, blocked.reason_code) == ("already_running", "runtime_owned")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not worker_error
    acquired = scope_api.acquire_instance(scope, win32=api)
    assert acquired.status == "acquired"
    acquired.lease.close()
