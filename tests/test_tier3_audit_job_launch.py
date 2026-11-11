from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.tier3_audit_job_inventory import WindowsAuditJobInventory, job_inventory_failures
from scripts.tier3_audit_job_launch import (
    AuditJobLaunchRegistry, _environment_block, launch_in_audit_job, system_console_host_path,
)
from tier3_windows_job import WindowsJob


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows atomic audit Job launch')


def executable():
    return str(Path(sys._base_executable).with_name('pythonw.exe'))


def final_snapshot(witness):
    deadline = time.monotonic() + 2
    while True:
        snapshot = witness.snapshot()
        births = [e['pid'] for e in snapshot['events'] if e['message'] == 6]
        if not job_inventory_failures(snapshot, registered_pids=births):
            return snapshot
        if time.monotonic() >= deadline:
            return snapshot
        time.sleep(.01)


def test_kernel_membership_precedes_first_instruction_and_immediate_grandchild(tmp_path):
    marker = tmp_path / 'running.json'
    code = ('import subprocess,sys,json,pathlib; '
            'p=subprocess.Popen([sys.executable,"-c","pass"],'
            'creationflags=subprocess.CREATE_NO_WINDOW); '
            'pathlib.Path(sys.argv[1]).write_text(json.dumps({"pid":p.pid})); p.wait()')
    registered = []
    with WindowsJob() as job, WindowsAuditJobInventory(job) as witness, (tmp_path / 'log').open('wb') as log:
        def created(process):
            assert not marker.exists()
            assert process.pid in job.snapshot().process_ids
            assert process.poll() is None
            registered.append(process.pid)
        with launch_in_audit_job(job, [executable(), '-c', code, str(marker)], stdout=log,
                                 on_created=created) as process:
            assert process.wait(10) == 0
        registered.append(json.loads(marker.read_text())['pid'])
        snapshot = final_snapshot(witness)
        assert not job_inventory_failures(snapshot, registered_pids=registered), snapshot
        assert snapshot['total_processes'] == 2


def test_receipt_failure_never_resumes_child_and_does_not_hide_kernel_birth(tmp_path):
    marker = tmp_path / 'must-not-exist'
    registered = []
    with WindowsJob() as job, WindowsAuditJobInventory(job) as witness, (tmp_path / 'log').open('wb') as log:
        def rejected(process):
            registered.append(process.pid)
            raise ValueError('receipt rejected')
        with pytest.raises(ValueError, match='receipt rejected'):
            launch_in_audit_job(job, [executable(), '-c',
                'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(marker)],
                stdout=log, on_created=rejected)
        assert not marker.exists()
        snapshot = final_snapshot(witness)
        assert snapshot['active_processes'] == 0
        assert snapshot['total_processes'] == 1
        assert {e['pid'] for e in snapshot['events'] if e['message'] == 6} == set(registered)


def test_explicit_environment_arguments_and_log_handles_are_preserved(tmp_path):
    value = 'spaces " quotes and 中文'
    code = 'import os,sys,json; print(json.dumps([os.environ["AUDIT_VALUE"],sys.argv[1],os.getcwd()]))'
    log_path = tmp_path / 'log'
    with WindowsJob() as job, log_path.open('wb') as log:
        with launch_in_audit_job(job, [executable(), '-c', code, value], stdout=log,
                env={**os.environ, 'AUDIT_VALUE': value}, cwd=tmp_path) as process:
            assert process.wait(10) == 0
    assert json.loads(log_path.read_text()) == [value, value, str(tmp_path)]


def test_failed_native_creation_does_not_fall_back_or_add_job_members(tmp_path):
    with WindowsJob() as job, WindowsAuditJobInventory(job) as witness, (tmp_path / 'log').open('wb') as log:
        with pytest.raises(OSError):
            launch_in_audit_job(job, [str(tmp_path / 'absent.exe')], stdout=log)
        snapshot = witness.snapshot()
        assert snapshot['total_processes'] == snapshot['active_processes'] == 0


def test_console_children_are_observed_not_silently_ignored(tmp_path):
    console_executable = str(Path(sys._base_executable))
    with WindowsJob() as job, WindowsAuditJobInventory(job, observe_images=True) as witness, (tmp_path / 'log').open('wb') as log:
        with launch_in_audit_job(job, [console_executable, '-c', 'import time; time.sleep(.15)'], stdout=log) as process:
            assert process.wait(10) == 0
        snapshot = final_snapshot(witness)
        images = {item['pid']: item for item in snapshot['process_images']}
        births = {e['pid'] for e in snapshot['events'] if e['message'] == 6}
        assert not snapshot['errors'], snapshot
        assert births == set(images)
        assert Path(images[process.pid]['image_path']).resolve() == Path(console_executable).resolve()
        assert all(item['created_at_filetime'] > 0 for item in images.values())
        failures = job_inventory_failures(snapshot, registered_pids={process.pid})
        assert set(failures) == {f'job_process_unregistered:{pid}' for pid in births - {process.pid}}
        # No auto-registration by executable name: the session must separately
        # verify and account for any system-created console host.


def test_live_process_cannot_be_discarded_and_only_owned_handle_is_terminated(tmp_path):
    with WindowsJob() as job, (tmp_path / 'log').open('wb') as log:
        with launch_in_audit_job(job, [executable(), '-c', 'import time; time.sleep(30)'], stdout=log) as process:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(.01)
            with pytest.raises(RuntimeError, match='live'):
                process.close()
            process.terminate()
            assert process.wait(5) == 1
        assert process.poll() == 1


@pytest.mark.parametrize('environment', [{'bad=key': 'value'}, {'key': 'bad\0value'}, {'': 'value'}])
def test_invalid_environment_rejected_without_truncation(environment):
    with pytest.raises(ValueError):
        _environment_block(environment)


def test_launcher_registry_keeps_failed_attempts_replacements_and_final_exits(tmp_path):
    from scripts.tier3_audit_process_registry import native_process_inventory_failures
    config = tmp_path / 'config.json'
    config.write_text('{}')
    with WindowsJob() as job, AuditJobLaunchRegistry() as registry, (tmp_path / 'log').open('wb') as log:
        for role in ('resolver', 'application', 'application'):
            child = registry.launch(job, role, [executable(), '-c', 'pass'], config_path=config, stdout=log)
            assert child.wait(10) == 0
        with pytest.raises(FileNotFoundError):
            registry.launch(job, 'application', [str(tmp_path / 'absent.exe')], config_path=config, stdout=log)
        snapshot = registry.snapshot()
        assert not native_process_inventory_failures(snapshot), snapshot
        assert len(snapshot['attempts']) == 4
        assert [item['role'] for item in snapshot['attempts']] == [
            'resolver', 'application', 'application', 'application']
        assert snapshot['attempts'][-1]['creation_attempted'] is False
        assert all(item['exit_confirmed'] for item in snapshot['attempts'][:-1])
    assert registry.snapshot() == snapshot


def test_console_host_authority_does_not_come_from_environment(monkeypatch):
    expected = system_console_host_path()
    monkeypatch.setenv('SystemRoot', 'Z:\\untrusted')
    assert system_console_host_path() == expected
    assert expected.is_file()
