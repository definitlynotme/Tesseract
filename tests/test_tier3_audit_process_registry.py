from __future__ import annotations

import random

from scripts.tier3_audit_process_registry import AuditProcessRegistry, process_inventory_failures


def receipt(pid, *, prewarm=1):
    return dict(process_id=pid, interface='physical', socket_guard_installed=True,
                received_prewarm=prewarm)


def test_failed_launch_and_replacement_remain_in_inventory():
    published = []
    registry = AuditProcessRegistry(published.append)
    first = registry.begin('helper-1-1')
    registry.launched(first, 10, 'JobAssignmentError')
    registry.exited(first, 74)
    second = registry.begin('helper-1-2')
    registry.launched(second, 11, None)
    registry.job_assigned(11, [11])
    registry.start_sent(11)
    registry.exited(second, 0)
    assert len(registry.snapshot()['attempts']) == 2
    assert not process_inventory_failures(
        registry.snapshot(), {10: receipt(10, prewarm=0), 11: receipt(11)}, interface='physical')
    assert 'isolation_process_unproven:10' in process_inventory_failures(
        registry.snapshot(), {11: receipt(11)}, interface='physical')
    assert published[0]['attempts'][0]['pid'] is None


def test_late_exit_cannot_be_forged_from_empty_job_or_prestart_receipt():
    registry = AuditProcessRegistry(lambda _value: None)
    key = registry.begin('helper-1-1')
    registry.launched(key, 10, None)
    registry.job_assigned(10, [10])
    registry.start_sent(10)
    registry.exited(key, None)
    failures = process_inventory_failures(registry.snapshot(), {10: receipt(10, prewarm=0)},
                                         interface='physical')
    assert 'isolation_process_exit_unproven:10' in failures
    assert 'isolation_config_unproven:10' in failures
    registry.exited(key, 0)
    assert not process_inventory_failures(registry.snapshot(), {10: receipt(10)},
                                         interface='physical')


def test_1000_failed_started_cancelled_and_replaced_process_attempts():
    rng = random.Random(919)
    registry = AuditProcessRegistry(lambda _value: None)
    receipts = {}
    unproven_launches = set()
    for index in range(1000):
        key = registry.begin(f'helper-{index % 16}-{index // 16}')
        case = rng.randrange(4)
        if case == 0:
            registry.launched(key, None, 'ProcessCreationError')
            unproven_launches.add(f'isolation_launch_pid_unproven:{key}')
            continue
        pid = 1000 + index
        registry.launched(key, pid, None if case > 1 else 'JobAssignmentError')
        receipts[pid] = receipt(pid, prewarm=int(case > 1))
        if case > 1:
            registry.job_assigned(pid, [pid])
            registry.start_sent(pid)
        registry.exited(key, 0 if case == 3 else 74)
        registry.exited(key, 0 if case == 3 else 74)
    assert len(registry.snapshot()['attempts']) == 1000
    assert set(process_inventory_failures(registry.snapshot(), receipts, interface='physical')) == unproven_launches
    missing_pid = next(iter(receipts))
    del receipts[missing_pid]
    assert f'isolation_process_unproven:{missing_pid}' in process_inventory_failures(
        registry.snapshot(), receipts, interface='physical')


def test_failed_process_start_without_pid_cannot_prove_no_child_was_created():
    registry = AuditProcessRegistry(lambda _value: None)
    key = registry.begin('helper-1-1')
    registry.launched(key, None, 'ProcessCreationError')
    assert process_inventory_failures(registry.snapshot(), {}, interface='physical') == [
        f'isolation_launch_pid_unproven:{key}',
    ]


def test_single_source_scope_does_not_waive_failed_helper_evidence():
    registry = AuditProcessRegistry(lambda _value: None)
    assert not process_inventory_failures(registry.snapshot(), {}, interface='physical',
                                         require_helpers=False)
    key = registry.begin('unexpected-helper')
    registry.launched(key, 10, 'StartupFailure')
    registry.exited(key, 74)
    assert process_inventory_failures(registry.snapshot(), {}, interface='physical',
                                     require_helpers=False)
