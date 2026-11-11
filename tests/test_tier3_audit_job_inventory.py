from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.tier3_audit_job_inventory import (
    AuditJobEvents, WindowsAuditJobInventory, job_inventory_failures,
)


def closed_events():
    events = AuditJobEvents()
    for pid in (11, 12, 13):
        events.observe(6, pid)
        events.observe(7, pid)
    return events.snapshot(total_processes=3, active_processes=0)


def test_job_receipts_require_kernel_count_and_registered_process_agreement():
    result = closed_events()
    assert not job_inventory_failures(result, registered_pids={11, 12, 13})
    assert 'job_process_unregistered:13' in job_inventory_failures(
        result, registered_pids={11, 12})
    assert 'job_registered_process_unobserved:14' in job_inventory_failures(
        result, registered_pids={11, 12, 13, 14})


@pytest.mark.parametrize('message', [6, 7])
def test_missing_notification_is_not_proof_a_child_did_not_exist(message):
    events = AuditJobEvents()
    for kind in (6, 7):
        if kind != message:
            events.observe(kind, 11)
    snapshot = events.snapshot(total_processes=1, active_processes=0)
    assert job_inventory_failures(snapshot, registered_pids={11})


def test_lost_birth_and_exit_are_detected_by_cumulative_kernel_count():
    snapshot = closed_events()
    snapshot['total_processes'] = 4
    assert 'job_notification_coverage_unproven' in job_inventory_failures(
        snapshot, registered_pids={11, 12, 13})


def test_duplicate_birth_while_alive_is_not_silently_merged():
    events = AuditJobEvents()
    events.observe(6, 11)
    events.observe(6, 11)
    events.observe(7, 11)
    assert 'job_pid_reused_or_duplicate:11' in job_inventory_failures(
        events.snapshot(total_processes=2, active_processes=0), registered_pids={11})


def test_pid_reused_after_exit_is_not_a_duplicate():
    # ship1 18-act / 27-act (10-28): birth -> exit -> birth of the same PID is OS PID reuse.
    events = AuditJobEvents()
    for _ in range(2):
        events.observe(6, 11)
        events.observe(7, 11)
    failures = job_inventory_failures(
        events.snapshot(total_processes=2, active_processes=0), registered_pids={11})
    assert not any(f.startswith('job_pid_reused_or_duplicate') for f in failures)
    assert 'job_notification_coverage_unproven' not in failures
    # the kernel count still has to cover every birth
    assert 'job_notification_coverage_unproven' in job_inventory_failures(
        events.snapshot(total_processes=3, active_processes=0), registered_pids={11})


def test_abnormal_exit_remains_in_inventory_and_cannot_pass_clean_acceptance():
    events = AuditJobEvents()
    events.observe(6, 11)
    events.observe(8, 11)
    events.observe(7, 11)
    assert 'job_abnormal_exit:11' in job_inventory_failures(
        events.snapshot(total_processes=1, active_processes=0), registered_pids={11})


@pytest.mark.parametrize('field,value', [
    ('active_processes', 1), ('total_processes', False), ('total_processes', '3'),
    ('active_processes', None), ('errors', ['read_failure']),
    ('schema', 0), ('events', []),
])
def test_incomplete_or_invalid_os_evidence_fails_closed(field, value):
    snapshot = closed_events()
    snapshot[field] = value
    assert job_inventory_failures(snapshot, registered_pids={11, 12, 13})


def test_final_witness_recomputes_events_instead_of_trusting_summary_counts():
    snapshot = closed_events()
    snapshot['events'][1]['message'] = 4
    snapshot.update(exited_count=3, complete=True)
    assert job_inventory_failures(snapshot, registered_pids={11, 12, 13})


def readback_without_notification():
    result = closed_events()
    result['events'] = [item for item in result['events']
                        if not (item['pid'] == 13 and item['message'] == 7)]
    result['process_images'] = [dict(pid=13, created_at_filetime=12345)]
    result['exit_readbacks'] = [dict(pid=13, created_at_filetime=12345,
                                    exit_confirmed=True, exitcode=0)]
    return result


def test_missing_exit_notification_requires_same_retained_process_exit_readback():
    result = readback_without_notification()
    assert not job_inventory_failures(result, registered_pids={11, 12, 13})
    assert not any(item['pid'] == 13 and item['message'] == 7 for item in result['events'])


@pytest.mark.parametrize('field,value', [
    ('pid', 14), ('created_at_filetime', 54321), ('exit_confirmed', False),
    ('exit_confirmed', 1), ('exitcode', None), ('exitcode', True),
    ('exitcode', 0xc0000005),
])
def test_invalid_or_crashed_retained_process_cannot_replace_exit_evidence(field, value):
    result = readback_without_notification()
    result['exit_readbacks'][0][field] = value
    assert job_inventory_failures(result, registered_pids={11, 12, 13})


def test_retained_exit_readback_cannot_replace_missing_birth_accounting():
    result = readback_without_notification()
    result['events'] = [item for item in result['events'] if item['pid'] != 13]
    assert job_inventory_failures(result, registered_pids={11, 12, 13})


def test_exit_notification_cannot_override_a_live_retained_process():
    result = readback_without_notification()
    result['events'].append(dict(message=7, pid=13))
    result['exit_readbacks'][0].update(exit_confirmed=False, exitcode=None)
    assert 'job_retained_process_still_active:13' in job_inventory_failures(
        result, registered_pids={11, 12, 13})


def test_early_snapshot_is_immutable_and_zero_count_job_is_not_a_run():
    events = AuditJobEvents()
    events.observe(6, 11)
    early = events.snapshot(total_processes=1, active_processes=1)
    preserved = copy.deepcopy(early)
    events.observe(7, 11)
    assert early == preserved
    assert 'job_no_processes' in job_inventory_failures(
        AuditJobEvents().snapshot(total_processes=0, active_processes=0),
        registered_pids=set())


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel Job witness')
def test_real_job_captures_child_and_short_lived_grandchild_without_network():
    from tier3_windows_job import WindowsJob

    # The GUI base interpreter avoids the venv redirector and OS console-host
    # children. Console executables require those additional launch receipts.
    executable = str(Path(sys._base_executable).with_name('pythonw.exe'))
    code = ('import subprocess,sys; sys.stdin.buffer.read(1); '
            'p=subprocess.Popen([sys.executable,"-c","pass"],'
            'creationflags=subprocess.CREATE_NO_WINDOW); '
            'print(p.pid,flush=True); p.wait()')
    with WindowsJob() as job, WindowsAuditJobInventory(job) as witness:
        process = subprocess.Popen(
            [executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            job.assign_pid(process.pid)
            stdout, stderr = process.communicate(b'x', timeout=10)
            assert process.returncode == 0, stderr.decode(errors='replace')
            grandchild = int(stdout.strip())
            deadline = time.monotonic() + 2
            while True:
                snapshot = witness.snapshot()
                failures = job_inventory_failures(
                    snapshot, registered_pids={process.pid, grandchild})
                if not failures or time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            assert not failures, (failures, snapshot)
            assert snapshot['total_processes'] == 2
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel Job witness')
def test_cannot_attach_witness_after_job_has_already_started():
    from tier3_windows_job import WindowsJob

    executable = getattr(sys, '_base_executable', sys.executable)
    with WindowsJob() as job:
        process = subprocess.Popen([executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                   stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            job.assign_pid(process.pid)
            with pytest.raises(ValueError, match='empty'):
                WindowsAuditJobInventory(job)
        finally:
            process.communicate(b'x', timeout=10)
