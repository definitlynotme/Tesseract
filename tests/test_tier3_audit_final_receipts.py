from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.tier3_audit_socket_policy import physical_process_receipt_failures


def receipts():
    common = dict(interface='physical', socket_guard_installed=True, completed=True,
                  audit_id='a' * 32, manifest_sha256='b' * 64,
                  native_process_inventory=dict(schema=1, attempts=[]))
    attempt = dict(attempt_id='helper-1-1:1', pid=2, launch_finished=True,
                   job_process_ids=[2, 20], start_sent=True, exit_confirmed=True, exitcode=0)
    values = {
        1: dict(common, process_id=1, rendered=1,
                process_inventory=dict(schema=2, attempts=[attempt])),
        2: dict(common, process_id=2, received_prewarm=1,
                mihomo_processes=[dict(pid=20, helper_pid=2, exitcode=0)]),
    }
    values[1]['native_process_inventory'] = dict(schema=1, attempts=[dict(
        attempt_id='core:1', pid=10, creation_attempted=True, launch_finished=True,
        configs=[dict(sha256='c' * 64)], executable_sha256='d' * 64,
        exit_confirmed=True, exitcode=0,
    )])
    return values


def check(value):
    return physical_process_receipt_failures(value, parent_pid=1, interface='physical')


def test_final_same_manifest_receipts_include_native_job_member_exit():
    assert not check(receipts())


@pytest.mark.parametrize('pid', [1, 2])
@pytest.mark.parametrize('field,value', [
    ('completed', False), ('completed', None), ('manifest_sha256', 'c' * 64),
    ('audit_id', 'd' * 32),
])
def test_stale_or_unfinished_receipt_cannot_prove_isolation(pid, field, value):
    values = receipts()
    values[pid][field] = value
    assert check(values)


@pytest.mark.parametrize('change', ['missing', 'unexited', 'not_in_job', 'wrong_owner'])
def test_helper_exit_does_not_prove_native_child_exit(change):
    values = receipts()
    child = values[2]['mihomo_processes'][0]
    if change == 'missing':
        values[2]['mihomo_processes'] = []
    elif change == 'unexited':
        child['exitcode'] = None
    elif change == 'not_in_job':
        child['pid'] = 30
    else:
        child['helper_pid'] = 3
    assert check(values)


def test_pid_reuse_cannot_merge_two_generation_receipts():
    values = receipts()
    repeated = deepcopy(values[1]['process_inventory']['attempts'][0])
    repeated['attempt_id'] = 'helper-1-2:2'
    values[1]['process_inventory']['attempts'].append(repeated)
    assert check(values)


def test_legacy_pid_list_cannot_prove_completed_native_process_inventory():
    values = receipts()
    values[1].pop('process_inventory')
    values[1]['spawned_helper_pids'] = [2]
    assert check(values)


@pytest.mark.parametrize('change', ['missing', 'no_core', 'unexited', 'helper_pid', 'native_pid'])
def test_main_core_cannot_be_omitted_or_confused_with_helper_child(change):
    values = receipts()
    item = values[1]['native_process_inventory']['attempts'][0]
    if change == 'missing':
        values[1].pop('native_process_inventory')
    elif change == 'no_core':
        values[1]['native_process_inventory']['attempts'] = []
    elif change == 'unexited':
        item['exitcode'] = None
    elif change == 'helper_pid':
        item['pid'] = 2
    else:
        item['pid'] = 20
    assert check(values)
