"""Sidecar audit boundary; installation is deliberately separate from startup.

Frozen root AND helper processes still need tier3_audit_runtime_hook before
freeze_support. P1-16 wires attach_audit_guard before RT-1 and invokes
start_startup_audit only at the actual audit action (never hello/pipe setup).

T-P2-32 adds the production entry: ``prepare_startup_audit`` (before RT-1,
refuses without the guard or with a mismatched frozen identity),
``StartupAudit`` (wraps the real runtime start, samples off the publication
tick on its own thread) and the CPU accounting format of ASK-02 section 6.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from atomic_file import replace_with_retry


def attach_audit_guard(scope):
    """Validate and reuse the runtime-hook guard; never emit startup markers.

    Returns the guard for an explicit finish() at the real process-exit
    boundary. Normal atexit and helper BaseProcess.run finalizers remain active.
    Non-audit runs do not import or install the audit stack.
    """
    manifest_path = os.environ.get('TESSERACT_ISOLATED_AUDIT_MANIFEST')
    if not manifest_path:
        if scope.kind == 'audit':
            raise PermissionError('audit scope requires an isolation manifest')
        return None
    if scope.kind != 'audit' or scope.may_take_over_host_network:
        raise PermissionError('audit requires a non-mutating audit scope')
    from scripts import tier3_audit_entry as entry

    manifest = entry.read_manifest(manifest_path, os.environ)
    if (scope.id != manifest['audit_id']
            or scope.config_dir is None or scope.data_dir is None
            or Path(scope.config_dir).resolve(strict=True) != Path(manifest['config_dir'])
            or Path(scope.data_dir).resolve(strict=True) != Path(manifest['data_dir'])):
        raise PermissionError('audit scope does not match the isolation manifest')
    # Never repair a missing hook here: freeze_support and early imports have
    # already run. Bind hook evidence to this guard and this process.
    guard = entry._guard
    if guard is None or getattr(guard, '_runtime_hook_pid', None) != os.getpid():
        raise PermissionError('audit runtime-hook guard is missing')
    if (guard is None or guard.manifest != manifest
            or not guard.socket_guard_installed()):
        raise PermissionError('audit guard is missing, stale or incomplete')
    return guard


def start_startup_audit(scope, action, /, *args, **kwargs):
    """Enter the real root audit action, preserving legacy control-file fields.

    Markers mean action entry, not startup success. Each file is atomically
    replaced; the pair is not a transaction. A publish failure aborts the action.
    The caller owns one audit run per fresh isolated directory.
    """
    if not callable(action):
        raise TypeError('audit startup action must be callable')
    if (multiprocessing.current_process().name != 'MainProcess'
            or any(arg.split('=', 1)[0] in {
                '--multiprocessing-fork', '--validator-helper-smoke',
                '--validator-helper-slots'} for arg in sys.argv[1:])):
        raise PermissionError('helpers cannot publish root audit startup markers')
    guard = attach_audit_guard(scope)
    if guard is None:
        raise PermissionError('audit startup requires an isolation manifest')
    # T-P2-32: a frozen sidecar must match its declared build before any marker.
    require_build_identity()
    marker = dict(window_started_monotonic=time.monotonic(),
                  process_id=os.getpid(), parent_process_id=os.getppid(),
                  audit_id=guard.manifest['audit_id'],
                  manifest_sha256=guard.manifest['manifest_sha256'])
    with guard.lock:
        for filename in ('window-start-control.json', 'child-process-control.json'):
            destination = Path(guard.manifest['data_dir'], filename)
            pending = destination.with_suffix('.pending')
            pending.write_bytes(json.dumps(marker).encode('utf-8'))
            replace_with_retry(pending, destination)
    # The action runs synchronously on this thread; it may read the exact
    # marker origin through current_startup_marker() (never a file re-read).
    _marker_local.value = dict(marker)
    try:
        return action(*args, **kwargs)
    finally:
        _marker_local.value = None


_marker_local = threading.local()


def current_startup_marker() -> dict | None:
    """The marker published for the action running on this thread, if any."""
    value = getattr(_marker_local, 'value', None)
    return dict(value) if value is not None else None


# ---------------------------------------------------------------------------
# T-P2-32: production startup-audit entry, CPU accounting and audit output.
# PROTOCOL: audit.startup is an internal guarded action, never an IPC command.
# ---------------------------------------------------------------------------

MANIFEST_ENV = 'TESSERACT_ISOLATED_AUDIT_MANIFEST'
STARTUP_AUDIT_PREFIX = 'TESSERACT_STARTUP_AUDIT_'
STARTUP_AUDIT_PATH_ENV = 'TESSERACT_STARTUP_AUDIT_PATH'
STARTUP_AUDIT_DURATION_ENV = 'TESSERACT_STARTUP_AUDIT_DURATION_MS'
STARTUP_AUDIT_INTERVAL_ENV = 'TESSERACT_STARTUP_AUDIT_INTERVAL_MS'
STARTUP_AUDIT_QUIT_ENV = 'TESSERACT_STARTUP_AUDIT_QUIT'
STARTUP_AUDIT_ACTION_ENV = 'TESSERACT_STARTUP_AUDIT_ACTION'
# '' waits for the host's real service.start; 'start-services' (the legacy Qt
# value) lets the audit run that same start itself; 'idle' samples the bridge
# from hello without any start, so it never publishes startup markers.
STARTUP_AUDIT_ACTIONS = frozenset({'', 'start-services', 'idle'})
STARTUP_AUDIT_SCHEMA = 'tesseract-sidecar-startup-audit-v1'
DEFAULT_AUDIT_FILENAME = 'sidecar-startup-audit.json'
ACTION_MARKER_FILENAME = 'startup-action-control.json'
CPU_WINDOWS_SECONDS = ((0, 30), (30, 60), (60, 120), (120, 150))
THREAD_CLASSES = ('bridge', 'transaction', 'audit', 'runtime')
COMPONENT_KEYS = ('publisher_thread_cpu_ns', 'state_worker_thread_cpu_ns',
                  'logs_worker_thread_cpu_ns', 'logs_stream_thread_cpu_ns',
                  'traffic_worker_thread_cpu_ns', 'log_work_cpu_ns',
                  'logging_handler_cpu_ns', 'producer_cpu_ns', 'log_publication_cpu_ns')
CPU_DEFINITION = {
    'publisher_cores': 'delta publisher_thread_cpu_ns / delta elapsed_monotonic_ns '
                       '(Publisher tick CPU on the owner thread; Windows thread time is coarse)',
    'bridge_cores': '(delta bridge-class thread lifetime CPU + delta logs.work_cpu_ns) / '
                    'delta elapsed; the gate value. Over-counts on purpose (log work on bridge '
                    'threads counts twice), but it is NOT a strict upper bound: only threads '
                    'seen alive by at least one sample are attributed, so a bridge thread that '
                    'starts and exits between two samples (or whose OpenThread failed) is '
                    'missed; that CPU appears in unclassified_cores. None (with '
                    'upper_incomplete=true) when the window lacks the log-work counter at '
                    'either boundary; never silently equal to bridge_cores_lower',
    'bridge_cores_lower': 'delta bridge-class thread lifetime CPU / delta elapsed; disjoint '
                          'threads only, excludes log producer callbacks on runtime threads',
    'unclassified_cores': '(delta process CPU - delta sum of the four thread classes) / '
                          'delta elapsed; cross-check for CPU no class saw: short-lived or '
                          'unopenable Python threads and native (non-Python) threads. Both '
                          'clocks tick at about 15.6 ms, so small negative values are noise',
    'unit': 'cores of one logical processor; not divided by processor count; '
            '0.1 core over 150 s is about 15 s CPU',
    'classes': {
        'bridge': 'owner loop (bridge-main: ingress pump, publisher tick, transport writes), '
                  'sampler, log batches/stream, traffic, ingress, pipe, normal/sampling/'
                  'cancel/receipt executor workers, onboarding expiry, stdio reader',
        'transaction': 'executor control/change/session-end workers, bridge-audit-control '
                       '(runtime start/stop work), bridge-network-recovery, '
                       'bridge-settings-apply and bridge-exit-* (exit-quality probes run '
                       'runtime work)',
        'audit': 'other bridge-audit-* threads (this measurement)',
        'runtime': 'every other Python thread of the process'},
    'thread_counters': 'tracked = threads attributed so far; open_failures = threads seen '
                       'alive whose OpenThread failed (never attributed); '
                       'untracked_live_threads = threads beyond the tracking bound',
    'components': 'callback/component counters from bridge_traffic.collection_metrics; '
                  'never summed with their own thread lifetime',
}
FIRSTS_DEFINITION = {
    'first_frame_after_origin_elapsed_ms': 'elapsed_ms of the first audit sample whose publisher '
                                           'frame counter exceeds the counter at the first sample '
                                           'after the origin; resolution is the sampling interval. '
                                           'With the default action frames already flow before '
                                           'the origin, so this is not a time-to-first-frame',
    'first_pool_node_elapsed_ms': 'first sample whose published tiers.summary pool.total > 0',
    'first_pool_usable_elapsed_ms': 'first sample whose published tiers.summary pool.usable > 0',
}
_DEFAULT_DURATION_MS = 150_000
_DEFAULT_INTERVAL_MS = 250
_WRITE_EVERY_SECONDS = 5.0
_SUBSCRIBE_WAIT_SECONDS = 2.0
_STOP_WAIT_SECONDS = 120.0
_FINISH_JOIN_SECONDS = 3.0
_MAX_SAMPLES = 20_000
_TRUE = frozenset({'1', 'true', 'yes', 'on'})
_FALSE = frozenset({'', '0', 'false', 'no', 'off'})
_IDENTITY_FIELDS = ('schema_version', 'build_id', 'declared_executable_sha256',
                    'executable_sha256', 'identity_match')


def startup_audit_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Only TESSERACT_STARTUP_AUDIT_* or the isolation manifest enable it."""
    env = os.environ if environ is None else environ
    if str(env.get(MANIFEST_ENV) or '').strip():
        return True
    return any(str(key).upper().startswith(STARTUP_AUDIT_PREFIX) and str(value).strip()
               for key, value in env.items())


@dataclass(frozen=True)
class StartupAuditSettings:
    path: Path | None
    duration_ms: int
    interval_ms: int
    quit_when_done: bool
    action: str


def _bounded_int(env, key, default, minimum, maximum):
    raw = str(env.get(key) or '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f'{key} must be an integer') from None
    if not minimum <= value <= maximum:
        raise ValueError(f'{key} must be within {minimum}..{maximum}')
    return value


def parse_startup_audit_settings(environ: Mapping[str, str] | None = None) -> StartupAuditSettings:
    """Pure parser shared by the sidecar and the lab dry-run; invalid input raises.

    Unlike the Qt window audit this never clamps silently: an audit with a
    surprising duration or action must refuse rather than measure something else.
    """
    env = os.environ if environ is None else environ
    raw_path = str(env.get(STARTUP_AUDIT_PATH_ENV) or '').strip()
    path = None
    if raw_path:
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError(f'{STARTUP_AUDIT_PATH_ENV} must be absolute')
    quit_raw = str(env.get(STARTUP_AUDIT_QUIT_ENV) or '').strip().lower()
    if quit_raw not in _TRUE | _FALSE:
        raise ValueError(f'{STARTUP_AUDIT_QUIT_ENV} must be a boolean')
    action = str(env.get(STARTUP_AUDIT_ACTION_ENV) or '').strip().lower()
    if action not in STARTUP_AUDIT_ACTIONS:
        raise ValueError('unsupported sidecar startup audit action')
    return StartupAuditSettings(
        path=path,
        duration_ms=_bounded_int(env, STARTUP_AUDIT_DURATION_ENV, _DEFAULT_DURATION_MS, 1000, 1_200_000),
        interval_ms=_bounded_int(env, STARTUP_AUDIT_INTERVAL_ENV, _DEFAULT_INTERVAL_MS, 50, 5000),
        quit_when_done=quit_raw in _TRUE, action=action)


def build_identity() -> dict:
    """Bridge build identity; a frozen identity hashes the sidecar EXE itself."""
    from tier3_build_identity import tier3_build_identity
    value = tier3_build_identity()
    record = {key: value.get(key) for key in _IDENTITY_FIELDS}
    record.update(frozen=bool(getattr(sys, 'frozen', False)),
                  executable=str(Path(sys.executable).resolve()))
    return record


def require_build_identity(identity: Mapping[str, Any] | None = None) -> None:
    """A frozen sidecar must match its declared build; source runs record only."""
    if identity is None:
        if not getattr(sys, 'frozen', False):
            return
        identity = build_identity()
    if identity.get('frozen') and identity.get('identity_match') is not True:
        raise PermissionError('frozen sidecar identity does not match its build declaration')


@dataclass(frozen=True)
class StartupAuditPlan:
    settings: StartupAuditSettings
    path: Path
    data_dir: Path
    audit_id: str
    manifest_sha256: str
    identity: dict = field(default_factory=dict)


def prepare_startup_audit(scope, guard, environ: Mapping[str, str] | None = None, *,
                          identity: Mapping[str, Any] | None = None) -> StartupAuditPlan | None:
    """Called before RT-1. None means not requested; any raise means refuse.

    The validated runtime-hook guard is mandatory (a STARTUP_AUDIT_* variable
    without the isolation manifest is refused, so an env flag can never start
    the service on a host-owning scope), and a frozen sidecar must match its
    build identity. Nothing is written here.
    """
    env = os.environ if environ is None else environ
    if not startup_audit_requested(env):
        return None
    settings = parse_startup_audit_settings(env)
    if guard is None:
        raise PermissionError('startup audit requires the isolated audit guard')
    if scope.kind != 'audit' or scope.may_take_over_host_network or scope.data_dir is None:
        raise PermissionError('startup audit requires a non-mutating audit scope')
    manifest = guard.manifest
    if manifest.get('audit_id') != scope.id:
        raise PermissionError('startup audit guard does not match this scope')
    record = build_identity() if identity is None else dict(identity)
    require_build_identity(record)
    return StartupAuditPlan(settings=settings,
                            path=settings.path or Path(scope.data_dir) / DEFAULT_AUDIT_FILENAME,
                            data_dir=Path(manifest['data_dir']), audit_id=manifest['audit_id'],
                            manifest_sha256=manifest['manifest_sha256'], identity=record)


# -- per-thread CPU ----------------------------------------------------------

_FAMILY_DIGITS = re.compile(r'[-_ ]?\d+$')
_DEFAULT_THREAD_NAME = re.compile(r'^Thread-\d+ \((?P<target>[^)]*)\)$')
# bridge-audit-control runs the environment action's real runtime start
# (same transaction as a host service.start on bridge-jobs-change).
# Network recovery, settings apply and exit-quality probes run runtime work
# on bridge-named threads; counting them as bridge cost would fail P2-34
# for work the bridge only schedules.
_TRANSACTION_FAMILIES = frozenset({'bridge-jobs-control', 'bridge-jobs-change',
                                   'bridge-jobs-session-end', 'bridge-audit-control',
                                   'bridge-network-recovery', 'bridge-settings-apply',
                                   'bridge-exit-probe', 'bridge-exit-verify',
                                   'bridge-exit-auto'})
_THREAD_API = None


def thread_family(name: str, *, main: bool = False) -> str:
    if main:
        return 'bridge-main'
    text = str(name or '?')
    default = _DEFAULT_THREAD_NAME.match(text)
    if default:
        return f"Thread({default.group('target')})"
    base = text
    for _ in range(3):
        base = _FAMILY_DIGITS.sub('', base)
    return base or text


def thread_class(family: str) -> str:
    """Disjoint classes; executor transaction workers run runtime work."""
    if family in _TRANSACTION_FAMILIES:
        return 'transaction'
    if family.startswith('bridge-audit'):
        return 'audit'
    if family.startswith('bridge') or family == 'Thread(_read)':
        return 'bridge'
    return 'runtime'


def _thread_times_api():
    global _THREAD_API
    if _THREAD_API is None and os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]

        # Immediate queries: PyDLL keeps the GIL (no Windows GIL handoff tick).
        kernel = ctypes.PyDLL('kernel32', use_last_error=True)
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
        kernel.GetThreadTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        def open_thread(native_id):
            return kernel.OpenThread(0x0800, False, native_id) or None

        def cpu_ns(handle):
            times = [FILETIME() for _ in range(4)]
            if not kernel.GetThreadTimes(handle, *(ctypes.byref(item) for item in times)):
                return None
            kernel_time, user_time = times[2], times[3]
            return 100 * (((kernel_time.high << 32) | kernel_time.low)
                          + ((user_time.high << 32) | user_time.low))

        _THREAD_API = (open_thread, cpu_ns, kernel.CloseHandle)
    return _THREAD_API


class ThreadCpuReader:
    """Lifetime CPU per Python thread (GetThreadTimes), grouped by class.

    Handles of exited threads stay open, so a finished thread keeps counting
    in later samples; the table is bounded and reports untracked threads.
    Only threads alive at some sample are attributed: a thread born and
    finished between two samples is missed (see unclassified_cores), and a
    thread whose OpenThread fails is counted in ``open_failures``.
    ``read`` is a test seam: (thread) -> cumulative CPU ns or None.
    """

    def __init__(self, *, max_tracked: int = 1024, read: Callable[[Any], int | None] | None = None):
        self._read = read
        self._api = None if read is not None else _thread_times_api()
        self.available = read is not None or self._api is not None
        self.max_tracked = max_tracked
        self._tracked: dict[Any, list] = {}
        self.open_failures = 0

    def _cpu(self, thread, entry):
        if self._read is not None:
            return self._read(thread)
        if entry[0] is None:
            return None
        return self._api[1](entry[0])

    def sample(self) -> dict:
        main = threading.main_thread()
        untracked = 0
        for thread in threading.enumerate():
            if thread in self._tracked:
                continue
            native = getattr(thread, 'native_id', None)
            if not native:
                continue
            if len(self._tracked) >= self.max_tracked:
                untracked += 1
                continue
            family = thread_family(thread.name, main=thread is main)
            handle = None if self._api is None else self._api[0](native)
            if self._api is not None and handle is None:
                self.open_failures += 1
            self._tracked[thread] = [handle, family, thread_class(family), None]
        families: dict[str, int] = {}
        classes = dict.fromkeys(THREAD_CLASSES, 0)
        for thread, entry in self._tracked.items():
            value = self._cpu(thread, entry)
            if value is not None:
                entry[3] = int(value)
            if entry[3] is None:
                continue
            families[entry[1]] = families.get(entry[1], 0) + entry[3]
            classes[entry[2]] += entry[3]
        return dict(available=self.available, classes=classes, families=families,
                    tracked=len(self._tracked), untracked_live_threads=untracked,
                    open_failures=self.open_failures)

    def close(self) -> None:
        if self._api is not None:
            for entry in self._tracked.values():
                if entry[0] is not None:
                    self._api[2](entry[0])
                    entry[0] = None


# -- CPU report ----------------------------------------------------------------

def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def cpu_components(metrics: Mapping[str, Any] | None) -> dict | None:
    """Component counters (bridge_traffic.collection_metrics) in audit form."""
    if not isinstance(metrics, Mapping):
        return None
    logs = metrics.get('logs') if isinstance(metrics.get('logs'), Mapping) else {}
    traffic = metrics.get('traffic') if isinstance(metrics.get('traffic'), Mapping) else {}
    return {'publisher_thread_cpu_ns': _int_or_none(metrics.get('publisher_thread_cpu_ns')),
            'state_worker_thread_cpu_ns': _int_or_none(metrics.get('state_worker_thread_cpu_ns')),
            'logs_worker_thread_cpu_ns': _int_or_none(logs.get('worker_thread_cpu_ns')),
            'logs_stream_thread_cpu_ns': _int_or_none(logs.get('stream_thread_cpu_ns')),
            'traffic_worker_thread_cpu_ns': _int_or_none(traffic.get('worker_thread_cpu_ns')),
            'log_work_cpu_ns': _int_or_none(logs.get('work_cpu_ns')),
            'logging_handler_cpu_ns': _int_or_none(logs.get('logging_handler_cpu_ns')),
            'producer_cpu_ns': _int_or_none(logs.get('producer_cpu_ns')),
            'log_publication_cpu_ns': _int_or_none(logs.get('publication_cpu_ns'))}


def _path_int(sample, *path):
    value = sample
    for key in path:
        value = value.get(key) if isinstance(value, Mapping) else None
    return _int_or_none(value)


def cpu_window(start: Mapping[str, Any], end: Mapping[str, Any], *, window: Any = None,
               complete: bool = True) -> dict:
    """ASK-02 section 6 format for one window between two cumulative samples."""
    elapsed = int(end['monotonic_ns']) - int(start['monotonic_ns'])

    def delta(*path):
        first, last = _path_int(start, *path), _path_int(end, *path)
        return None if first is None or last is None else max(0, last - first)

    def cores(value):
        return None if value is None or elapsed <= 0 else round(value / elapsed, 6)

    threads = {name: delta('threads', 'classes', name) for name in THREAD_CLASSES}
    if not (isinstance(start.get('threads'), Mapping) and start['threads'].get('available')):
        threads = dict.fromkeys(THREAD_CLASSES)
    components = {name: delta('components', name) for name in COMPONENT_KEYS}
    lower = threads['bridge']
    log_work = components['log_work_cpu_ns']
    # The gate value must never quietly degrade to the lower bound: a missing
    # log-work counter at either boundary makes the upper bound unknown.
    upper = None if lower is None or log_work is None else lower + log_work
    upper_incomplete = lower is not None and log_work is None
    process = delta('process_cpu_ns')
    classified = (None if any(value is None for value in threads.values())
                  else sum(threads.values()))
    unclassified = (None if process is None or classified is None
                    else (int(end['process_cpu_ns']) - int(start['process_cpu_ns'])) - classified)
    missing = [side for side, sample in (('start', start), ('end', end))
               if not isinstance(sample.get('components'), Mapping)]
    return {'window_s': window, 'start_elapsed_ms': start.get('elapsed_ms'),
            'end_elapsed_ms': end.get('elapsed_ms'), 'elapsed_ns': elapsed, 'complete': complete,
            'publisher_cores': cores(components['publisher_thread_cpu_ns']),
            'bridge_cores': cores(upper), 'upper_incomplete': upper_incomplete,
            'bridge_cores_lower': cores(lower),
            'log_work_cores': cores(log_work), 'transaction_cores': cores(threads['transaction']),
            'audit_cores': cores(threads['audit']), 'runtime_cores': cores(threads['runtime']),
            'process_cores': cores(process),
            'unclassified_cores': (None if unclassified is None or elapsed <= 0
                                   else round(unclassified / elapsed, 6)),
            'components_missing_at': missing,
            'component_cores': {name: cores(value) for name, value in components.items()},
            'cpu_ns': {'publisher': components['publisher_thread_cpu_ns'], 'bridge_upper': upper,
                       'bridge_lower': lower, 'log_work': log_work, 'process': process,
                       'process_minus_classified': unclassified,
                       **{f'thread_{name}': value for name, value in threads.items()}}}


def cpu_report(samples: list[Mapping[str, Any]], *, interval_ms: int) -> dict:
    """Whole observation, the four fixed windows, and through-exit totals."""
    observed = [sample for sample in samples if sample.get('label') != 'final']
    final = samples[-1] if samples and samples[-1].get('label') == 'final' else None
    windows = []
    for low, high in CPU_WINDOWS_SECONDS:
        starts = [s for s in observed if s['elapsed_ms'] <= low * 1000]
        ends = [s for s in observed if s['elapsed_ms'] <= high * 1000]
        if not observed or observed[-1]['elapsed_ms'] <= low * 1000 or not ends:
            windows.append({'window_s': [low, high], 'available': False})
            continue
        start = starts[-1] if starts else observed[0]
        end = ends[-1]
        if end is start:
            windows.append({'window_s': [low, high], 'available': False})
            continue
        complete = end['elapsed_ms'] >= high * 1000 - interval_ms
        windows.append(dict(cpu_window(start, end, window=[low, high], complete=complete),
                            available=True))
    full = (cpu_window(observed[0], observed[-1], window='observation')
            if len(observed) > 1 else None)
    first = observed[0] if observed else None
    through_exit = (cpu_window(first, final, window='through_exit')
                    if first is not None and final is not None else None)
    return {'definition': CPU_DEFINITION, 'windows': windows, 'full': full,
            'through_exit': through_exit}


def _json_safe(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {'unserializable': type(value).__name__}


def _write_json_atomic(path: Path, value: Any) -> None:
    """Pending-then-replace, the same contract as the legacy control files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending')
    pending.write_bytes(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                   default=str).encode('utf-8'))
    replace_with_retry(pending, path)


# -- session port ----------------------------------------------------------------

class AuditSessionPort(Protocol):
    """What StartupAudit needs from the bridge session (no runtime getter)."""

    def collection_metrics(self) -> dict | None: ...
    def publisher_counters(self) -> dict | None: ...
    def published_snapshot(self, topic: str) -> dict | None: ...
    def subscribed_topics(self) -> list[str]: ...
    def run_start_action(self, should_run: Callable[[], bool]) -> Any: ...
    def request_stop_and_wait(self, timeout: float) -> dict: ...
    def request_quit(self) -> None: ...
    def quitting(self) -> bool: ...


class BridgeSessionAuditPort:
    """Adapter over BridgeSession's public members and existing public APIs only.

    Reads already-published state (PublicationState.snapshot of subscribed
    topics) and collection counters; it never calls a runtime status getter,
    never subscribes on its own and never takes the publisher writer lock.
    """

    def __init__(self, session):
        self._session = session

    def collection_metrics(self):
        from bridge_traffic import collection_metrics
        session = self._session
        parts = (session.publisher, session.sampler, session.logs, session.traffic)
        if any(part is None for part in parts):
            return None
        return collection_metrics(*parts)

    def publisher_counters(self):
        publisher = self._session.publisher
        if publisher is None:
            return None
        return {'frames': int(publisher.frames), 'ticks': int(publisher.ticks)}

    def published_snapshot(self, topic):
        try:
            event = self._session.state.snapshot(topic)
        except ValueError:
            return None  # Not subscribed by the host: observe nothing.
        data = event.get('data') if isinstance(event, dict) else None
        return data if isinstance(data, dict) else None

    def subscribed_topics(self):
        try:
            return sorted(tuple(self._session.subscribed_topics))
        except RuntimeError:
            return []

    def run_start_action(self, should_run):
        """The same BridgeSession.start as service.start, in the mutation domain."""
        from bridge_jobs import JobContext
        session = self._session

        def work():
            if not should_run():
                return None
            return session.start({}, JobContext('audit-startup'))

        ran, result = session.executor.run_internal_mutation(work, abandoned=self.quitting)
        if not ran:
            raise RuntimeError('startup audit action abandoned before start')
        return result

    def request_stop_and_wait(self, timeout):
        session = self._session
        ack = session.shutdown.request_stop()
        if not ack.accepted:
            return {'accepted': False, 'skipped_reason': ack.skipped_reason}
        job = session.executor.wait(ack.op, timeout)
        if job is None:
            return {'accepted': True, 'state': 'timeout'}
        return {'accepted': True, 'state': job['state'], 'result': job['result'],
                'error': job['error']}

    def request_quit(self):
        self._session.shutdown.request_quit()

    def quitting(self):
        shutdown = self._session.shutdown
        return shutdown is not None and shutdown.quitting


# -- the audit -----------------------------------------------------------------

class StartupAudit:
    """One sidecar startup audit: markers at the real start, async sampling.

    ``run_start`` wraps BridgeSession's real runtime start (host command or
    the environment action alike); only the first one publishes the legacy
    markers via start_startup_audit. Sampling and every file write run on
    ``bridge-audit-*`` threads; the publication tick is never called or
    blocked. ``finish`` is the exit boundary: final sample plus bounded join.
    """

    def __init__(self, scope, plan: StartupAuditPlan, *, thread_cpu: ThreadCpuReader | None = None,
                 clock_ns: Callable[[], int] = time.monotonic_ns,
                 process_cpu_ns: Callable[[], int] = time.process_time_ns):
        self.scope, self.plan, self.settings = scope, plan, plan.settings
        self.samples_path = plan.path.with_name(plan.path.stem + '.samples.jsonl')
        self._thread_cpu = thread_cpu if thread_cpu is not None else ThreadCpuReader()
        self._clock_ns, self._process_cpu_ns = clock_ns, process_cpu_ns
        self._port: AuditSessionPort | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._final = threading.Event()
        self.final_written = threading.Event()
        self._attempted = False
        self._marker_failure: str | None = None
        self._sampler_failure: str | None = None
        self._finished = False
        self._origin_ns: int | None = None
        self._marker = None
        self._action = None
        self._samples: list[dict] = []
        self._dropped_samples = 0
        self._flushed = 0
        self._dirty = False
        self._frames_at_origin = None
        self._firsts = dict.fromkeys(FIRSTS_DEFINITION)
        self._error_counts: dict[str, int] = {}
        self._observation_end = None
        self._start_result = None
        self._stop_result = None
        self._quit_requested = False
        self._notes: deque = deque(maxlen=32)
        self._errors: deque = deque(maxlen=32)
        self._sampler: threading.Thread | None = None
        self._controller: threading.Thread | None = None
        self._finisher: threading.Thread | None = None

    # wiring -------------------------------------------------------------------
    def bind(self, port: AuditSessionPort) -> None:
        self._port = port

    def begin(self) -> None:
        """After hello_ack: start the environment action, if one is configured."""
        if self.settings.action == 'idle':
            with self._lock:
                if self._attempted:
                    return
                self._attempted = True
            self._set_origin(self._clock_ns(), None, dict(action='idle', source='environment'))
        elif self.settings.action == 'start-services':
            self._controller = threading.Thread(target=self._control, name='bridge-audit-control',
                                                daemon=True)
            self._controller.start()

    def run_start(self, action: Callable[[], Any]) -> Any:
        """Wrap the real runtime start; markers precede it exactly once."""
        with self._lock:
            if self._marker_failure is not None:
                # Fail closed: this audit process already lost its origin; a
                # later start would run unmarked and unsampled.
                self._notes.append('start_refused_after_marker_failure')
                self._dirty = True
                raise PermissionError('startup audit markers failed; refusing an unaudited start')
            first = not self._attempted
            self._attempted = True
        if not first:
            if self.settings.action == 'idle':
                self._notes.append('start_during_idle_audit')
            return action()
        source = getattr(self._local, 'source', None) or 'command'

        def audited():
            marker = current_startup_marker()
            record = dict(action='service.start', source=source, origin='before_runtime_start',
                          action_started_monotonic=marker['window_started_monotonic'],
                          process_id=marker['process_id'], audit_id=marker['audit_id'])
            self._write_action_marker(record)
            self._set_origin(int(marker['window_started_monotonic'] * 1e9), marker, record)
            return action()

        try:
            return start_startup_audit(self.scope, audited)
        except Exception as exc:
            # Before the origin nothing of the action ran: guard, identity or
            # marker publication failed (PermissionError or any other OSError
            # such as replace_with_retry giving up). After it, the runtime
            # start itself failed and later restarts stay allowed.
            if self._origin_ns is None:
                with self._lock:
                    self._marker_failure = type(exc).__name__
                    self._record_error('markers', str(exc) or type(exc).__name__,
                                       type=type(exc).__name__)
                    self._dirty = True
            raise

    def finish(self) -> None:
        """Exit boundary (exit_process and main finally); idempotent, bounded."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            sampler = self._sampler
        self._final.set()
        if sampler is None:
            sampler = threading.Thread(target=self._write_final, name='bridge-audit-final',
                                       daemon=True)
            sampler.start()
        sampler.join(_FINISH_JOIN_SECONDS)
        if not sampler.is_alive():
            self._thread_cpu.close()

    # threads ------------------------------------------------------------------
    def _control(self):
        port = self._port
        try:
            deadline = time.monotonic() + _SUBSCRIBE_WAIT_SECONDS
            while (not self._final.is_set() and time.monotonic() < deadline
                   and not port.quitting() and not port.subscribed_topics()):
                self._final.wait(.02)
            if self._final.is_set() or port.quitting():
                self._notes.append('environment_action_abandoned')
                return
            self._local.source = 'environment'
            try:
                # Checked inside the mutation domain: a host start that won
                # the race already owns the markers; never start twice.
                result = port.run_start_action(lambda: not self._attempted)
                if result is None:
                    self._notes.append('host_start_preceded_environment_action')
                self._start_result = _json_safe(result)
            except Exception as exc:
                self._start_result = {'error': getattr(exc, 'code', None) or type(exc).__name__}
            finally:
                self._local.source = None
            with self._lock:
                self._dirty = True
        except Exception as exc:
            self._record_error('control', type(exc).__name__)

    def _set_origin(self, origin_ns, marker, record):
        with self._lock:
            if self._origin_ns is not None:
                return
            self._origin_ns, self._marker, self._action = origin_ns, marker, record
            if self._finished:
                return  # The exit-time final writer records this origin.
            self._sampler = threading.Thread(target=self._sample_loop, name='bridge-audit-sampler',
                                             daemon=True)
            self._sampler.start()

    def _sample_loop(self):
        interval_ns = self.settings.interval_ms * 1_000_000
        end_ns = self._origin_ns + self.settings.duration_ms * 1_000_000
        next_ns = self._clock_ns()
        last_write = time.monotonic()
        ended = False
        try:
            while not self._final.is_set():
                if not ended:
                    now = self._clock_ns()
                    self._take_sample(now, 'periodic', boundary=now >= end_ns)
                    if now >= end_ns:
                        ended = True
                        self._end_observation(now, 'duration_elapsed')
                        last_write = time.monotonic()
                if self._dirty and time.monotonic() - last_write >= _WRITE_EVERY_SECONDS:
                    # One failed write (e.g. a transient sharing violation) must
                    # not end sampling, the observation or the quit.
                    self._safe_write('write')
                    last_write = time.monotonic()
                if ended:
                    wait = 1.0
                else:
                    now = self._clock_ns()
                    next_ns = max(next_ns + interval_ns, now)
                    # The boundary itself is sampled, so the last window ends on time.
                    wait = max(0.0, (min(next_ns, end_ns) - now) / 1e9)
                self._final.wait(wait)
        except Exception as exc:
            try:
                self._fail_sampler(exc)
            except Exception as failure:
                self._record_error('sampler_failure', type(failure).__name__)
            # Keep the exit boundary: finish() still takes the final sample.
            self._final.wait()
        self._write_final()

    def _fail_sampler(self, exc):
        """Unexpected sampler exit: record it, end the observation, still quit."""
        reason = type(exc).__name__
        now = self._clock_ns()
        with self._lock:
            self._sampler_failure = reason
            self._record_error('sampler', reason)
            ended = self._observation_end is not None
        if not ended:
            self._end_observation(now, 'sampler_failed')
        else:
            self._safe_write('write')  # completed stays false (sampler_failure)

    def _end_observation(self, now, reason):
        with self._lock:
            if self._observation_end is not None:
                return
            self._observation_end = dict(monotonic_ns=now, reason=reason,
                                         elapsed_ms=(now - self._origin_ns) // 1_000_000)
            self._dirty = True
            start_finisher = self.settings.quit_when_done and self._finisher is None
            if start_finisher:
                self._finisher = threading.Thread(target=self._stop_and_quit,
                                                  name='bridge-audit-finish', daemon=True)
        # The stop/quit never depends on the observation-end write succeeding.
        if start_finisher:
            self._finisher.start()
        self._safe_write('observation_end_write')

    def _stop_and_quit(self):
        port = self._port
        if (self._action or {}).get('action') == 'service.start':
            try:
                self._stop_result = _json_safe(port.request_stop_and_wait(_STOP_WAIT_SECONDS))
            except Exception as exc:
                self._stop_result = {'error': type(exc).__name__}
        try:
            self._quit_requested = True
            port.request_quit()
        except Exception as exc:
            self._record_error('quit', type(exc).__name__)
        with self._lock:
            self._dirty = True

    def _write_final(self):
        """Only the finish() path reaches here; the final file is `completed`."""
        try:
            if self._origin_ns is not None:
                self._take_sample(self._clock_ns(), 'final', boundary=True)
            self._write(final=True)
        except Exception as exc:
            self._record_error('final', type(exc).__name__)
        finally:
            self.final_written.set()

    def _record_error(self, stage, error, **extra):
        with self._lock:
            self._error_counts[stage] = self._error_counts.get(stage, 0) + 1
            self._errors.append({'stage': stage, 'error': error, **extra})

    def _safe_write(self, stage):
        try:
            self._write()
        except Exception as exc:
            self._record_error(stage, type(exc).__name__)
            with self._lock:
                self._dirty = True  # retried on the next write opportunity

    # sampling ------------------------------------------------------------------
    def _read_port(self, name, call, *, boundary, elapsed_ms, retries=0):
        """One port read; a failure loses only this value, never the sample."""
        for attempt in range(retries + 1):
            try:
                return call()
            except Exception as exc:
                if attempt < retries:
                    continue
                extra = {'elapsed_ms': elapsed_ms}
                if boundary:
                    extra['boundary'] = True
                self._record_error('metrics:' + name, type(exc).__name__, **extra)
        return None

    def _take_sample(self, now, label, *, boundary=False):
        port = self._port
        elapsed_ms = (now - self._origin_ns) // 1_000_000
        sample = {'label': label, 'elapsed_ms': elapsed_ms,
                  'monotonic_ns': now, 'process_cpu_ns': self._process_cpu_ns()}
        boundary = boundary or label == 'final' or not self._samples or self._near_window_edge(
            elapsed_ms)
        try:
            sample['threads'] = self._thread_cpu.sample()
        except Exception as exc:
            sample['threads'] = None
            self._record_error('threads', type(exc).__name__, elapsed_ms=elapsed_ms)
        metrics = counters = service = tiers = None
        if port is not None:
            def read(name, call, retries=0):
                return self._read_port(name, call, boundary=boundary, elapsed_ms=elapsed_ms,
                                       retries=retries)
            # Publisher.metrics iterates its rate deque without a lock while
            # the owner thread appends to it: a cross-thread RuntimeError is
            # transient, so collection_metrics gets one immediate retry.
            metrics = read('collection_metrics', port.collection_metrics, retries=1)
            counters = read('publisher_counters', port.publisher_counters)
            service = read('service.state', lambda: port.published_snapshot('service.state'))
            tiers = read('tiers.summary', lambda: port.published_snapshot('tiers.summary'))
        sample['components'] = cpu_components(metrics)
        publisher = dict(counters or {})
        if isinstance(metrics, Mapping):
            for key in ('lateness_ns', 'queue_depth', 'dropped', 'log_records_dropped',
                        'production_to_send_ns', 'pending_write', 'ticks_per_second',
                        'frames_per_second'):
                publisher[key] = metrics.get(key)
        sample['publisher'] = publisher or None
        runtime = service.get('runtime') if isinstance(service, Mapping) else None
        intent = service.get('intent') if isinstance(service, Mapping) else None
        sample['service'] = None if not isinstance(runtime, Mapping) else {
            'startup_phase': runtime.get('startup_phase'),
            'stop_requested': (intent or {}).get('stop_requested')}
        pool = tiers.get('pool') if isinstance(tiers, Mapping) else None
        bastion = ((tiers.get('tiers') or {}).get('bastion') if isinstance(tiers, Mapping) else None)
        sample['tiers'] = None if not isinstance(pool, Mapping) else {
            'pool_total': pool.get('total'), 'pool_usable': pool.get('usable'),
            'bastion_usable': (bastion or {}).get('usable')}
        with self._lock:
            self._observe_firsts(sample)
            if len(self._samples) < _MAX_SAMPLES or label == 'final':
                self._samples.append(sample)
            else:
                self._dropped_samples += 1
            self._dirty = True

    def _near_window_edge(self, elapsed_ms):
        """Could this sample be the last one at or before a window edge?"""
        edges = {edge * 1000 for window in CPU_WINDOWS_SECONDS for edge in window}
        edges.add(self.settings.duration_ms)
        interval = self.settings.interval_ms
        return any(edge - interval < elapsed_ms <= edge for edge in edges)

    def _observe_firsts(self, sample):
        frames = (sample.get('publisher') or {}).get('frames')
        key = 'first_frame_after_origin_elapsed_ms'
        if isinstance(frames, int):
            if self._frames_at_origin is None:
                self._frames_at_origin = frames
            elif self._firsts[key] is None and frames > self._frames_at_origin:
                self._firsts[key] = sample['elapsed_ms']
        tiers = sample.get('tiers') or {}
        for key, field_name in (('pool_total', 'first_pool_node_elapsed_ms'),
                                ('pool_usable', 'first_pool_usable_elapsed_ms')):
            value = tiers.get(key)
            if self._firsts[field_name] is None and isinstance(value, int) and value > 0:
                self._firsts[field_name] = sample['elapsed_ms']

    # output -----------------------------------------------------------------
    def _write_action_marker(self, record):
        try:
            destination = self.plan.data_dir / ACTION_MARKER_FILENAME
            pending = destination.with_suffix('.pending')
            pending.write_bytes(json.dumps(record).encode('utf-8'))
            replace_with_retry(pending, destination)
        except OSError as exc:
            # Auxiliary R8-compatible origin; the legacy pair stays authoritative.
            self._record_error('action_marker', type(exc).__name__)

    def document(self, *, final: bool = False) -> dict:
        with self._lock:
            samples = list(self._samples)
            port = self._port
            body = {
                # True only on the finish() path of a sampler that did not
                # die; a failed sampler leaves completed=false plus the reason.
                'schema': STARTUP_AUDIT_SCHEMA,
                'completed': bool(final and self._sampler_failure is None),
                'sampler_failure': self._sampler_failure,
                'marker_failure': self._marker_failure,
                'process_id': os.getpid(), 'parent_process_id': os.getppid(),
                'audit_id': self.plan.audit_id, 'manifest_sha256': self.plan.manifest_sha256,
                'build_identity': dict(self.plan.identity),
                'settings': {'duration_ms': self.settings.duration_ms,
                             'interval_ms': self.settings.interval_ms,
                             'quit_when_done': self.settings.quit_when_done,
                             'action': self.settings.action},
                'marker': self._marker, 'action': self._action,
                'origin_monotonic': None if self._origin_ns is None else self._origin_ns / 1e9,
                'observation_end': self._observation_end, 'firsts': dict(self._firsts),
                'firsts_definition': FIRSTS_DEFINITION,
                'start_result': self._start_result, 'stop_result': self._stop_result,
                'quit_requested': self._quit_requested,
                'samples': {'path': self.samples_path.name, 'count': len(samples),
                            'dropped': self._dropped_samples},
                'notes': list(self._notes), 'errors': list(self._errors),
                'error_counts': dict(self._error_counts)}
        body['subscription'] = {'topics': port.subscribed_topics() if port is not None else []}
        body['cpu'] = cpu_report(samples, interval_ms=self.settings.interval_ms)
        last = next((s for s in reversed(samples) if s.get('threads')), None)
        body['thread_families_ns'] = last['threads'].get('families') if last else None
        return body

    def _write(self, *, final: bool = False) -> None:
        with self._write_lock:
            with self._lock:
                start = self._flushed
                fresh = self._samples[start:]
                self._dirty = False
            if fresh:
                payload = ''.join(json.dumps(sample, separators=(',', ':'), default=str) + '\n'
                                  for sample in fresh)
                self.samples_path.parent.mkdir(parents=True, exist_ok=True)
                with self.samples_path.open('a', encoding='utf-8') as stream:
                    stream.write(payload)
                with self._lock:
                    # Advanced only after the append: a failed write is retried.
                    self._flushed = start + len(fresh)
            _write_json_atomic(self.plan.path, self.document(final=final))


def create_startup_audit(scope, plan: StartupAuditPlan) -> StartupAudit:
    return StartupAudit(scope, plan)
