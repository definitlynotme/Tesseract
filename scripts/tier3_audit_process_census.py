"""Cross-check independent kernel Job evidence against application launch logs.

Passing this check proves lifecycle coverage only. Socket and packet-path
checks remain separate and mandatory for performance acceptance.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from scripts.tier3_audit_job_inventory import job_inventory_failures
from scripts.tier3_audit_process_registry import native_process_inventory_failures


def audit_process_census(job_snapshot, receipts, launch_inventory, *, console_host_path):
    """Reconcile all processes; only the exact OS console host is auxiliary.

    ``console_host_path`` comes from the launcher, not from a child receipt.
    Python, Core, DNS and version-query processes all need explicit receipts.
    Merely having an executable name on an allowlist is not registration.
    """
    failures = native_process_inventory_failures(launch_inventory)
    registered, roles = set(), set()

    def add(pid):
        if type(pid) is not int or pid <= 0:
            failures.append('process_census_invalid_pid')
        elif pid in registered:
            failures.append(f'process_census_duplicate_registration:{pid}')
        else:
            registered.add(pid)

    attempts = launch_inventory.get('attempts', []) if isinstance(launch_inventory, Mapping) else []
    for attempt in attempts if isinstance(attempts, list) else ():
        if not isinstance(attempt, Mapping):
            continue
        role = attempt.get('role')
        if role not in {'application', 'resolver'}:
            failures.append('process_census_invalid_launcher_role')
        if attempt.get('pid') is not None:
            add(attempt['pid'])
            roles.add(role)
    if roles != {'application', 'resolver'}:
        failures.append('process_census_launcher_roles_unproven')

    if not isinstance(receipts, Mapping) or not receipts:
        failures.append('process_census_receipts_missing')
        receipts = {}
    for pid, receipt in receipts.items():
        if not isinstance(receipt, Mapping) or receipt.get('process_id') != pid:
            failures.append('process_census_receipt_invalid')
            continue
        violations = receipt.get('command_policy_violation_count', 0)
        if type(violations) is not int or violations != 0:
            failures.append(f'process_census_command_policy_violated:{pid}')
        # An application receipt can describe the exact process launched by the
        # controller or the interpreter spawned by the Windows venv redirector.
        if pid not in registered:
            add(pid)
        children = receipt.get('mihomo_processes')
        if not isinstance(children, list):
            failures.append('process_census_children_invalid')
            children = []
        for child in children:
            add(child.get('pid') if isinstance(child, Mapping) else None)
        inventory = receipt.get('native_process_inventory', {})
        failures.extend(native_process_inventory_failures(inventory))
        native_attempts = inventory.get('attempts') if isinstance(inventory, Mapping) else None
        for attempt in native_attempts if isinstance(native_attempts, list) else ():
            if isinstance(attempt, Mapping) and attempt.get('pid') is not None:
                add(attempt['pid'])
        commands = receipt.get('command_process_inventory', dict(schema=1, attempts=[]))
        failures.extend(native_process_inventory_failures(commands))
        command_attempts = commands.get('attempts') if isinstance(commands, Mapping) else None
        for attempt in command_attempts if isinstance(command_attempts, list) else ():
            if isinstance(attempt, Mapping) and attempt.get('pid') is not None:
                add(attempt['pid'])

    images = job_snapshot.get('process_images', []) if isinstance(job_snapshot, Mapping) else []
    if (not isinstance(job_snapshot, Mapping) or job_snapshot.get('image_observation_enabled') is not True
            or not isinstance(images, list)):
        failures.append('process_census_images_unproven')
        images = []
    image_pids, auxiliary = set(), []
    expected_console = os.path.normcase(str(Path(console_host_path).resolve()))
    for image in images:
        if (not isinstance(image, Mapping) or type(image.get('pid')) is not int
                or image['pid'] <= 0 or image['pid'] in image_pids
                or type(image.get('created_at_filetime')) is not int or image['created_at_filetime'] <= 0
                or not isinstance(image.get('image_path'), str) or not Path(image['image_path']).is_absolute()):
            failures.append('process_census_image_invalid')
            continue
        pid = image['pid']
        image_pids.add(pid)
        if (pid not in registered
                and os.path.normcase(str(Path(image['image_path']).resolve())) == expected_console):
            add(pid)
            auxiliary.append(dict(image))
    events = job_snapshot.get('events') if isinstance(job_snapshot, Mapping) else None
    births = {event.get('pid') for event in events
              if isinstance(event, Mapping) and event.get('message') == 6
              and type(event.get('pid')) is int} if isinstance(events, list) else set()
    if image_pids != births:
        failures.append('process_census_image_coverage_unproven')
    failures.extend(job_inventory_failures(job_snapshot, registered_pids=registered))
    return dict(evidence_class='kernel_and_registered_process_lifecycle_only',
                failures=failures, registered_pids=sorted(registered),
                os_auxiliary_processes=auxiliary)
