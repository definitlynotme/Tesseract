"""Local process-only comparison; not a Tier3 performance acceptance test."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Diagnostic-only single-variable import comparison, inherited by spawn.
if '--os-name-from-runtime' in sys.argv and sys.platform == 'win32':
    if os.environ.get('TESSERACT_ISOLATED_AUDIT_MANIFEST'):
        raise RuntimeError('isolation proof cannot include an OS-name experiment')
    import platform
    platform.system = lambda: 'Windows'

# Source-process equivalent of the frozen entry hook, including spawned copies.
if os.environ.get('TESSERACT_ISOLATED_AUDIT_MANIFEST'):
    from scripts.tier3_audit_entry import install_from_environment
    install_from_environment()


def traced_native_bootstrap(connection):
    import faulthandler
    from tier3_validator_helper import helper_process_bootstrap
    folder = Path(os.environ['TESSERACT_HELPER_STACK_DIRECTORY'])
    with (folder / f'helper-{os.getpid()}.txt').open('w', encoding='utf-8') as output:
        faulthandler.dump_traceback_later(3, repeat=True, file=output)
        try:
            helper_process_bootstrap(connection)
        finally:
            faulthandler.cancel_dump_traceback_later()


def main() -> int:
    from tier3_validator_supervisor import Tier3ValidatorSupervisor, Tier3WorkerStarter
    from test_support.tier3_deterministic_helper import deterministic_no_child_bootstrap, deterministic_helper_bootstrap
    parser = argparse.ArgumentParser()
    parser.add_argument('--bulkhead', type=int, required=True)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--query-version', action='store_true')
    parser.add_argument('--query-platform-version', action='store_true',
                        help='Observe the original Windows version command and its child receipt')
    parser.add_argument('--release-on-prewarm', action='store_true',
                        help='Diagnostic A/B: release launch slots after verified owned Popen, not READY')
    parser.add_argument('--wait-initial-ready', action='store_true',
                        help='Diagnostic A/B: hold later shells until the initial four are READY')
    parser.add_argument('--os-name-from-runtime', action='store_true',
                        help='Diagnostic A/B: resolve only OS name without WMI')
    parser.add_argument('--workers', type=int, choices=range(1, 17), default=8)
    args = parser.parse_args()
    auditing = bool(os.environ.get('TESSERACT_ISOLATED_AUDIT_MANIFEST'))
    if auditing and (args.release_on_prewarm or args.wait_initial_ready or args.os_name_from_runtime):
        parser.error('isolation proof cannot be combined with startup policy experiments')
    if args.query_platform_version:
        import platform
        platform._syscmd_ver()
    if args.query_version:
        from core_runner import MihomoCoreRunner
        root = Path(__file__).resolve().parents[1]
        # Exercise the ordinary binary-identity cache as well as the short-lived
        # native process. No runtime Core/configuration is started here.
        for _ in range(2):
            runner = MihomoCoreRunner(binary_path=str(root / 'bin/mihomo.exe'),
                runtime_config_path=str(root / 'unused-audit-version.yaml'), project_root=root,
                work_dir=os.environ.get('TESSERACT_DATA_DIR', str(root)))
            runner.get_version()
    durations = []
    events = []
    origin = time.monotonic()
    original = Tier3WorkerStarter._spawn

    def spawn(starter, worker_id, generation):
        start = time.monotonic()
        try:
            worker = original(starter, worker_id, generation)
            events.append(dict(worker=worker_id, pid=worker.helper_pid,
                               phase='spawn_returned', at=time.monotonic() - origin))
            return worker
        finally:
            durations.append(dict(worker=worker_id, generation=generation,
                                  elapsed=time.monotonic() - start))

    if not auditing:
        Tier3WorkerStarter._spawn = spawn
    original_progress = Tier3ValidatorSupervisor._handle_startup_progress
    original_ready = Tier3ValidatorSupervisor._handle_ready
    original_send = Tier3ValidatorSupervisor._send

    def progress(owner, worker, payload):
        events.append(dict(worker=worker.worker_id, phase=payload['phase'],
                           at=time.monotonic() - origin,
                           reported_at=payload.get('monotonic_ns', 0) / 1e9 - origin))
        return original_progress(owner, worker, payload)

    def ready(owner, worker, payload):
        events.append(dict(worker=worker.worker_id, phase='ready_received', at=time.monotonic() - origin))
        return original_ready(owner, worker, payload)

    def send(owner, worker, kind, payload):
        result = original_send(owner, worker, kind, payload)
        if kind == 'start':
            events.append(dict(worker=worker.worker_id, phase='start_sent', at=time.monotonic() - origin))
        return result

    Tier3ValidatorSupervisor._handle_startup_progress = progress
    Tier3ValidatorSupervisor._handle_ready = ready
    if not auditing:
        Tier3ValidatorSupervisor._send = send
    if args.release_on_prewarm:
        from tier3_validation_protocol import Tier3WorkerPhase

        original_activate = Tier3ValidatorSupervisor._activate_waiting_workers

        def activate(owner):
            # Test-only: remove completed, Job-verified Popen work from the
            # launch semaphore. Worker capacity still requires real READY.
            with owner._lock:
                importing = sum(worker.phase is Tier3WorkerPhase.STARTING
                                and worker.prewarm_started
                                for worker in owner._workers.values())
                bound = owner.mihomo_start_bulkhead
                owner.mihomo_start_bulkhead = bound + importing
            try:
                return original_activate(owner)
            finally:
                with owner._lock:
                    owner.mihomo_start_bulkhead = bound

        Tier3ValidatorSupervisor._activate_waiting_workers = activate
    if args.wait_initial_ready:
        original_schedule = Tier3ValidatorSupervisor._schedule_worker_spawn

        def schedule(owner, worker_id):
            with owner._lock:
                if worker_id > min(4, owner._desired_workers) and not owner._initial_cohort_ready_unlocked():
                    return False
            return original_schedule(owner, worker_id)

        Tier3ValidatorSupervisor._schedule_worker_spawn = schedule
    from tier3_validator_helper import helper_process_bootstrap
    target = (helper_process_bootstrap if args.native else
              deterministic_helper_bootstrap if args.child else deterministic_no_child_bootstrap)
    if args.native and os.environ.get('TESSERACT_HELPER_STACK_DIRECTORY'):
        if auditing:
            parser.error('isolation proof cannot include a diagnostic helper target')
        target = traced_native_bootstrap
    supervisor = Tier3ValidatorSupervisor(
        binary_path=str(Path(__file__).resolve().parents[1] / 'bin/mihomo.exe') if args.native else 'fixture',
        probe_urls=['http://fixture/204'], expected_statuses={204},
        probe_timeout_seconds=5, slow_threshold_ms=3000, min_workers=args.workers, max_workers=args.workers,
        startup_bulkhead=args.bulkhead,
        helper_target=target,
        capacity_sink=lambda _count: None, attempt_sink=lambda _value: None,
    )
    start = origin = time.monotonic()
    supervisor.start()
    deadline = start + 12
    while time.monotonic() < deadline and supervisor.snapshot().active_workers < args.workers:
        time.sleep(.02)
    before = supervisor.snapshot()
    elapsed = time.monotonic() - start
    status = supervisor.stop(timeout_seconds=4)
    print(json.dumps(dict(bulkhead=args.bulkhead, workers=args.workers, native=args.native,
                          release_on_prewarm=args.release_on_prewarm, ready_seconds=elapsed,
                          wait_initial_ready=args.wait_initial_ready,
                          os_name_from_runtime=args.os_name_from_runtime,
                          active=before.active_workers, pending=before.pending_spawn_workers,
                          phases=[dict(worker=w['worker_id'], phase=w['startup_phase']) for w in before.workers],
                          failures=before.failure_reason_counts, spawn=durations,
                          events=events, stop=status)), flush=True)
    return 0 if before.active_workers == args.workers and status == 'STOPPED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
