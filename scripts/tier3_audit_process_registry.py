from __future__ import annotations

"""Audit-only inventory. A failed launch remains evidence, never disappears."""

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


class AuditNativeProcessRegistry:
    """Account for native Core attempts independently of helper Job ownership."""

    def __init__(self, publish: Callable[[dict[str, Any]], None]) -> None:
        self._publish = publish
        self._lock = threading.RLock()
        self._attempts: dict[str, dict[str, Any]] = {}

    def begin(self) -> str:
        with self._lock:
            key = f"core:{len(self._attempts) + 1}"
            self._attempts[key] = dict(
                attempt_id=key, requested_at=time.monotonic(),
                creation_attempted=False, launch_finished=False, pid=None,
                error=None, configs=[], executable_sha256=None,
                exitcode=None, exit_confirmed=False,
            )
            self._emit()
            return key

    def configured(self, key: str, config_sha256: str, executable_sha256: str) -> None:
        with self._lock:
            item = self._attempts[key]
            if item['executable_sha256'] not in (None, executable_sha256):
                raise ValueError('audit Core executable changed after launch')
            item['configs'].append(dict(sha256=config_sha256, checked_at=time.monotonic()))
            item['executable_sha256'] = executable_sha256
            self._emit()

    def creating(self, key: str) -> None:
        with self._lock:
            self._attempts[key]['creation_attempted'] = True
            self._emit()

    def launched(self, key: str, pid: int | None, error: str | None) -> None:
        with self._lock:
            self._attempts[key].update(
                launch_finished=True, pid=pid, error=error, launched_at=time.monotonic(),
            )
            self._emit()

    def exited(self, key: str, exitcode: int | None) -> None:
        if exitcode is None:
            return
        with self._lock:
            item = self._attempts[key]
            if item['exit_confirmed'] and item['exitcode'] == exitcode:
                return
            item.update(exit_confirmed=True, exitcode=exitcode)
            self._emit()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(schema=1, attempts=[
                {**item, 'configs': [dict(config) for config in item['configs']]}
                for item in self._attempts.values()
            ])

    def _emit(self) -> None:
        self._publish(self.snapshot())


def native_process_inventory_failures(inventory: Any, *, other_pids=()) -> list[str]:
    if not isinstance(inventory, Mapping) or inventory.get('schema') != 1:
        return ['isolation_core_inventory_unproven']
    attempts = inventory.get('attempts')
    if not isinstance(attempts, list):
        return ['isolation_core_inventory_unproven']
    failures = []
    seen_keys, seen_pids = set(), set(other_pids)

    def digest(value):
        return (isinstance(value, str) and len(value) == 64
                and all(c in '0123456789abcdef' for c in value))

    for item in attempts:
        if (not isinstance(item, Mapping) or not isinstance(item.get('attempt_id'), str)
                or item['attempt_id'] in seen_keys or item.get('launch_finished') is not True):
            failures.append('isolation_core_launch_unaccounted')
            continue
        key = item['attempt_id']
        seen_keys.add(key)
        pid = item.get('pid')
        if pid is None:
            # Only a rejection before the OS launch call proves no child.
            if item.get('creation_attempted') is not False or not item.get('error'):
                failures.append(f'isolation_core_launch_pid_unproven:{key}')
            continue
        if type(pid) is not int or pid <= 0 or pid in seen_pids:
            failures.append(f'isolation_core_pid_unproven:{key}')
            continue
        seen_pids.add(pid)
        configs = item.get('configs')
        if (item.get('creation_attempted') is not True
                or not digest(item.get('executable_sha256'))
                or not isinstance(configs, list) or not configs
                or any(not isinstance(config, Mapping) or not digest(config.get('sha256'))
                       for config in configs)):
            failures.append(f'isolation_core_config_unproven:{pid}')
        if item.get('exit_confirmed') is not True or type(item.get('exitcode')) is not int:
            failures.append(f'isolation_core_exit_unproven:{pid}')
    return failures


class AuditProcessRegistry:
    def __init__(self, publish: Callable[[dict[str, Any]], None]) -> None:
        self._publish = publish
        self._lock = threading.RLock()
        self._attempts: dict[str, dict[str, Any]] = {}
        self._sequence = 0

    def begin(self, name: str) -> str:
        with self._lock:
            self._sequence += 1
            key = f"{name}:{self._sequence}"
            self._attempts[key] = dict(
                attempt_id=key, name=name, requested_at=time.monotonic(),
                launch_finished=False, pid=None, error=None, job_process_ids=[],
                start_sent=False, exit_confirmed=False, exitcode=None,
            )
            self._emit()
            return key

    def launched(self, key: str, pid: int | None, error: str | None) -> None:
        with self._lock:
            item = self._attempts[key]
            item.update(launch_finished=True, pid=pid, error=error, launched_at=time.monotonic())
            self._emit()

    def job_assigned(self, pid: int, members: list[int]) -> None:
        with self._lock:
            item = self._for_pid(pid)
            item['job_process_ids'] = sorted(set(members))
            self._emit()

    def start_sent(self, pid: int) -> None:
        with self._lock:
            self._for_pid(pid)['start_sent'] = True
            self._emit()

    def exited(self, key: str, exitcode: int | None) -> None:
        if exitcode is None:
            return
        with self._lock:
            self._attempts[key].update(exit_confirmed=True, exitcode=exitcode)
            self._emit()

    def _for_pid(self, pid: int) -> dict[str, Any]:
        for item in reversed(tuple(self._attempts.values())):
            if item['pid'] == pid:
                return item
        raise ValueError('audit observed an unregistered child')

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(schema=2, attempts=[
                {**item, 'job_process_ids': list(item['job_process_ids'])}
                for item in self._attempts.values()
            ])

    def _emit(self) -> None:
        self._publish(self.snapshot())


def process_inventory_failures(
    inventory: Mapping[str, Any], receipts: Mapping[int, Mapping[str, Any]],
    *, interface: str, require_helpers: bool = True,
) -> list[str]:
    attempts = inventory.get('attempts')
    if inventory.get('schema') != 2 or not isinstance(attempts, list):
        return ['isolation_process_inventory_unproven']
    if require_helpers and not attempts:
        return ['isolation_helper_inventory_unproven']
    failures: list[str] = []
    seen = set()
    seen_pids = set()
    for item in attempts:
        if not isinstance(item, Mapping):
            failures.append('isolation_launch_unaccounted')
            continue
        key = item.get('attempt_id')
        if not isinstance(key, str) or key in seen or not item.get('launch_finished'):
            failures.append('isolation_launch_unaccounted')
            continue
        seen.add(key)
        pid = item.get('pid')
        if pid is None:
            if not item.get('error'):
                failures.append(f'isolation_launch_unaccounted:{key}')
            else:
                # Process.start can fail after OS creation but before Python
                # publishes a PID. The exception is not proof of no child.
                failures.append(f'isolation_launch_pid_unproven:{key}')
            continue
        if type(pid) is not int or pid <= 0:
            failures.append(f'isolation_invalid_pid:{key}')
            continue
        if pid in seen_pids:
            failures.append(f'isolation_process_pid_reused:{pid}')
        seen_pids.add(pid)
        receipt = receipts.get(pid, {})
        if (receipt.get('process_id') != pid
                or receipt.get('interface') != interface
                or receipt.get('socket_guard_installed') is not True):
            failures.append(f'isolation_process_unproven:{pid}')
        if item.get('exit_confirmed') is not True or type(item.get('exitcode')) is not int:
            failures.append(f'isolation_process_exit_unproven:{pid}')
        if item.get('start_sent'):
            if pid not in item.get('job_process_ids', ()):
                failures.append(f'isolation_job_assignment_unproven:{pid}')
            if (type(receipt.get('received_prewarm')) is not int
                    or receipt['received_prewarm'] <= 0):
                failures.append(f'isolation_config_unproven:{pid}')
        elif receipt.get('received_prewarm', 0):
            failures.append(f'isolation_unregistered_start:{pid}')
    return failures
