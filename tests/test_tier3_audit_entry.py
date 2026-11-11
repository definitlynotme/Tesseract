from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import runpy
import socket
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.tier3_audit_entry import IsolatedAuditGuard, read_manifest
from scripts.tier3_audit_import_hooks import AuditImportHooks


def _manifest(tmp_path):
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    payload = dict(schema=1, audit_id='a' * 32, interface='physical', interface_index=1,
                   dns_controller_port=19110, dns_listener_port=19111,
                   config_dir=str(config), data_dir=str(data))
    path = tmp_path / 'audit.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    environment = dict(TESSERACT_CONFIG_DIR=str(config), TESSERACT_DATA_DIR=str(data))
    return path, payload, environment


def test_manifest_binds_one_explicit_isolated_config_and_data_root(tmp_path):
    path, payload, environment = _manifest(tmp_path)
    result = read_manifest(path, environment)
    assert result['audit_id'] == payload['audit_id']
    assert len(result['manifest_sha256']) == 64


@pytest.mark.parametrize('field,value', [('schema', 2), ('schema', True), ('audit_id', 'invalid'),
    ('interface', ''), ('interface_index', True), ('interface_index', 0),
    ('dns_listener_port', 19110), ('dns_listener_port', True),
    ('change_validator_timeout', 6)])
def test_audit_manifest_cannot_silently_weaken_isolation_or_change_policy(tmp_path, field, value):
    path, payload, environment = _manifest(tmp_path)
    payload[field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        read_manifest(path, environment)


@pytest.mark.parametrize('field', ['TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR'])
def test_audit_manifest_cannot_fall_back_to_real_user_directories(tmp_path, field):
    path, payload, environment = _manifest(tmp_path)
    environment.pop(field)
    with pytest.raises(ValueError, match='dedicated'):
        read_manifest(path, environment)


def test_inactive_runtime_hook_does_not_import_audit_stack(monkeypatch):
    monkeypatch.delenv('TESSERACT_ISOLATED_AUDIT_MANIFEST', raising=False)
    import builtins
    original = builtins.__import__

    def forbidden(name, *args, **kwargs):
        assert not name.startswith('scripts.tier3_audit')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', forbidden)
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')


def test_lazy_hooks_do_not_import_target_before_its_normal_import(monkeypatch, tmp_path):
    name = 'audit_test_lazy_dependency'
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / (name + '.py')).write_text('value = 1\n')
    visited = []
    hooks = AuditImportHooks({name: lambda module: visited.append(module.value)})
    try:
        hooks.install()
        assert name not in sys.modules
        assert visited == []
        module = importlib.import_module(name)
        assert module.value == 1 and visited == [1]
        assert importlib.import_module(name) is module
        assert visited == [1]
    finally:
        hooks.remove()
        sys.modules.pop(name, None)


def test_lazy_hook_uses_existing_custom_frozen_loader_not_filesystem(monkeypatch):
    name = 'audit_test_frozen_dependency'
    events = []

    class Loader:
        def create_module(self, spec):
            events.append('create')
        def exec_module(self, module):
            events.append('execute')
            module.value = 42

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == name:
                return importlib.util.spec_from_loader(name, Loader())

    monkeypatch.setattr(sys, 'meta_path', [Finder(), *sys.meta_path])
    hooks = AuditImportHooks({name: lambda module: events.append(module.value)})
    try:
        hooks.install()
        importlib.import_module(name)
        assert events == ['create', 'execute', 42]
    finally:
        hooks.remove()
        sys.modules.pop(name, None)


def test_instrumentation_error_aborts_import_instead_of_running_unguarded(monkeypatch):
    name = 'audit_test_existing_dependency'
    monkeypatch.setitem(sys.modules, name, ModuleType(name))
    callback = Mock(side_effect=PermissionError('isolation failed'))
    hooks = AuditImportHooks({name: callback})
    try:
        with pytest.raises(PermissionError):
            hooks.install()
        assert name not in hooks.applied
    finally:
        hooks.remove()


def _guard(tmp_path):
    path, payload, environment = _manifest(tmp_path)
    return IsolatedAuditGuard(read_manifest(path, environment))


def test_audit_window_identity_uses_own_real_mutex_not_unconditional_success(tmp_path):
    guard = _guard(tmp_path)
    shell = SimpleNamespace(acquire_single_instance_lock=Mock(return_value=False),
                            create_single_instance_restore_event=Mock(return_value=True),
                            signal_existing_instance=Mock(return_value=False))
    originals = dict(vars(shell))
    guard._isolate_window_identity(shell)
    try:
        assert shell.acquire_single_instance_lock() is False
        assert shell.create_single_instance_restore_event() is True
        assert shell.signal_existing_instance() is False
        originals['acquire_single_instance_lock'].assert_called_once_with(
            'Local\\TesseractAudit-' + 'a' * 32 + '-instance')
        originals['signal_existing_instance'].assert_called_once_with(
            'Local\\TesseractAudit-' + 'a' * 32 + '-restore')
    finally:
        guard.uninstall_for_test()
    assert vars(shell) == originals


def test_shared_window_hook_records_owner_before_real_audit_start(tmp_path, monkeypatch):
    guard = _guard(tmp_path)
    shell = SimpleNamespace(acquire_single_instance_lock=Mock(),
                            create_single_instance_restore_event=Mock(), signal_existing_instance=Mock())
    monkeypatch.setitem(sys.modules, 'desktop_app_shell', shell)
    result = object()
    class Window:
        def start_startup_window_audit(self, path, **kwargs):
            marker = json.loads(Path(guard.manifest['data_dir'], 'window-start-control.json').read_text())
            assert marker['process_id'] == os.getpid()
            assert marker['manifest_sha256'] == guard.manifest['manifest_sha256']
            assert path == 'audit.json' and kwargs == {'duration_ms': 500}
            return result
    desktop = SimpleNamespace(ProxyDesktopWindow=Window, APP_UPDATE_ENABLED=True,
                              acquire_single_instance_lock=Mock(), create_single_instance_restore_event=Mock(),
                              signal_existing_instance=Mock())
    guard._observe_window_start(desktop)
    try:
        assert Window().start_startup_window_audit('audit.json', duration_ms=500) is result
        assert desktop.acquire_single_instance_lock is shell.acquire_single_instance_lock
        assert desktop.APP_UPDATE_ENABLED is False
    finally:
        guard.uninstall_for_test()
    assert desktop.APP_UPDATE_ENABLED is True


def test_auxiliary_network_suppression_is_shared_opt_in_and_reversible(tmp_path):
    import urllib.request
    guard = _guard(tmp_path)
    app = SimpleNamespace(APP_UPDATE_ENABLED=True)
    class Alerts:
        def _configured_alert_channels(self):
            return ('example-channel',)
    old_opener = urllib.request._opener
    guard._disable_update_checks(app)
    guard._disable_alert_delivery(SimpleNamespace(AlertManager=Alerts))
    guard._disable_system_proxy_discovery(urllib.request)
    try:
        assert app.APP_UPDATE_ENABLED is False
        assert Alerts()._configured_alert_channels() == ()
        assert not any(isinstance(handler, urllib.request.ProxyHandler)
                       for handler in urllib.request._opener.handlers)
    finally:
        guard.uninstall_for_test()
    assert app.APP_UPDATE_ENABLED is True
    assert Alerts()._configured_alert_channels() == ('example-channel',)
    assert urllib.request._opener is old_opener


def test_partial_instrumentation_cannot_claim_socket_isolation(tmp_path):
    guard = _guard(tmp_path)
    app = SimpleNamespace(APP_UPDATE_ENABLED=True)
    guard._disable_update_checks(app)
    try:
        assert not guard.socket_guard_installed()
    finally:
        guard.uninstall_for_test()


@pytest.mark.parametrize('locked_attempts', [2, 4])
def test_receipt_replace_tolerates_only_bounded_transient_locks(tmp_path, monkeypatch, locked_attempts):
    import atomic_file
    guard = _guard(tmp_path)
    guard.publish()
    destination = Path(guard.manifest['data_dir'], f'physical-config-control-{os.getpid()}.json')
    previous = destination.read_bytes()
    replace = os.replace
    calls = []
    def locked(source, target):
        calls.append((source, target))
        if len(calls) <= locked_attempts:
            raise PermissionError('controlled transient file lock')
        return replace(source, target)
    monkeypatch.setattr(atomic_file.os, 'replace', locked)
    monkeypatch.setattr(atomic_file.time, 'sleep', lambda _delay: None)
    guard.receipt['rendered'] = 5
    if locked_attempts == 4:
        with pytest.raises(PermissionError):
            guard.publish()
        assert len(calls) == 4
        assert destination.read_bytes() == previous
    else:
        guard.publish()
        assert len(calls) == 3
        assert json.loads(destination.read_text())['rendered'] == 5


def test_render_instrumentation_changes_only_audit_copy_routing_and_dns(tmp_path):
    guard = _guard(tmp_path)
    original = dict(mode='rule', proxies=[dict(type='ss', name='fixture', server='192.0.2.1', port=443)],
                    rules=['MATCH,fixture'], tun={'enable': False}, dns={'nameserver': ['system']})
    import copy
    renderer = SimpleNamespace(build_single_proxy_validation_config=lambda: copy.deepcopy(original))
    guard._install_renderers(renderer)
    try:
        result = renderer.build_single_proxy_validation_config()
        assert result['proxies'] == original['proxies'] and result['rules'] == original['rules']
        assert result['interface-name'] == 'physical'
        assert result['dns']['nameserver'] == ['127.0.0.1:19111']
        assert original['dns']['nameserver'] == ['system']
        assert guard.receipt['rendered'] == 1
    finally:
        guard.uninstall_for_test()


def test_audit_does_not_silently_turn_a_capture_config_into_a_valid_run(tmp_path):
    guard = _guard(tmp_path)
    renderer = SimpleNamespace(build_full_clash_config=lambda: {'tun': {'enable': True}})
    guard._install_renderers(renderer)
    try:
        with pytest.raises(ValueError, match='TUN capture'):
            renderer.build_full_clash_config()
        assert guard.receipt['rendered'] == 0
    finally:
        guard.uninstall_for_test()


def test_received_helper_config_must_be_pinned_before_popen(tmp_path):
    guard = _guard(tmp_path)
    started = []
    class Prewarm:
        def __init__(self, payload, send):
            started.append(payload)
        def close(self):
            pass
    guard._install_prewarm(SimpleNamespace(ValidatorPrewarmProcess=Prewarm))
    try:
        with pytest.raises(ValueError):
            Prewarm({'prewarm_config': {}}, lambda *_args: None)
        assert not started and guard.receipt['received_prewarm'] == 0
    finally:
        guard.uninstall_for_test()


def test_process_start_failure_remains_accounted_before_any_pid_exists(tmp_path):
    guard = _guard(tmp_path)
    class Process:
        name = 'tier3-validator-helper-1-1'
        pid = None
        exitcode = None
        def start(self):
            raise OSError('creation failed')
        def join(self):
            pass
        def close(self):
            pass
        def run(self):
            pass
    guard._install_process(SimpleNamespace(BaseProcess=Process))
    try:
        with pytest.raises(OSError, match='creation failed'):
            Process().start()
        attempt, = guard.registry.snapshot()['attempts']
        assert attempt['error'] == 'OSError' and attempt['launch_finished'] is True
        assert attempt['pid'] is None and attempt['start_sent'] is False
    finally:
        guard.uninstall_for_test()


def test_process_run_publishes_final_receipt_even_without_normal_exit(tmp_path):
    guard = _guard(tmp_path)
    class Process:
        def start(self):
            pass
        def join(self):
            pass
        def close(self):
            pass
        def run(self):
            raise RuntimeError('child failure')
    guard._install_process(SimpleNamespace(BaseProcess=Process))
    try:
        with pytest.raises(RuntimeError, match='child failure'):
            Process().run()
        path = Path(guard.manifest['data_dir'], f'physical-config-control-{os.getpid()}.json')
        receipt = json.loads(path.read_text())
        assert receipt['completed'] is True
        assert receipt['evidence_class'] == 'process_configuration_only'
        assert receipt['received_prewarm'] == 0
    finally:
        guard.uninstall_for_test()


def test_audit_system_proxy_writes_fail_before_backend_mutation(tmp_path):
    guard = _guard(tmp_path)
    backend = SimpleNamespace(write=Mock())
    original = backend.write
    guard._block_system_proxy(SimpleNamespace(WinRegProxyBackend=backend))
    try:
        with pytest.raises(PermissionError, match='system proxy'):
            backend.write({})
        original.assert_not_called()
    finally:
        guard.uninstall_for_test()


def test_socket_guard_refuses_external_loopback_but_allows_owned_listener(tmp_path):
    guard = _guard(tmp_path)
    # Keep this unit test independent of modules imported by the pytest host.
    guard.hooks = AuditImportHooks({})
    original = socket.socket.connect
    try:
        guard.install()
        assert guard.socket_guard_installed()
        with socket.socket() as external:
            with pytest.raises(PermissionError, match='non-owned'):
                external.connect(('127.0.0.1', 19112))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            with socket.socket() as client:
                client.connect(listener.getsockname())
                peer, _ = listener.accept()
                peer.close()
        guard.finish()
        receipt = json.loads(Path(guard.manifest['data_dir'],
                                 f'physical-config-control-{os.getpid()}.json').read_text())
        assert receipt['socket_guard_installed'] is True and receipt['completed'] is True
    finally:
        guard.uninstall_for_test()
    assert socket.socket.connect is original


def test_audit_firewall_commands_fail_before_host_mutation(tmp_path):
    guard = _guard(tmp_path)
    owner = SimpleNamespace(_run_command=Mock())
    original = owner._run_command
    guard._block_firewall(SimpleNamespace(KillSwitchManager=owner))
    try:
        with pytest.raises(PermissionError, match='system firewall'):
            owner._run_command(['netsh', 'advfirewall', 'firewall', 'delete', 'rule'], timeout=1)
        original.assert_not_called()
    finally:
        guard.uninstall_for_test()
    assert owner._run_command is original
