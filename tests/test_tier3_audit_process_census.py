from __future__ import annotations

import copy

import pytest

from scripts.tier3_audit_process_census import audit_process_census


def case(tmp_path):
    console = str(tmp_path / 'system32' / 'conhost.exe')
    def attempt(pid, role=None):
        return dict(attempt_id=str(pid), pid=pid, launch_finished=True, creation_attempted=True,
                    configs=[{'sha256': '1' * 64}], executable_sha256='2' * 64,
                    exit_confirmed=True, exitcode=0, role=role)
    launch = dict(schema=1, attempts=[attempt(10, 'application'), attempt(11, 'resolver')])
    receipts = {
        10: dict(process_id=10, mihomo_processes=[], native_process_inventory=dict(schema=1, attempts=[attempt(12)])),
        13: dict(process_id=13, mihomo_processes=[{'pid': 14}], native_process_inventory=dict(schema=1, attempts=[])),
    }
    snapshot = dict(schema=1, total_processes=6, active_processes=0, errors=[], query_stable=True,
                    image_observation_enabled=True,
                    events=[dict(message=message, pid=pid) for pid in range(10, 16) for message in (6, 7)],
                    process_images=[dict(pid=pid, image_path=console if pid == 15 else str(tmp_path / f'{pid}.exe'),
                                         created_at_filetime=pid * 1000) for pid in range(10, 16)])
    return snapshot, receipts, launch, console


def test_resolver_launcher_core_helpers_and_os_console_all_accounted(tmp_path):
    snapshot, receipts, launch, console = case(tmp_path)
    result = audit_process_census(snapshot, receipts, launch, console_host_path=console)
    assert not result['failures']
    assert result['registered_pids'] == list(range(10, 16))
    assert [item['pid'] for item in result['os_auxiliary_processes']] == [15]
    assert result['evidence_class'] == 'kernel_and_registered_process_lifecycle_only'


@pytest.mark.parametrize('kind', ['missing_dns', 'unknown_child', 'lost_event', 'missing_image',
                                  'fake_console', 'missing_application', 'late_exit', 'duplicate_native'])
def test_incomplete_process_census_is_not_an_isolation_pass(tmp_path, kind):
    snapshot, receipts, launch, console = case(tmp_path)
    if kind == 'missing_dns':
        launch['attempts'].pop()
    elif kind == 'unknown_child':
        receipts[10]['native_process_inventory']['attempts'].clear()
    elif kind == 'lost_event':
        snapshot['events'] = [event for event in snapshot['events'] if event['pid'] != 13]
    elif kind == 'missing_image':
        snapshot['process_images'].pop()
    elif kind == 'fake_console':
        snapshot['process_images'][-1]['image_path'] = str(tmp_path / 'untrusted' / 'conhost.exe')
    elif kind == 'missing_application':
        launch['attempts'].pop(0)
    elif kind == 'late_exit':
        snapshot['active_processes'] = 1
    elif kind == 'duplicate_native':
        receipts[13]['mihomo_processes'].append({'pid': 12})
    assert audit_process_census(snapshot, receipts, launch, console_host_path=console)['failures']


def test_census_cannot_mutate_application_or_kernel_evidence(tmp_path):
    snapshot, receipts, launch, console = case(tmp_path)
    before = copy.deepcopy((snapshot, receipts, launch))
    audit_process_census(snapshot, receipts, launch, console_host_path=console)
    assert (snapshot, receipts, launch) == before


def test_command_pid_needs_its_own_registration_and_final_exit(tmp_path):
    snapshot, receipts, launch, console = case(tmp_path)
    query = receipts[10]['native_process_inventory']['attempts'].pop()
    receipts[10]['command_process_inventory'] = dict(schema=1, attempts=[query])
    assert not audit_process_census(snapshot, receipts, launch, console_host_path=console)['failures']
    query['exit_confirmed'] = False
    assert 'isolation_core_exit_unproven:12' in audit_process_census(
        snapshot, receipts, launch, console_host_path=console)['failures']


def test_command_receipt_does_not_remove_unregistered_short_lived_process(tmp_path):
    snapshot, receipts, launch, console = case(tmp_path)
    query = receipts[10]['native_process_inventory']['attempts'].pop()
    receipts[10]['command_process_inventory'] = dict(schema=1, attempts=[dict(query, pid=99)])
    failures = audit_process_census(snapshot, receipts, launch, console_host_path=console)['failures']
    assert 'job_process_unregistered:12' in failures
    assert 'job_registered_process_unobserved:99' in failures


@pytest.mark.parametrize('location', ['receipts', 'children', 'native_attempts', 'events', 'images'])
def test_partial_json_is_a_recorded_failure_not_an_exception(tmp_path, location):
    snapshot, receipts, launch, console = case(tmp_path)
    if location == 'receipts':
        receipts = None
    elif location == 'children':
        receipts[13]['mihomo_processes'] = None
    elif location == 'native_attempts':
        receipts[10]['native_process_inventory']['attempts'] = None
    elif location == 'events':
        snapshot['events'] = None
    else:
        snapshot['process_images'] = None
    assert audit_process_census(snapshot, receipts, launch, console_host_path=console)['failures']
