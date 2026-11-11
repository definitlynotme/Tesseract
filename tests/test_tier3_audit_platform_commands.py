"""Observe native OS queries without substituting their answers or ignoring children."""
import hashlib
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.tier3_audit_process_census import audit_process_census
from scripts.tier3_audit_process_registry import native_process_inventory_failures
from tests.test_tier3_audit_entry import _guard


@pytest.mark.skipif(sys.platform != 'win32', reason='real Windows version command')
def test_real_platform_version_query_has_registered_exit(tmp_path):
    guard = _guard(tmp_path)
    guard.install()
    try:
        result = platform._syscmd_ver()
        assert result[0] == 'Microsoft'
        guard._record_native_exits()
        commands = guard.receipt['command_process_inventory']
        assert len(commands['attempts']) == 1
        assert not native_process_inventory_failures(commands)
        command, = commands['attempts']
        from scripts.tier3_audit_job_launch import system_console_host_path
        binary = system_console_host_path().with_name('cmd.exe')
        assert command['executable_sha256'] == hashlib.sha256(binary.read_bytes()).hexdigest()
        assert command['pid'] > 0 and command['exitcode'] == 0
        assert guard.receipt['command_policy_violation_count'] == 0
    finally:
        guard.uninstall_for_test()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows command policy')
@pytest.mark.parametrize('command', ['ver & echo extra', 'cmd /c ver', ['ver']])
def test_extra_command_bytes_are_rejected_before_os_creation(tmp_path, monkeypatch, command):
    created = []

    class Process:
        def __init__(self, *args, **kwargs):
            created.append(True)

    monkeypatch.setattr(subprocess, 'Popen', Process)
    guard = _guard(tmp_path)
    module = SimpleNamespace(_syscmd_ver=lambda: subprocess.Popen(command, shell=True))
    guard._install_platform(module)
    try:
        with pytest.raises(PermissionError):
            module._syscmd_ver()
        assert not created
        attempt, = guard.receipt['command_process_inventory']['attempts']
        assert not attempt['creation_attempted'] and attempt['pid'] is None
        assert guard.receipt['command_policy_violation_count'] == 1
        census = audit_process_census({}, {guard.receipt['process_id']: guard.receipt},
                                      dict(schema=1, attempts=[]), console_host_path=tmp_path / 'conhost.exe')
        assert f"process_census_command_policy_violated:{guard.receipt['process_id']}" in census['failures']
    finally:
        guard.uninstall_for_test()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows registry query policy')
def test_autorun_is_not_silently_removed_or_executed(tmp_path, monkeypatch):
    import winreg
    created = []

    class Process:
        def __init__(self, *args, **kwargs):
            created.append(True)

    class RegistryKey:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(subprocess, 'Popen', Process)
    monkeypatch.setattr(winreg, 'OpenKey', lambda *_a, **_k: RegistryKey())
    monkeypatch.setattr(winreg, 'QueryValueEx', lambda *_a: ('unapproved startup command', winreg.REG_SZ))
    guard = _guard(tmp_path)
    module = SimpleNamespace(_syscmd_ver=lambda: subprocess.Popen('ver', shell=True))
    guard._install_platform(module)
    try:
        with pytest.raises(PermissionError, match='AutoRun'):
            module._syscmd_ver()
        assert not created and guard.receipt['command_policy_violation_count'] == 1
    finally:
        guard.uninstall_for_test()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows command policy')
@pytest.mark.parametrize('override', ['environment', 'executable'])
def test_platform_query_rejects_alternative_shell(tmp_path, monkeypatch, override):
    created = []

    class Process:
        def __init__(self, *args, **kwargs):
            created.append(True)

    alternate = tmp_path / 'cmd.exe'
    alternate.write_bytes(b'not the system command processor')
    options = {'shell': True}
    if override == 'environment':
        monkeypatch.setenv('COMSPEC', str(alternate))
    else:
        options['executable'] = str(alternate)
    monkeypatch.setattr(subprocess, 'Popen', Process)
    guard = _guard(tmp_path)
    module = SimpleNamespace(_syscmd_ver=lambda: subprocess.Popen('ver', **options))
    guard._install_platform(module)
    try:
        with pytest.raises(PermissionError):
            module._syscmd_ver()
        assert not created
        assert guard.receipt['command_policy_violation_count'] == 1
        attempt, = guard.receipt['command_process_inventory']['attempts']
        assert not attempt['creation_attempted'] and attempt['pid'] is None
    finally:
        guard.uninstall_for_test()
