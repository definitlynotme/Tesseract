from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace

import pytest
import yaml

import core_runner
from scripts.tier3_audit_process_registry import native_process_inventory_failures
from scripts.tier3_audit_socket_policy import pin_mihomo_audit_dns
from tests.test_tier3_audit_entry import _guard


@pytest.fixture
def audited_core(tmp_path, monkeypatch):
    guard = _guard(tmp_path)
    binary = tmp_path / 'fixture-core.exe'
    binary.write_bytes(b'fake binary: never executed')
    path = tmp_path / 'data' / 'core.yaml'
    config = {'interface-name': 'physical', 'tun': {'enable': False}}
    pin_mihomo_audit_dns(config, 19111)
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    processes, commands = [], []

    def factory(*args, **kwargs):
        commands.append((args, kwargs))
        process = SimpleNamespace(pid=101 + len(processes), returncode=None, stdout=io.StringIO(''))
        process.poll = lambda: process.returncode
        processes.append(process)
        return process

    monkeypatch.setattr(core_runner.MihomoCoreRunner, '_start_reader_thread', lambda *a, **k: None)
    guard._install_core_runner(core_runner)
    runner = core_runner.MihomoCoreRunner(
        binary_path=str(binary), runtime_config_path=str(path),
        work_dir=str(tmp_path / 'data'), process_factory=factory,
    )
    yield SimpleNamespace(guard=guard, runner=runner, path=path, config=config,
                          processes=processes, commands=commands, binary=binary)
    guard.uninstall_for_test()


def test_core_start_and_final_exit_are_both_registered(audited_core):
    case = audited_core
    assert case.runner.start()['running']
    item, = case.guard.receipt['native_process_inventory']['attempts']
    assert item['pid'] == 101 and item['creation_attempted']
    assert item['configs'][0]['sha256'] == hashlib.sha256(case.path.read_bytes()).hexdigest()
    assert item['executable_sha256'] == hashlib.sha256(case.binary.read_bytes()).hexdigest()
    assert native_process_inventory_failures(case.guard.receipt['native_process_inventory']) == [
        'isolation_core_exit_unproven:101',
    ]
    case.processes[0].returncode = 0
    case.guard.finish()
    assert not native_process_inventory_failures(case.guard.receipt['native_process_inventory'])


def test_replacement_core_does_not_erase_old_live_process(audited_core):
    case = audited_core
    case.runner.start()
    # Model an abnormal owner replacement; the old handle must remain audited.
    case.runner.process = None
    case.runner.start()
    case.processes[1].returncode = 0
    case.guard.finish()
    assert native_process_inventory_failures(case.guard.receipt['native_process_inventory']) == [
        'isolation_core_exit_unproven:101',
    ]
    case.processes[0].returncode = 1
    case.guard.finish()
    assert not native_process_inventory_failures(case.guard.receipt['native_process_inventory'])


@pytest.mark.parametrize('config', [{}, {'interface-name': 'physical', 'tun': {'enable': True}}])
def test_unchecked_core_file_is_rejected_before_os_creation(audited_core, config):
    case = audited_core
    case.path.write_text(yaml.safe_dump(config), encoding='utf-8')
    with pytest.raises(ValueError):
        case.runner.start()
    assert not case.commands
    item, = case.guard.receipt['native_process_inventory']['attempts']
    assert not item['creation_attempted'] and item['error'] == 'ValueError'
    assert item['pid'] is None and item['launch_finished']


def test_os_launch_error_cannot_disappear_from_inventory(audited_core):
    case = audited_core
    # The configured underlying factory is fixed at construction, so install a
    # second runner with a failing factory rather than bypassing its wrapper.
    def fail(*args, **kwargs):
        raise OSError('launch error')
    runner = core_runner.MihomoCoreRunner(
        binary_path=str(case.binary), runtime_config_path=str(case.path),
        work_dir=str(case.path.parent), process_factory=fail,
    )
    with pytest.raises(OSError):
        runner.start()
    assert native_process_inventory_failures(case.guard.receipt['native_process_inventory']) == [
        'isolation_core_launch_pid_unproven:core:1',
    ]


def test_hot_reload_revalidates_actual_file_before_controller_call(audited_core, monkeypatch):
    case = audited_core
    case.runner.start()
    calls = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def read(self):
            return b''

    monkeypatch.setattr(core_runner, 'urlopen', lambda *a, **k: calls.append(True) or Response())
    case.config['dns']['nameserver'] = ['system']
    case.path.write_text(yaml.safe_dump(case.config), encoding='utf-8')
    with pytest.raises(ValueError, match='DNS'):
        case.runner.hot_reload()
    assert not calls
    pin_mihomo_audit_dns(case.config, 19111)
    case.path.write_text(yaml.safe_dump(case.config), encoding='utf-8')
    assert case.runner.hot_reload()['reloaded']
    assert len(calls) == 1
    item, = case.guard.receipt['native_process_inventory']['attempts']
    assert len(item['configs']) == 2
    assert 'nameserver' not in json.dumps(item)


def test_unknown_running_core_cannot_hot_reload_as_if_audited(audited_core, monkeypatch):
    case = audited_core
    case.runner.process = SimpleNamespace(pid=199, poll=lambda: None)
    monkeypatch.setattr(core_runner, 'urlopen', lambda *a, **k: pytest.fail('unregistered process'))
    with pytest.raises(PermissionError, match='unregistered'):
        case.runner.hot_reload()


def test_reload_cannot_relabel_a_running_process_with_new_binary_hash(audited_core, monkeypatch):
    case = audited_core
    case.runner.start()
    original, = case.guard.receipt['native_process_inventory']['attempts']
    digest = original['executable_sha256']
    case.binary.write_bytes(b'changed binary')
    monkeypatch.setattr(core_runner, 'urlopen', lambda *a, **k: pytest.fail('changed executable'))
    with pytest.raises(ValueError, match='executable changed'):
        case.runner.hot_reload()
    item, = case.guard.receipt['native_process_inventory']['attempts']
    assert item['executable_sha256'] == digest
    assert len(item['configs']) == 1


def test_version_query_without_native_launch_is_not_a_valid_receipt(audited_core):
    case = audited_core
    case.runner.command_runner = lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout='not real version evidence', stderr='')
    with pytest.raises(RuntimeError, match='no native process receipt'):
        case.runner.get_version()
    attempt, = case.guard.receipt['command_process_inventory']['attempts']
    assert not attempt['creation_attempted'] and attempt['pid'] is None
    assert attempt['error'] == 'RuntimeError'
    assert not case.guard.receipt['native_process_inventory']['attempts']


def test_version_query_rejects_extra_flags_before_os_creation(audited_core):
    import subprocess
    case = audited_core
    case.runner.command_runner = lambda *_a, **_k: subprocess.run(
        [str(case.binary), '-f', 'unapproved-runtime.yaml'])
    with pytest.raises(PermissionError, match='exactly one'):
        case.runner.get_version()
    attempt, = case.guard.receipt['command_process_inventory']['attempts']
    assert not attempt['creation_attempted'] and attempt['pid'] is None


def test_version_exception_cannot_erase_live_process_handle(tmp_path, monkeypatch):
    import subprocess
    processes = []

    class Process:
        def __init__(self, *_a, **_k):
            self.pid, self.returncode = 991, None
            processes.append(self)
        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, 'Popen', Process)
    guard = _guard(tmp_path)
    binary = tmp_path / 'fixture.exe'
    binary.write_bytes(b'never executed')

    def fail_after_creation(command, **_kwargs):
        subprocess.Popen(command)
        raise subprocess.TimeoutExpired(command, .1)

    guard._install_core_runner(core_runner)
    try:
        runner = core_runner.MihomoCoreRunner(binary_path=str(binary),
            runtime_config_path=str(tmp_path / 'unused.yaml'), work_dir=str(tmp_path),
            command_runner=fail_after_creation)
        with pytest.raises(subprocess.TimeoutExpired):
            runner.get_version()
        assert native_process_inventory_failures(guard.receipt['command_process_inventory']) == [
            'isolation_core_exit_unproven:991']
        processes[0].returncode = 1
        guard.finish()
        assert not native_process_inventory_failures(guard.receipt['command_process_inventory'])
    finally:
        guard.uninstall_for_test()


def test_real_owned_core_exit_is_recorded_without_helper(tmp_path):
    import socket
    from pathlib import Path

    binary = Path(__file__).resolve().parents[1] / 'bin' / 'mihomo.exe'
    if not binary.is_file():
        pytest.skip('repository Mihomo unavailable')
    guard = _guard(tmp_path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = {'interface-name': 'physical', 'tun': {'enable': False},
              'mixed-port': port, 'bind-address': '127.0.0.1', 'allow-lan': False,
              'mode': 'rule', 'rules': ['MATCH,REJECT']}
    pin_mihomo_audit_dns(config, 19111)
    path = tmp_path / 'data' / 'runtime.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    guard._install_core_runner(core_runner)
    runner = core_runner.MihomoCoreRunner(
        binary_path=str(binary), runtime_config_path=str(path),
        work_dir=str(path.parent), project_root=path.parent,
    )
    try:
        status = runner.start()
        assert status['running']
        pid = status['pid']
        item, = guard.receipt['native_process_inventory']['attempts']
        assert item['pid'] == pid
        assert native_process_inventory_failures(guard.receipt['native_process_inventory']) == [
            f'isolation_core_exit_unproven:{pid}',
        ]
    finally:
        runner.stop(timeout=3)
        guard.finish()
        guard.uninstall_for_test()
    assert not native_process_inventory_failures(guard.receipt['native_process_inventory'])
    receipt = json.loads(next((tmp_path / 'data').glob('physical-config-control-*.json')).read_text())
    assert receipt['completed']
    assert not native_process_inventory_failures(receipt['native_process_inventory'])
