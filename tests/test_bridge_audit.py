from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridge_audit
from bridge_instance_scope import resolve_scope
from scripts import tier3_audit_entry as entry
from scripts.tier3_audit_import_hooks import AuditImportHooks


@pytest.fixture
def audit(tmp_path, monkeypatch):
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    payload = dict(schema=1, audit_id='a' * 32, interface='physical', interface_index=1,
                   dns_controller_port=19110, dns_listener_port=19111,
                   config_dir=str(config), data_dir=str(data))
    manifest = tmp_path / 'audit.json'
    manifest.write_bytes(json.dumps(payload).encode())
    for key, value in {'TESSERACT_CONFIG_DIR': str(config), 'TESSERACT_DATA_DIR': str(data),
                       entry.ENVIRONMENT_KEY: str(manifest)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('TESSERACT_INSTANCE_SCOPE', raising=False)
    monkeypatch.setattr(entry, '_guard', None)
    # Exercise real socket/command guards, keeping unrelated pytest imports out.
    original_init = entry.IsolatedAuditGuard.__init__
    def initialize(guard, value):
        original_init(guard, value)
        guard.hooks = AuditImportHooks({
            'system_proxy': guard._block_system_proxy,
            'killswitch': guard._block_firewall,
            'runtime_network_recovery': guard._block_recovery_firewall,
        })
    monkeypatch.setattr(entry.IsolatedAuditGuard, '__init__', initialize)
    scope = resolve_scope(config_dir=config, data_dir=data)
    try:
        yield scope, manifest
    finally:
        if entry._guard is not None:
            entry._guard.uninstall_for_test()


def test_runtime_hook_installs_before_entry_and_attach_emits_no_markers(audit):
    scope, _ = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    guard = entry._guard
    assert guard.socket_guard_installed()
    assert bridge_audit.attach_audit_guard(scope) is guard
    assert not list(scope.data_dir.glob('*start-control*'))
    assert not (scope.data_dir / 'child-process-control.json').exists()


@pytest.mark.parametrize('installed', [False, True])
def test_missing_hook_fails_before_rt1_runtime_and_markers(audit, monkeypatch, capsys,
                                                         installed):
    import bridge_main
    scope, _ = audit
    if installed:
        # A late/manual installation is not evidence that the hook ran.
        entry.install_from_environment()
    install = Mock(side_effect=AssertionError('entry must never install a guard'))
    monkeypatch.setattr(entry, 'install_from_environment', install)
    action, recovery, runtime = Mock(), Mock(), Mock()
    lease = Mock()
    steps = bridge_main.Steps(
        scope=lambda: scope,
        acquire=lambda _: SimpleNamespace(status='acquired', lease=lease),
        logging=lambda _: None, audit=bridge_audit.attach_audit_guard,
        recovery=recovery, transport=runtime, loop=runtime)
    assert bridge_main.main([], steps=steps) == 4
    assert json.loads(capsys.readouterr().err) == {
        'code': 'startup_failed', 'type': 'PermissionError'}
    with pytest.raises(PermissionError, match='runtime-hook'):
        bridge_audit.start_startup_audit(scope, action)
    for callback in (install, action, recovery, runtime):
        callback.assert_not_called()
    lease.close.assert_called_once_with()
    if not installed:
        assert entry._guard is None
    for name in ('window-start-control.json', 'child-process-control.json'):
        assert not (scope.data_dir / name).exists()
    assert not list(scope.data_dir.glob('*.pending'))


def test_hook_evidence_from_another_process_is_rejected(audit, monkeypatch):
    scope, _ = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    monkeypatch.setattr(entry._guard, '_runtime_hook_pid', os.getpid() + 1)
    with pytest.raises(PermissionError, match='runtime-hook'):
        bridge_audit.attach_audit_guard(scope)


def test_markers_atomic_complete_and_visible_at_real_action(audit, monkeypatch):
    scope, manifest = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    calls = []
    original = bridge_audit.replace_with_retry
    def replace_marker(source, target):
        assert source.parent == target.parent
        assert source.suffix == '.pending'
        assert json.loads(source.read_bytes())['audit_id'] == scope.id
        if target.exists():
            assert target.read_bytes() == b'old'
        original(source, target)
        calls.append(target.name)
    monkeypatch.setattr(bridge_audit, 'replace_with_retry', replace_marker)
    for name in ('window-start-control.json', 'child-process-control.json'):
        (scope.data_dir / name).write_bytes(b'old')
    def action(value, *, flag):
        assert calls == ['window-start-control.json', 'child-process-control.json']
        marker = json.loads((scope.data_dir / calls[0]).read_bytes())
        assert marker == json.loads((scope.data_dir / calls[1]).read_bytes())
        assert set(marker) == {'window_started_monotonic', 'process_id', 'parent_process_id',
                               'audit_id', 'manifest_sha256'}
        assert marker['process_id'] == os.getpid()
        assert marker['parent_process_id'] == os.getppid()
        assert marker['window_started_monotonic'] > 0
        assert marker['manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
        return value, flag
    assert bridge_audit.start_startup_audit(scope, action, 7, flag=True) == (7, True)
    assert not list(scope.data_dir.glob('*.pending'))


def test_marker_replace_failure_does_not_enter_action(audit, monkeypatch):
    scope, _ = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    target = scope.data_dir / 'window-start-control.json'
    target.write_bytes(b'old')
    monkeypatch.setattr(bridge_audit, 'replace_with_retry', Mock(side_effect=PermissionError))
    action = Mock()
    with pytest.raises(PermissionError):
        bridge_audit.start_startup_audit(scope, action)
    assert target.read_bytes() == b'old'
    action.assert_not_called()


@pytest.mark.parametrize('changes', [dict(id='b' * 32), dict(kind='default'),
    dict(may_take_over_host_network=True), dict(data_dir=None)])
def test_scope_mismatch_fails_before_guard_or_marker(audit, changes):
    scope, _ = audit
    with pytest.raises(PermissionError):
        bridge_audit.attach_audit_guard(replace(scope, **changes))
    assert entry._guard is None
    assert not list(scope.data_dir.iterdir())


def test_changed_manifest_and_tampered_guard_fail_closed(audit, monkeypatch):
    scope, manifest = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    guard = bridge_audit.attach_audit_guard(scope)
    raw = manifest.read_bytes()
    manifest.write_bytes(raw + b'\n')
    with pytest.raises(PermissionError, match='stale'):
        bridge_audit.attach_audit_guard(scope)
    manifest.write_bytes(raw)
    monkeypatch.setattr(guard, 'socket_guard_installed', lambda: False)
    with pytest.raises(PermissionError, match='incomplete'):
        bridge_audit.attach_audit_guard(scope)


def test_inactive_and_missing_manifest(audit, monkeypatch):
    scope, _ = audit
    monkeypatch.delenv(entry.ENVIRONMENT_KEY)
    assert bridge_audit.attach_audit_guard(replace(scope, kind='custom')) is None
    with pytest.raises(PermissionError):
        bridge_audit.attach_audit_guard(scope)
    with pytest.raises(PermissionError):
        bridge_audit.start_startup_audit(replace(scope, kind='custom'), Mock())


@pytest.mark.parametrize('flag', ['--multiprocessing-fork', '--validator-helper-smoke=1',
                                  '--validator-helper-slots'])
def test_helpers_never_publish_root_markers(audit, monkeypatch, flag):
    scope, _ = audit
    monkeypatch.setattr(sys, 'argv', ['tesseract-runtime.exe', flag])
    with pytest.raises(PermissionError, match='helpers'):
        bridge_audit.start_startup_audit(scope, Mock())
    assert not list(scope.data_dir.iterdir())


def test_real_bridge_order_and_rt1_skip_all_host_io(audit):
    import bridge_main
    from runtime_network_recovery import recover_network_after_unclean_exit
    scope, _ = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    events = []
    lease = SimpleNamespace(close=lambda: events.append('closed'))
    def attach(value):
        bridge_audit.attach_audit_guard(value)
        events.append('guard')
    def recover(value):
        assert events == ['guard']
        assert entry._guard.socket_guard_installed()
        backend, runner = Mock(), Mock()
        report = recover_network_after_unclean_exit(
            SimpleNamespace(path=scope.data_dir / 'must-not-open.json'), scope=value,
            backend=backend, command_runner=runner)
        assert all(part['error_code'] == 'scope_isolated' for part in report.values())
        report.gate.require_idle()
        assert not backend.mock_calls and not runner.mock_calls
        events.append('RT-1')
        return report
    def unavailable(_args):
        raise bridge_main.StartupError('controlled_end')
    steps = bridge_main.Steps(scope=lambda: scope,
        acquire=lambda _: SimpleNamespace(status='acquired', lease=lease),
        logging=lambda _: None, audit=attach, recovery=recover, transport=unavailable)
    assert bridge_main.main([], steps=steps) == 4
    assert events == ['guard', 'RT-1', 'closed']
    assert not (scope.data_dir / 'window-start-control.json').exists()


def test_guard_blocks_proxy_and_both_recovery_firewall_aliases(audit):
    import killswitch
    import runtime_network_recovery as recovery
    import system_proxy
    scope, _ = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    bridge_audit.attach_audit_guard(scope)
    runner = Mock()
    for module in (killswitch, recovery):
        with pytest.raises(PermissionError, match='firewall'):
            module.recovery_delete_owned_firewall_rule('id', 'name', runner, timeout=1)
    with pytest.raises(PermissionError, match='proxy'):
        system_proxy.WinRegProxyBackend.write(object(), object())
    runner.assert_not_called()


def test_root_helper_inventory_job_and_exit_receipts(audit, monkeypatch):
    scope, _ = audit
    runpy.run_path('scripts/tier3_audit_runtime_hook.py')
    root = bridge_audit.attach_audit_guard(scope)
    root_pid = os.getpid()
    helper_pid = root_pid + 100000
    class Process:
        name = 'tier3-validator-helper-fixture'
        pid = None
        exitcode = None
        def start(self): self.pid = helper_pid
        def join(self): self.exitcode = 0
        def close(self): pass
        def run(self): raise RuntimeError('controlled helper failure')
    root._install_process(SimpleNamespace(BaseProcess=Process))
    process = Process()
    process.start()
    root.registry.job_assigned(helper_pid, [helper_pid])
    root.registry.start_sent(helper_pid)
    process.join()
    # Simulate the helper runtime hook and BaseProcess.run finally, without spawning.
    with monkeypatch.context() as child:
        child.setattr(os, 'getpid', lambda: helper_pid)
        child.setattr(os, 'getppid', lambda: root_pid)
        helper = entry.IsolatedAuditGuard(root.manifest)
        helper.hooks = AuditImportHooks({})
        try:
            helper.install()
            class ChildProcess:
                def start(self): pass
                def join(self): pass
                def close(self): pass
                def run(self): raise RuntimeError('controlled helper failure')
            helper._install_process(SimpleNamespace(BaseProcess=ChildProcess))
            with pytest.raises(RuntimeError):
                ChildProcess().run()
        finally:
            helper.uninstall_for_test()
    root.finish()
    parent = json.loads((scope.data_dir / f'physical-config-control-{root_pid}.json').read_bytes())
    child = json.loads((scope.data_dir / f'physical-config-control-{helper_pid}.json').read_bytes())
    for receipt in (parent, child):
        assert receipt['completed'] is True and receipt['socket_guard_installed'] is True
        assert receipt['audit_id'] == scope.id
        assert receipt['manifest_sha256'] == root.manifest['manifest_sha256']
        assert receipt['evidence_class'] == 'process_configuration_only'
        assert {'process_inventory', 'native_process_inventory', 'command_process_inventory',
                'executable', 'outbound_socket_guard_installed', 'non_owned_loopback_blocked',
                'auxiliary_proxy_disabled', 'mihomo_processes'} <= receipt.keys()
    assert child['parent_process_id'] == parent['process_id']
    attempt, = parent['process_inventory']['attempts']
    assert attempt['pid'] == helper_pid and attempt['job_process_ids'] == [helper_pid]
    assert attempt['start_sent'] and attempt['exit_confirmed'] and attempt['exitcode'] == 0


@pytest.mark.parametrize('matching', [True, False])
def test_frozen_identity_hashes_sidecar_not_shell(tmp_path, monkeypatch, matching):
    import tier3_build_identity as identity
    runtime = tmp_path / 'tesseract-runtime.exe'
    runtime.write_bytes(b'controlled sidecar executable')
    shell = tmp_path / 'Tesseract.exe'
    shell.write_bytes(b'different shell executable')
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    declared = digest if matching else hashlib.sha256(shell.read_bytes()).hexdigest()
    (tmp_path / 'tier3_build_id.json').write_bytes(json.dumps(dict(
        build_id='a' * 64, components={'bridge_audit.py': 'b' * 64},
        declared_executable_sha256=declared)).encode())
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(runtime))
    identity._frozen_executable_sha256.cache_clear()
    try:
        result = identity.tier3_build_identity()
        assert result['executable_sha256'] == digest
        assert result['identity_match'] is matching
    finally:
        identity._frozen_executable_sha256.cache_clear()
