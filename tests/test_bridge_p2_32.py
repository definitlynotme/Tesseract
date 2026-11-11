"""T-P2-32: startup audit entry, CPU accounting output and the P2 A/B lab harness.

Never touches the host proxy, firewall or network: guards come from the P1
audit fixture (real runtime hook, controlled hooks), the harness only runs
its --dry-run, and Job tests spawn trivial local Python children.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridge_audit
import bridge_main
from bridge_audit import (StartupAudit, StartupAuditPlan, StartupAuditSettings, ThreadCpuReader,
                          cpu_report, cpu_window, prepare_startup_audit, start_startup_audit)
from scripts import tier3_audit_entry as entry
from tests.test_bridge_audit import audit  # noqa: F401  (shared isolated-audit fixture)

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / '.build' / 'claude-p2-ab'
MARKERS = ('window-start-control.json', 'child-process-control.json')
LEGACY_FIELDS = {'window_started_monotonic', 'process_id', 'parent_process_id', 'audit_id',
                 'manifest_sha256'}


def _hook():
    import runpy
    runpy.run_path(str(ROOT / 'scripts' / 'tier3_audit_runtime_hook.py'))
    return entry._guard


def _no_markers(data_dir):
    return (not any((Path(data_dir) / name).exists() for name in MARKERS)
            and not (Path(data_dir) / 'startup-action-control.json').exists()
            and not list(Path(data_dir).glob('*.pending')))


def _plan(tmp_path, *, action='idle', duration_ms=300, interval_ms=50, quit_when_done=True,
          data_dir=None, audit_id='a' * 32, manifest_sha256='0' * 64):
    settings = StartupAuditSettings(path=None, duration_ms=duration_ms, interval_ms=interval_ms,
                                    quit_when_done=quit_when_done, action=action)
    return StartupAuditPlan(settings=settings, path=Path(tmp_path) / 'audit-out' / 'sidecar.json',
                            data_dir=Path(data_dir or tmp_path), audit_id=audit_id,
                            manifest_sha256=manifest_sha256, identity={'frozen': False})


class FakePort:
    """Session port double; records which thread every call came from."""

    def __init__(self, audit_object=None):
        self.audit = audit_object
        self.calls = []
        self.frames = 0
        self.topics = ['service.state', 'tiers.summary']
        self._quit = threading.Event()
        self.runtime_starts = 0

    def _note(self, name):
        self.calls.append((name, threading.current_thread().name))

    def collection_metrics(self):
        self._note('metrics')
        tick = len(self.calls)
        return {'publisher_thread_cpu_ns': tick * 1_000_000, 'state_worker_thread_cpu_ns': tick,
                'lateness_ns': 0, 'queue_depth': 0, 'dropped': 0,
                'logs': {'worker_thread_cpu_ns': tick, 'stream_thread_cpu_ns': 0,
                         'work_cpu_ns': tick * 10, 'producer_cpu_ns': 1,
                         'logging_handler_cpu_ns': 2, 'publication_cpu_ns': 3},
                'traffic': {'worker_thread_cpu_ns': tick}}

    def publisher_counters(self):
        self._note('counters')
        self.frames += 1
        return {'frames': self.frames, 'ticks': self.frames}

    def published_snapshot(self, topic):
        self._note('snapshot:' + topic)
        if topic == 'service.state':
            return {'runtime': {'startup_phase': 'collecting'}, 'intent': {'stop_requested': False}}
        return {'pool': {'total': 2, 'usable': 1}, 'tiers': {'bastion': {'usable': 1}}}

    def subscribed_topics(self):
        return list(self.topics)

    def run_start_action(self, should_run):
        self._note('run_start_action')
        if not should_run():
            return None

        def runtime_start():
            self.runtime_starts += 1
            return {'failure_code': None, 'startup_phase': 'collecting'}
        # Same shape as BridgeSession.start -> StartupAudit.run_start(access.start).
        return self.audit.run_start(runtime_start)

    def request_stop_and_wait(self, timeout):
        self._note('stop')
        return {'accepted': True, 'state': 'done', 'result': {'startup_phase': 'stopped'}}

    def request_quit(self):
        self._note('quit')
        self._quit.set()

    def quitting(self):
        return self._quit.is_set()


def _wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return False


# -- enabling, refusal, guard ------------------------------------------------------

def test_enabled_only_by_startup_audit_env_or_manifest():
    assert not bridge_audit.startup_audit_requested({})
    assert not bridge_audit.startup_audit_requested({'TESSERACT_STARTUP_AUDIT_PATH': '  '})
    assert bridge_audit.startup_audit_requested({'TESSERACT_STARTUP_AUDIT_QUIT': '1'})
    assert bridge_audit.startup_audit_requested({'TESSERACT_ISOLATED_AUDIT_MANIFEST': 'x'})
    scope = SimpleNamespace(kind='default', id='default', may_take_over_host_network=True,
                            data_dir=None)
    assert prepare_startup_audit(scope, None, {}) is None


@pytest.mark.parametrize('env', [
    {'TESSERACT_STARTUP_AUDIT_PATH': str(Path(os.sep, 'abs', 'a.json').resolve())},
    {'TESSERACT_STARTUP_AUDIT_ACTION': 'start-services'},
    {'TESSERACT_ISOLATED_AUDIT_MANIFEST': 'manifest.json'},
])
def test_missing_guard_is_refused(tmp_path, env):
    scope = SimpleNamespace(kind='custom', id='c' * 24, may_take_over_host_network=False,
                            data_dir=tmp_path)
    with pytest.raises(PermissionError, match='guard'):
        prepare_startup_audit(scope, None, env)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('env', [
    {'TESSERACT_STARTUP_AUDIT_ACTION': 'ui-motion'},
    {'TESSERACT_STARTUP_AUDIT_DURATION_MS': '50'},
    {'TESSERACT_STARTUP_AUDIT_INTERVAL_MS': 'fast'},
    {'TESSERACT_STARTUP_AUDIT_QUIT': 'maybe'},
    {'TESSERACT_STARTUP_AUDIT_PATH': 'relative.json'},
])
def test_invalid_settings_never_clamp(env):
    with pytest.raises(ValueError):
        bridge_audit.parse_startup_audit_settings(env)


def test_settings_defaults_and_legacy_values():
    settings = bridge_audit.parse_startup_audit_settings({
        'TESSERACT_STARTUP_AUDIT_DURATION_MS': '150000', 'TESSERACT_STARTUP_AUDIT_INTERVAL_MS': '250',
        'TESSERACT_STARTUP_AUDIT_QUIT': '1', 'TESSERACT_STARTUP_AUDIT_ACTION': 'start-services'})
    assert settings == StartupAuditSettings(None, 150000, 250, True, 'start-services')
    assert bridge_audit.parse_startup_audit_settings({}) == StartupAuditSettings(
        None, 150000, 250, False, '')


def test_prepare_with_guard_writes_nothing_and_rejects_non_audit_scope(audit, monkeypatch):
    scope, manifest = audit
    guard = _hook()
    assert bridge_audit.attach_audit_guard(scope) is guard
    plan = prepare_startup_audit(scope, guard, dict(os.environ))
    assert plan.audit_id == scope.id and plan.path == scope.data_dir / 'sidecar-startup-audit.json'
    assert plan.manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert plan.identity['frozen'] is False and plan.identity['identity_match'] is True
    for changes in (dict(kind='custom'), dict(may_take_over_host_network=True), dict(id='b' * 32)):
        with pytest.raises(PermissionError):
            prepare_startup_audit(replace(scope, **changes), guard, dict(os.environ))
    assert _no_markers(scope.data_dir)


def test_main_refuses_requested_audit_before_rt1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('TESSERACT_STARTUP_AUDIT_PATH', str(tmp_path / 'audit.json'))
    monkeypatch.delenv('TESSERACT_ISOLATED_AUDIT_MANIFEST', raising=False)
    calls = []
    lease = SimpleNamespace(close=lambda: calls.append('release'))
    scope = SimpleNamespace(kind='custom', id='c' * 24, may_take_over_host_network=False,
                            data_dir=tmp_path, config_dir=tmp_path)

    def prepare(value, guard):
        calls.append('prepare')
        return bridge_audit.prepare_startup_audit(value, guard)
    steps = bridge_main.Steps(
        parse=lambda _argv: SimpleNamespace(lang='en_US'), scope=lambda: scope,
        acquire=lambda _scope: SimpleNamespace(status='acquired', lease=lease),
        logging=lambda _scope: calls.append('logging'),
        audit=lambda _scope: calls.append('audit'),
        recovery=lambda _scope: calls.append('RT-1'), transport=Mock(), loop=Mock(),
        startup_audit=prepare)
    assert bridge_main.main([], steps=steps) == 4
    assert json.loads(capsys.readouterr().err) == {'code': 'startup_audit_refused'}
    assert calls == ['logging', 'audit', 'prepare', 'release']
    assert not (tmp_path / 'audit.json').exists()


def test_main_prepares_after_guard_and_before_rt1(tmp_path):
    calls = []
    lease = SimpleNamespace(close=lambda: calls.append('release'))
    scope = SimpleNamespace(kind='audit', id='a' * 32, may_take_over_host_network=False,
                            data_dir=tmp_path, config_dir=tmp_path)

    guard_object = SimpleNamespace(finish=lambda: calls.append('guard.finish'))

    def unavailable(_args):
        raise bridge_main.StartupError('controlled_end')
    steps = bridge_main.Steps(
        parse=lambda _argv: SimpleNamespace(lang='en_US'), scope=lambda: scope,
        acquire=lambda _scope: SimpleNamespace(status='acquired', lease=lease),
        logging=lambda _scope: None,
        audit=lambda _scope: calls.append('guard') or guard_object,
        startup_audit=lambda _scope, guard: calls.append(('prepare', guard)),
        recovery=lambda _scope: calls.append('RT-1') or SimpleNamespace(
            gate=SimpleNamespace(require_idle=lambda: None)),
        transport=unavailable)
    assert bridge_main.main([], steps=steps) == 4
    assert calls == ['guard', ('prepare', guard_object), 'RT-1', 'guard.finish', 'release']
    assert bridge_main.Steps().startup_audit is None  # production default = prepare_startup_audit


# -- markers only at the real start; helpers never --------------------------------------

def test_markers_only_at_real_start_action(audit, tmp_path):
    scope, manifest = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   settings=StartupAuditSettings(None, 60_000, 50, False, ''),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    port = FakePort(startup)
    startup.bind(port)
    startup.begin()  # '' waits for the host's real start: nothing yet
    time.sleep(.05)
    assert _no_markers(scope.data_dir) and startup._sampler is None
    seen = {}

    def runtime_start():
        for name in MARKERS:
            seen[name] = json.loads((scope.data_dir / name).read_bytes())
        seen['action'] = json.loads((scope.data_dir / 'startup-action-control.json').read_bytes())
        return {'failure_code': None, 'startup_phase': 'collecting'}

    assert startup.run_start(runtime_start) == {'failure_code': None, 'startup_phase': 'collecting'}
    first, second = seen[MARKERS[0]], seen[MARKERS[1]]
    assert first == second and set(first) == LEGACY_FIELDS
    assert first['process_id'] == os.getpid() and first['parent_process_id'] == os.getppid()
    assert first['audit_id'] == scope.id
    assert first['manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert seen['action']['action_started_monotonic'] == first['window_started_monotonic']
    assert seen['action']['source'] == 'command'
    assert not list(scope.data_dir.glob('*.pending'))
    # A later start (restart after stop) never rewrites the run's markers.
    for name in MARKERS:
        (scope.data_dir / name).write_bytes(b'kept')
    assert startup.run_start(lambda: 'again') == 'again'
    assert all((scope.data_dir / name).read_bytes() == b'kept' for name in MARKERS)
    startup.finish()
    document = json.loads(plan.path.read_bytes())
    assert document['completed'] is True and document['marker'] == first
    assert document['origin_monotonic'] == pytest.approx(first['window_started_monotonic'])
    assert document['action']['source'] == 'command'


def test_bridge_session_wraps_only_the_runtime_start():
    order = []

    class Access:
        def start(self):
            order.append('runtime.start')
            return {'failure_code': None, 'startup_phase': 'collecting'}

    class Audit:
        def run_start(self, action):
            order.append('audit.enter')
            result = action()
            order.append('audit.exit')
            return result

    session = bridge_main.BridgeSession.__new__(bridge_main.BridgeSession)
    session.network_lock = threading.RLock()
    session.report = SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: order.append('gate')))
    session.secrets = SimpleNamespace(refresh=lambda **_kwargs: order.append('secrets'))
    session.access = Access()
    session.record_service = lambda: order.append('record')
    session.startup_audit = Audit()
    from bridge_jobs import JobContext
    assert session.start({}, JobContext('op'))['startup_phase'] == 'collecting'
    assert order == ['gate', 'secrets', 'audit.enter', 'runtime.start', 'audit.exit',
                     'secrets', 'record']
    order.clear()
    session.startup_audit = None
    session.start({}, JobContext('op'))
    assert order == ['gate', 'secrets', 'runtime.start', 'secrets', 'record']


@pytest.mark.parametrize('flag', ['--multiprocessing-fork', '--validator-helper-smoke=1',
                                  '--validator-helper-slots=2'])
def test_helpers_never_write_root_markers(audit, monkeypatch, tmp_path, flag):
    scope, _ = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    monkeypatch.setattr(sys, 'argv', ['tesseract-runtime.exe', flag])
    action = Mock()
    with pytest.raises(PermissionError, match='helpers'):
        startup.run_start(action)
    action.assert_not_called()
    assert _no_markers(scope.data_dir)
    startup.finish()
    assert json.loads(plan.path.read_bytes())['errors'][0]['stage'] == 'markers'


def test_helper_process_name_and_dispatch_never_reach_the_audit(audit, monkeypatch):
    import multiprocessing
    scope, _ = audit
    _hook()
    monkeypatch.setattr(multiprocessing, 'current_process', lambda: SimpleNamespace(name='Helper-1'))
    with pytest.raises(PermissionError, match='helpers'):
        start_startup_audit(scope, Mock())
    prepare = Mock()
    steps = bridge_main.Steps(helper=lambda _argv: 0, startup_audit=prepare)
    assert bridge_main.main(['--validator-helper-smoke=1'], steps=steps) == 0
    prepare.assert_not_called()
    assert _no_markers(scope.data_dir)


def test_idle_audit_never_writes_markers_even_if_started(tmp_path):
    plan = _plan(tmp_path, action='idle', duration_ms=5000, quit_when_done=False)
    startup = StartupAudit(SimpleNamespace(kind='audit'), plan)
    startup.bind(FakePort(startup))
    startup.begin()
    assert startup.run_start(lambda: 'started') == 'started'
    startup.finish()
    assert _no_markers(tmp_path)
    document = json.loads(plan.path.read_bytes())
    assert document['marker'] is None and document['action'] == {'action': 'idle', 'source': 'environment'}
    assert 'start_during_idle_audit' in document['notes']


# -- frozen identity ---------------------------------------------------------------------

@pytest.mark.parametrize('matching', [True, False])
def test_frozen_identity_must_match_before_any_marker(audit, monkeypatch, matching):
    import tier3_build_identity as identity_module
    scope, _ = audit
    guard = _hook()
    identity = {'schema_version': 2, 'build_id': 'b' * 64, 'declared_executable_sha256': 'c' * 64,
                'executable_sha256': 'c' * 64 if matching else 'd' * 64,
                'identity_match': matching}
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(identity_module, 'tier3_build_identity', lambda: dict(identity))
    record = bridge_audit.build_identity()
    assert record['frozen'] is True and record['identity_match'] is matching
    action = Mock(return_value='ran')
    if matching:
        assert prepare_startup_audit(scope, guard, dict(os.environ)).identity == record
        assert start_startup_audit(scope, action) == 'ran'
        assert all((scope.data_dir / name).exists() for name in MARKERS)
    else:
        with pytest.raises(PermissionError, match='identity'):
            prepare_startup_audit(scope, guard, dict(os.environ))
        with pytest.raises(PermissionError, match='identity'):
            start_startup_audit(scope, action)
        action.assert_not_called()
        assert _no_markers(scope.data_dir)


def test_source_run_identity_is_recorded_not_enforced(monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    bridge_audit.require_build_identity()  # no-op for source runs
    record = bridge_audit.build_identity()
    assert record['frozen'] is False and record['executable_sha256'] == 'source-run'
    bridge_audit.require_build_identity(record)


# -- environment action, quit, asynchronous sampling -------------------------------------

def test_start_services_action_uses_the_real_start_once_then_stops_and_quits(audit, tmp_path):
    scope, _ = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   settings=StartupAuditSettings(None, 400, 50, True, 'start-services'),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    port = FakePort(startup)
    startup.bind(port)
    startup.begin()
    assert _wait(port._quit.is_set)
    startup.finish()
    assert port.runtime_starts == 1
    names = [name for name, _thread in port.calls]
    assert names.index('stop') < names.index('quit')
    threads = {name: thread for name, thread in port.calls}
    assert threads['run_start_action'] == 'bridge-audit-control'
    assert threads['stop'] == threads['quit'] == 'bridge-audit-finish'
    document = json.loads(plan.path.read_bytes())
    assert document['action']['source'] == 'environment'
    assert document['start_result'] == {'failure_code': None, 'startup_phase': 'collecting'}
    assert document['stop_result']['state'] == 'done' and document['quit_requested'] is True
    assert document['observation_end']['reason'] == 'duration_elapsed'
    assert document['firsts']['first_frame_after_origin_elapsed_ms'] is not None
    assert set(document['firsts']) == set(document['firsts_definition'])
    assert document['firsts']['first_pool_usable_elapsed_ms'] is not None


def test_host_start_preempts_environment_action_without_double_start(audit, tmp_path):
    scope, _ = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   settings=StartupAuditSettings(None, 60_000, 50, False, 'start-services'),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    port = FakePort(startup)
    port.topics = []  # the controller waits for a subscription (bounded)
    startup.bind(port)
    startup.begin()
    assert startup.run_start(lambda: {'failure_code': None}) == {'failure_code': None}
    port.topics = ['service.state']
    startup._controller.join(5)
    startup.finish()
    assert port.runtime_starts == 0
    document = json.loads(plan.path.read_bytes())
    assert document['action']['source'] == 'command'
    assert 'host_start_preceded_environment_action' in document['notes']


def test_sampling_and_writes_never_run_on_the_calling_thread(tmp_path):
    plan = _plan(tmp_path, action='idle', duration_ms=300, interval_ms=50, quit_when_done=True)
    startup = StartupAudit(SimpleNamespace(kind='audit'), plan)
    port = FakePort(startup)
    writes = []
    original = bridge_audit._write_json_atomic

    def record_write(path, value):
        writes.append(threading.current_thread().name)
        original(path, value)
    bridge_audit._write_json_atomic = record_write
    try:
        startup.bind(port)
        began = time.perf_counter()
        startup.begin()
        assert time.perf_counter() - began < .5  # begin never samples or writes inline
        assert _wait(port._quit.is_set)
        startup.finish()
    finally:
        bridge_audit._write_json_atomic = original
    caller = threading.current_thread().name
    assert port.calls and all(thread.startswith('bridge-audit-') for _name, thread in port.calls)
    assert writes and all(thread.startswith('bridge-audit-') and thread != caller for thread in writes)
    assert 'stop' not in [name for name, _thread in port.calls]  # idle never stops a service
    document = json.loads(plan.path.read_bytes())
    lines = startup.samples_path.read_text(encoding='utf-8').splitlines()
    assert document['completed'] and document['samples']['count'] == len(lines) >= 5
    assert json.loads(lines[-1])['label'] == 'final'
    assert not list(plan.path.parent.glob('*.pending'))


def test_finish_without_any_action_still_writes_a_bounded_final_file(tmp_path):
    plan = _plan(tmp_path, action='')
    startup = StartupAudit(SimpleNamespace(kind='audit'), plan)
    startup.finish()
    startup.finish()  # idempotent
    document = json.loads(plan.path.read_bytes())
    assert document['completed'] is True and document['origin_monotonic'] is None
    assert document['samples']['count'] == 0 and document['cpu']['full'] is None


# -- CPU accounting format ------------------------------------------------------------

def _sample(elapsed_s, *, bridge, transaction=0, audit_ns=0, runtime=0, publisher=0, log_work=0,
            process=0, label='periodic'):
    return {'label': label, 'elapsed_ms': int(elapsed_s * 1000),
            'monotonic_ns': 1_000_000_000_000 + int(elapsed_s * 1e9), 'process_cpu_ns': process,
            'threads': {'available': True, 'classes': {'bridge': bridge, 'transaction': transaction,
                                                       'audit': audit_ns, 'runtime': runtime}},
            'components': {**dict.fromkeys(bridge_audit.COMPONENT_KEYS, 0),
                           'publisher_thread_cpu_ns': publisher, 'log_work_cpu_ns': log_work}}


def test_cpu_report_fields_windows_and_formula():
    samples = [_sample(t, bridge=int(t * 0.05e9), publisher=int(t * 0.01e9),
                       log_work=int(t * 0.002e9), transaction=int(t * 0.5e9),
                       process=int(t * 1.1e9), runtime=int(t * .3e9))
               for t in (0, 10, 30, 45, 60, 90, 120, 150)]
    samples.append(_sample(155, bridge=int(155 * 0.05e9) + 50_000_000, publisher=0,
                           log_work=int(155 * 0.002e9), process=int(155 * 1.1e9), label='final'))
    report = cpu_report(samples, interval_ms=250)
    assert set(report) == {'definition', 'windows', 'full', 'through_exit'}
    assert [window['window_s'] for window in report['windows']] == [[0, 30], [30, 60], [60, 120],
                                                                     [120, 150]]
    for window in report['windows']:
        assert window['available'] and window['complete']
        assert window['publisher_cores'] == pytest.approx(0.01)
        assert window['bridge_cores_lower'] == pytest.approx(0.05)
        assert window['bridge_cores'] == pytest.approx(0.052)  # threads + charged log work
        assert window['transaction_cores'] == pytest.approx(0.5)
        assert window['runtime_cores'] == pytest.approx(0.3)
        assert window['process_cores'] == pytest.approx(1.1)  # cores, never / CPU count
        # process minus the four classes (0.05 + 0.5 + 0 + 0.3): CPU no class saw
        assert window['unclassified_cores'] == pytest.approx(0.25)
        assert window['upper_incomplete'] is False and window['components_missing_at'] == []
        assert {'start_elapsed_ms', 'end_elapsed_ms', 'elapsed_ns', 'component_cores',
                'cpu_ns', 'audit_cores', 'log_work_cores'} <= set(window)
    assert report['windows'][2]['start_elapsed_ms'] == 60_000
    assert report['windows'][2]['end_elapsed_ms'] == 120_000
    assert report['full']['elapsed_ns'] == 150 * 10**9
    assert report['full']['cpu_ns']['bridge_lower'] == int(150 * 0.05e9)
    assert report['through_exit']['window_s'] == 'through_exit'
    assert report['through_exit']['end_elapsed_ms'] == 155_000
    assert 'not divided by processor count' in report['definition']['unit']


def test_short_run_marks_windows_incomplete_or_unavailable():
    samples = [_sample(t, bridge=int(t * 1e7)) for t in (0, 15, 30, 40)]
    windows = cpu_report(samples, interval_ms=250)['windows']
    assert windows[0]['available'] and windows[0]['complete']
    assert windows[1]['available'] and not windows[1]['complete']
    assert windows[1]['end_elapsed_ms'] == 40_000
    assert windows[2] == {'window_s': [60, 120], 'available': False}
    assert windows[3] == {'window_s': [120, 150], 'available': False}


def test_cpu_window_without_thread_times_reports_unknown_not_zero():
    start = _sample(0, bridge=0)
    end = _sample(10, bridge=0, publisher=10**8)
    start['threads'] = end['threads'] = {'available': False, 'classes': {}}
    window = cpu_window(start, end)
    assert window['bridge_cores'] is None and window['bridge_cores_lower'] is None
    assert window['publisher_cores'] == pytest.approx(0.01)


def test_thread_classes_are_disjoint_and_exited_threads_keep_counting():
    assert bridge_audit.thread_family('MainThread', main=True) == 'bridge-main'
    assert bridge_audit.thread_family('bridge-jobs-normal-3') == 'bridge-jobs-normal'
    assert bridge_audit.thread_family('Thread-7 (_read)') == 'Thread(_read)'
    cases = {'bridge-main': 'bridge', 'bridge-sampler': 'bridge', 'bridge/logs': 'bridge',
             'bridge-traffic-sampler': 'bridge', 'bridge-log-batches': 'bridge',
             'bridge-ingress-requests': 'bridge', 'bridge-pipe-read': 'bridge',
             'bridge-jobs-normal': 'bridge', 'Thread(_read)': 'bridge',
             'bridge-jobs-change': 'transaction', 'bridge-jobs-control': 'transaction',
             'bridge-jobs-session-end': 'transaction', 'bridge-audit-sampler': 'audit',
             'bridge-audit-control': 'transaction', 'bridge-audit-finish': 'audit',
             'bridge-network-recovery': 'transaction', 'bridge-settings-apply': 'transaction',
             'bridge-exit-probe': 'transaction', 'bridge-exit-verify': 'transaction',
             'bridge-exit-auto': 'transaction', 'bridge-onboarding-expiry': 'bridge',
             'bridge-diagnostic-cancel': 'bridge',
             'tier3-expansion': 'runtime', 'Thread(worker)': 'runtime'}
    assert {family: bridge_audit.thread_class(family) for family in cases} == cases
    values = {}
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name='bridge-jobs-change')
    worker.start()
    values[worker] = 40
    # T-P2-H2 (31b-#15): only this test's own thread reports CPU. Threads left
    # by earlier tests (possibly also named bridge-jobs-change) report nothing
    # (None), so the totals do not depend on the execution order.
    reader = ThreadCpuReader(read=lambda thread: values.get(thread))
    first = reader.sample()
    assert first['classes']['transaction'] == 40
    assert first['families'] == {'bridge-jobs-change': 40}
    release.set()
    worker.join()
    values[worker] = None  # an exited thread's handle reports nothing new
    second = reader.sample()
    assert second['classes']['transaction'] == 40 and second['families']['bridge-jobs-change'] == 40


@pytest.mark.skipif(os.name != 'nt', reason='GetThreadTimes')
def test_real_thread_cpu_is_measured_per_thread():
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(2000))
    reader = ThreadCpuReader()
    worker = threading.Thread(target=spin, name='bridge-test-spin')
    worker.start()
    try:
        assert _wait(lambda: reader.sample()['families'].get('bridge-test-spin', 0) > 0, 10)
    finally:
        stop.set()
        worker.join()
    sample = reader.sample()
    assert sample['available'] and sample['classes']['bridge'] >= sample['families']['bridge-test-spin']
    reader.close()


def test_session_port_reads_public_state_only():
    from bridge_audit import BridgeSessionAuditPort

    class State:
        def snapshot(self, topic):
            if topic == 'tiers.summary':
                raise ValueError('topic is not an allowed subscribed snapshot')
            return {'topic': topic, 'data': {'runtime': {'startup_phase': 'collecting'}}}

    executed = []

    class Executor:
        def run_internal_mutation(self, work, *, abandoned):
            assert abandoned() is False
            return True, work()

        def wait(self, op, timeout):
            return {'state': 'done', 'result': {'x': 1}, 'error': None}

    session = SimpleNamespace(
        publisher=SimpleNamespace(frames=3, ticks=9), sampler=None, logs=None, traffic=None,
        state=State(), subscribed_topics={'tiers.summary', 'service.state'}, executor=Executor(),
        shutdown=SimpleNamespace(quitting=False, request_stop=lambda: SimpleNamespace(
            accepted=False, skipped_reason='not_running', op=None), request_quit=Mock()),
        start=lambda args, context: executed.append(context.op) or {'ok': True})
    port = BridgeSessionAuditPort(session)
    assert port.collection_metrics() is None  # collectors not connected yet
    assert port.publisher_counters() == {'frames': 3, 'ticks': 9}
    assert port.published_snapshot('service.state') == {'runtime': {'startup_phase': 'collecting'}}
    assert port.published_snapshot('tiers.summary') is None
    assert port.subscribed_topics() == ['service.state', 'tiers.summary']
    assert port.run_start_action(lambda: False) is None and executed == []
    assert port.run_start_action(lambda: True) == {'ok': True} and executed == ['audit-startup']
    assert port.request_stop_and_wait(1) == {'accepted': False, 'skipped_reason': 'not_running'}
    port.request_quit()
    session.shutdown.request_quit.assert_called_once_with()


# -- lab harness: Job reaping and dry-run ---------------------------------------------------

def _require_lab():
    # The P2-ab lab harness is local evidence under .build/, which is never
    # published; the public tree and CI skip the tests that drive it.
    if not (LAB / 'run_sidecar_audit.py').is_file():
        pytest.skip('the P2-ab lab harness lives under .build/ (local evidence only)')


def _load_lab_module(name):
    import importlib.util
    _require_lab()
    spec = importlib.util.spec_from_file_location('p2ab_' + name, LAB / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job objects')
def test_harness_job_reaps_every_child_process(tmp_path):
    from tier3_windows_job import WindowsJob
    host = _load_lab_module('p2ab_host')
    python = sys._base_executable
    orphan = ('import subprocess, sys, time; '
              'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
              'time.sleep(.3)')
    with WindowsJob() as job, (tmp_path / 'out.log').open('wb') as log:
        process = host.launch_suspended_in_job(job, [python, '-c', orphan], stdout=log, stderr=log)
        assert process.pid in job.snapshot().process_ids  # assigned before it could run
        assert process.wait(timeout=30) == 0
        result = host.reap_job(job, grace_seconds=.5)
        # The orphan (plus the console host Windows gives it) outlived the root.
        assert result['remaining_before_terminate'] >= 1 and result['terminated_leftovers']
        assert result['remaining_after'] == 0 and result['all_reaped']
    with WindowsJob() as job, (tmp_path / 'clean.log').open('wb') as log:
        process = host.launch_suspended_in_job(job, [python, '-c', 'pass'], stdout=log, stderr=log)
        assert process.wait(timeout=30) == 0
        result = host.reap_job(job, grace_seconds=5)
        assert result == dict(remaining_before_terminate=0, leftover_pids=[],
                              terminated_leftovers=False, remaining_after=0, all_reaped=True)


def _harness(*argv, timeout=120):
    _require_lab()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('TESSERACT_', 'V87_', 'TIER3_LAB_'))}
    return subprocess.run([sys.executable, '-B', str(LAB / 'run_sidecar_audit.py'), *argv],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def test_harness_dry_run_source_needs_no_launch_or_network(tmp_path):
    result = _harness('dry-src', '--dry-run', '--runs-dir', str(tmp_path), '--seconds', '150')
    assert result.returncode == 0, result.stderr
    work = tmp_path / 'dry-src'
    summary = json.loads((work / 'summary.json').read_text(encoding='utf-8'))
    invocation = json.loads((work / 'invocation.json').read_text(encoding='utf-8'))
    assert summary == {'mode': 'dry-run', 'launched': False, 'ok': True, 'network_used': False}
    checks = invocation['preflight']
    assert checks['ok'] and checks['scope_is_audit'] and checks['scope_never_takes_over_host_network']
    assert checks['identity']['audit_guard_before_freeze_support']
    assert checks['audit_settings']['action'] == 'start-services'
    assert checks['audit_settings']['duration_ms'] == 150_000
    environment = invocation['environment']
    assert environment['TESSERACT_ISOLATED_AUDIT_MANIFEST'] == str((work / 'audit-manifest.json').resolve())
    assert environment['TESSERACT_STARTUP_AUDIT_QUIT'] == '1'
    assert invocation['planned_argv'][2].endswith('runtime_main.pyw')
    # Fresh profile, no launch artefacts at all.
    assert sorted(path.name for path in work.iterdir()) == [
        'audit-manifest.json', 'config', 'data', 'invocation.json', 'summary.json']
    assert sorted(path.name for path in (work / 'data').iterdir()) == ['mihomo']
    for secretive in ('mixed', 'controller'):
        assert isinstance(invocation['ports'][secretive], int)
    assert 'secret' not in json.dumps(invocation['environment'])


@pytest.mark.parametrize('matching', [True, False])
def test_harness_dry_run_frozen_identity(tmp_path, matching):
    exe = tmp_path / 'sidecar' / 'tesseract-runtime.exe'
    exe.parent.mkdir()
    exe.write_bytes(b'controlled frozen sidecar bytes')
    declared = hashlib.sha256(exe.read_bytes()).hexdigest() if matching else '0' * 64
    (exe.parent / 'tier3_build_id.json').write_text(json.dumps(
        {'build_id': 'b' * 64, 'declared_executable_sha256': declared}), encoding='utf-8')
    result = _harness('dry-frozen', '--dry-run', '--sidecar', 'frozen', '--sidecar-exe', str(exe),
                      '--runs-dir', str(tmp_path / 'runs'))
    assert result.returncode == (0 if matching else 2), result.stderr
    invocation = json.loads((tmp_path / 'runs' / 'dry-frozen' / 'invocation.json').read_text(
        encoding='utf-8'))
    assert invocation['preflight']['identity']['identity_match'] is matching
    assert invocation['planned_argv'][0] == str(exe.resolve())


def test_harness_refuses_live_profile_and_r8_directories(tmp_path, monkeypatch):
    harness = _load_lab_module('run_sidecar_audit')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    with pytest.raises(SystemExit):
        harness.check_runs_dir(tmp_path / 'roaming' / 'Tesseract' / 'runs')
    with pytest.raises(SystemExit):
        harness.check_runs_dir(ROOT / '.build' / 'claude-r8' / 'host-runs')
    assert harness.check_runs_dir(tmp_path / 'runs') == (tmp_path / 'runs').resolve()
    with pytest.raises(SystemExit):
        harness.parse_args(['measured-without-seed'])


# -- review round 1 (T-P2-32 fixes) -----------------------------------------------------

def test_missing_boundary_components_make_the_gate_unknown_not_the_lower_bound():
    start = _sample(0, bridge=0, log_work=0)
    end = _sample(10, bridge=int(0.5e9), log_work=int(0.2e9))
    both = cpu_window(start, end)
    assert both['bridge_cores'] == pytest.approx(0.07)
    assert both['bridge_cores_lower'] == pytest.approx(0.05) and both['upper_incomplete'] is False
    for side in ('start', 'end'):
        first, last = dict(start), dict(end)
        (first if side == 'start' else last)['components'] = None
        window = cpu_window(first, last)
        # Never silently the lower bound: the gate value is unknown and flagged.
        assert window['bridge_cores'] is None and window['upper_incomplete'] is True
        assert window['bridge_cores_lower'] == pytest.approx(0.05)
        assert window['bridge_cores'] != window['bridge_cores_lower']
        assert window['log_work_cores'] is None and window['cpu_ns']['bridge_upper'] is None
        assert window['components_missing_at'] == [side]
    samples = [_sample(t, bridge=int(t * 0.05e9), log_work=int(t * 0.02e9)) for t in (0, 30, 60)]
    samples[1]['components'] = None  # the 30 s boundary sample lost its counters
    report = cpu_report(samples, interval_ms=250)
    for window in report['windows'][:2]:
        assert window['bridge_cores'] is None and window['upper_incomplete'] is True
    assert report['full']['bridge_cores'] == pytest.approx(0.07)


class FlakyMetricsPort(FakePort):
    def __init__(self, audit_object=None, *, failures=0):
        super().__init__(audit_object)
        self.failures = failures

    def collection_metrics(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError('deque mutated during iteration')
        return super().collection_metrics()


def _run_idle(tmp_path, port_factory, **plan_args):
    plan = _plan(tmp_path, **{'action': 'idle', 'duration_ms': 300, 'interval_ms': 50,
                              'quit_when_done': True, **plan_args})
    startup = StartupAudit(SimpleNamespace(kind='audit'), plan)
    port = port_factory(startup)
    startup.bind(port)
    startup.begin()
    assert _wait(port._quit.is_set)
    startup.finish()
    document = json.loads(plan.path.read_bytes())
    lines = [json.loads(line) for line in
             startup.samples_path.read_text(encoding='utf-8').splitlines()]
    return startup, port, document, lines


def test_a_transient_collection_metrics_race_is_retried_once(tmp_path):
    _startup, _port, document, lines = _run_idle(
        tmp_path, lambda startup: FlakyMetricsPort(startup, failures=1))
    assert lines[0]['components'] is not None  # the retry recovered the first sample
    assert not [error for error in document['errors'] if error['stage'].startswith('metrics')]
    assert document['cpu']['full']['bridge_cores'] is not None


def test_port_reads_fail_independently_and_boundaries_are_flagged(tmp_path):
    _startup, _port, document, lines = _run_idle(
        tmp_path, lambda startup: FlakyMetricsPort(startup, failures=10**9))
    # A failing collection_metrics loses only the components, never the sample.
    assert all(line['components'] is None for line in lines)
    assert all(line['publisher']['frames'] >= 1 and line['service'] and line['tiers']
               for line in lines)
    metrics_errors = [error for error in document['errors']
                      if error['stage'] == 'metrics:collection_metrics']
    assert metrics_errors and metrics_errors[0]['error'] == 'RuntimeError'
    # T-P2-H2 (C1-#11): elapsed_ms is wall time on the coarse Windows clock
    # (0 or a 15.6 ms tick, more under load); only its type is a contract.
    elapsed = metrics_errors[0]['elapsed_ms']
    assert metrics_errors[0]['boundary'] is True
    assert type(elapsed) is int and elapsed >= 0
    assert document['error_counts']['metrics:collection_metrics'] == len(lines)
    full = document['cpu']['full']
    assert full['bridge_cores'] is None and full['upper_incomplete'] is True
    assert full['bridge_cores_lower'] is not None
    assert document['cpu']['through_exit']['bridge_cores'] is None


def test_a_failed_write_never_stops_sampling_or_the_quit(tmp_path, monkeypatch):
    original = bridge_audit._write_json_atomic
    attempts = []

    def flaky(path, value):
        attempts.append(value['completed'])
        if len(attempts) == 1:
            raise PermissionError('sharing violation')
        original(path, value)
    monkeypatch.setattr(bridge_audit, '_write_json_atomic', flaky)
    _startup, port, document, lines = _run_idle(tmp_path, FakePort)
    assert 'quit' in [name for name, _thread in port.calls]
    assert document['completed'] is True and document['quit_requested'] is True
    assert document['observation_end']['reason'] == 'duration_elapsed'
    assert {'stage': 'observation_end_write', 'error': 'PermissionError'} in document['errors']
    assert lines[-1]['label'] == 'final' and document['samples']['count'] == len(lines) >= 5
    assert attempts[0] is False and attempts[-1] is True


def test_a_crashed_sampler_still_quits_and_is_never_completed(tmp_path):
    plan = _plan(tmp_path, action='idle', duration_ms=60_000, interval_ms=50, quit_when_done=True)
    startup = StartupAudit(SimpleNamespace(kind='audit'), plan)
    original = startup._observe_firsts
    seen = []

    def crash_once(sample):
        seen.append(sample['label'])
        if len(seen) == 3:
            raise RuntimeError('unexpected')
        original(sample)
    startup._observe_firsts = crash_once
    port = FakePort(startup)
    startup.bind(port)
    startup.begin()
    assert _wait(port._quit.is_set)
    assert _wait(plan.path.exists)
    assert json.loads(plan.path.read_bytes())['completed'] is False
    startup.finish()
    document = json.loads(plan.path.read_bytes())
    assert document['completed'] is False and document['sampler_failure'] == 'RuntimeError'
    assert document['observation_end']['reason'] == 'sampler_failed'
    assert document['quit_requested'] is True
    assert {'stage': 'sampler', 'error': 'RuntimeError'} in document['errors']
    lines = startup.samples_path.read_text(encoding='utf-8').splitlines()
    assert json.loads(lines[-1])['label'] == 'final'  # finish() still took the exit sample


def test_marker_os_error_is_recorded_and_later_starts_fail_closed(audit, tmp_path, monkeypatch):
    scope, _ = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   settings=StartupAuditSettings(None, 60_000, 50, False, ''),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    startup.bind(FakePort(startup))
    original = bridge_audit.replace_with_retry

    def disk_full(_pending, _destination):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(bridge_audit, 'replace_with_retry', disk_full)
    action = Mock(return_value='started')
    with pytest.raises(OSError, match='No space'):
        startup.run_start(action)
    monkeypatch.setattr(bridge_audit, 'replace_with_retry', original)
    # The marker failure locks the audit: no unmarked, unsampled start later.
    with pytest.raises(PermissionError, match='unaudited start'):
        startup.run_start(action)
    action.assert_not_called()
    assert not any((scope.data_dir / name).exists() for name in MARKERS)
    startup.finish()
    document = json.loads(plan.path.read_bytes())
    assert document['errors'][0]['stage'] == 'markers' and document['errors'][0]['type'] == 'OSError'
    assert document['marker_failure'] == 'OSError' and document['origin_monotonic'] is None
    assert 'start_refused_after_marker_failure' in document['notes']


def test_runtime_start_failure_after_markers_keeps_restarts_allowed(audit, tmp_path):
    scope, _ = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   settings=StartupAuditSettings(None, 60_000, 50, False, ''),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    startup.bind(FakePort(startup))

    def failing_start():
        raise RuntimeError('runtime start failed')
    with pytest.raises(RuntimeError):
        startup.run_start(failing_start)
    assert all((scope.data_dir / name).exists() for name in MARKERS)
    assert startup.run_start(lambda: 'again') == 'again'
    startup.finish()
    document = json.loads(plan.path.read_bytes())
    assert document['marker_failure'] is None and document['completed'] is True


@pytest.mark.parametrize('first', ['host', 'environment'])
def test_environment_action_and_host_start_share_the_real_mutation_domain(audit, tmp_path, first):
    from bridge_audit import BridgeSessionAuditPort
    from bridge_jobs import JobEventRecorder, JobExecutor
    scope, _ = audit
    guard = _hook()
    plan = replace(prepare_startup_audit(scope, guard, dict(os.environ)),
                   settings=StartupAuditSettings(None, 60_000, 50, False, 'start-services'),
                   path=tmp_path / 'out' / 'sidecar.json')
    startup = StartupAudit(scope, plan)
    executor = JobExecutor(JobEventRecorder(), epoch='test-epoch')
    inside, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak, starts = [0], [0], []

    def runtime_start():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        starts.append(threading.current_thread().name)
        inside.set()
        release.wait(5)
        with lock:
            active[0] -= 1
        return {'failure_code': None, 'startup_phase': 'collecting'}

    def session_start(_args, _context):  # BridgeSession.start -> audit.run_start(access.start)
        return startup.run_start(runtime_start)

    class State:
        def snapshot(self, topic):
            raise ValueError(topic)
    executor.register('service.start', session_start)
    session = SimpleNamespace(
        executor=executor, start=session_start, subscribed_topics={'service.state'},
        shutdown=SimpleNamespace(quitting=False), publisher=None, sampler=None, logs=None,
        traffic=None, state=State())
    startup.bind(BridgeSessionAuditPort(session))
    try:
        if first == 'host':
            host = executor.submit('service.start', {})
            assert host.accepted and inside.wait(5)
            startup.begin()  # contends for the same mutation lock
        else:
            startup.begin()
            assert inside.wait(5)
            host = executor.submit('service.start', {})
            assert host.accepted
        time.sleep(.3)
        assert len(starts) == 1  # the other side waits behind the transaction
        release.set()
        assert executor.wait(host.op, 10)['state'] == 'done'
        startup._controller.join(10)
        assert not startup._controller.is_alive()
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 2)
    startup.finish()
    assert peak[0] == 1
    document = json.loads(plan.path.read_bytes())
    if first == 'host':
        assert starts == ['bridge-jobs-change']  # exactly one runtime start
        assert document['action']['source'] == 'command'
        assert 'host_start_preceded_environment_action' in document['notes']
    else:
        # The host's later explicit start is serialized behind, never overlapping.
        assert starts == ['bridge-audit-control', 'bridge-jobs-change']
        assert document['action']['source'] == 'environment'
    marker = json.loads((scope.data_dir / MARKERS[0]).read_bytes())
    assert document['marker'] == marker


def _production_steps(monkeypatch, calls, scope, *, guard=None, transport=None):
    """Patch the class main() instantiates when steps is None; production
    branches (prepare_startup_audit default, session wiring) stay active."""
    original = bridge_main.Steps
    lease = SimpleNamespace(close=lambda: calls.append('release'))

    def recovery(_scope):
        calls.append('RT-1')
        return SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None))
    monkeypatch.setattr(bridge_main, 'Steps', lambda: original(
        parse=lambda _argv: SimpleNamespace(lang='en_US'), scope=lambda: scope,
        acquire=lambda _scope: SimpleNamespace(status='acquired', lease=lease),
        logging=lambda _scope: None,
        audit=lambda _scope: calls.append('guard') or guard,
        recovery=recovery, transport=transport or Mock(), loop=Mock()))
    assert bridge_main.Steps().startup_audit is None


def test_production_default_prepare_refuses_before_rt1(tmp_path, monkeypatch, capsys):
    calls = []
    scope = SimpleNamespace(kind='custom', id='c' * 24, may_take_over_host_network=False,
                            data_dir=tmp_path, config_dir=tmp_path)
    _production_steps(monkeypatch, calls, scope)

    def refuse(_scope, guard):
        calls.append(('prepare', guard))
        raise PermissionError('startup audit requires the isolated audit guard')
    monkeypatch.setattr(bridge_main, 'prepare_startup_audit', refuse)
    assert bridge_main.main([]) == 4
    assert json.loads(capsys.readouterr().err) == {'code': 'startup_audit_refused'}
    assert calls == ['guard', ('prepare', None), 'release']  # RT-1 never ran


def test_production_main_wires_create_bind_begin_and_finishes_audit_before_guard(
        tmp_path, monkeypatch):
    import bridge_protocol as protocol
    calls = []
    scope = SimpleNamespace(kind='audit', id='a' * 32, may_take_over_host_network=False,
                            data_dir=tmp_path, config_dir=tmp_path,
                            hello_scope=lambda: {'kind': 'audit', 'id': 'a' * 32})
    plan = _plan(tmp_path, action='idle', duration_ms=1000, interval_ms=50, quit_when_done=True)

    def guard_finish():
        # The guard receipt is written only after the audit's final file.
        calls.append(('guard.finish', json.loads(plan.path.read_bytes())['completed']))
    guard = SimpleNamespace(finish=guard_finish)

    class Transport:
        hello = None

        def send(self, frame):
            calls.append(frame['t'])
            self.hello = frame
            return 'complete'

        def receive(self, timeout=None):
            now = int(time.time() * 1000)
            return {'t': 'hello_ack', 'epoch': self.hello['epoch'], 'proto': protocol.PROTOCOL_VERSION,
                    'accepted': True, 'produced_at_ms': now, 'sent_at_ms': now}

        def close(self):
            calls.append('transport.close')
    transport = Transport()
    _production_steps(monkeypatch, calls, scope, guard=guard, transport=lambda _args: transport)
    monkeypatch.setattr(bridge_main, 'prepare_startup_audit',
                        lambda _scope, value: calls.append(('prepare', value)) or plan)

    class RecordingAudit(StartupAudit):
        def bind(self, port):
            calls.append('bind')
            super().bind(port)

        def begin(self):
            calls.append('begin')
            super().begin()

        def finish(self):
            calls.append('audit.finish')
            super().finish()
    monkeypatch.setattr(bridge_audit, 'create_startup_audit',
                        lambda value, audit_plan: calls.append('create') or RecordingAudit(
                            value, audit_plan))

    class State:
        def snapshot(self, topic):
            raise ValueError(topic)

    class Session:
        def __init__(self, _scope, _report, _lease, _epoch):
            calls.append('session')
            self.startup_audit = self.shutdown = self.exit_code = None
            self.publisher = self.sampler = self.logs = self.traffic = None
            self.subscribed_topics, self.state = {'service.state'}, State()
            self.quit = threading.Event()

        def connect(self, _transport, finish_audit):
            calls.append('connect')
            self.finish_audit = finish_audit
            self.shutdown = SimpleNamespace(
                quitting=False, request_quit=lambda: (calls.append('quit'), self.quit.set()))

        def run(self):
            calls.append('run')
            assert self.quit.wait(10)
            self.finish_audit()  # exit_process -> finish_audit
            self.exit_code = 0
            return 0
    monkeypatch.setattr(bridge_main, 'BridgeSession', Session)
    assert bridge_main.main([]) == 0
    assert calls == ['guard', ('prepare', guard), 'RT-1', 'session', 'create', 'connect', 'bind',
                     'hello', 'begin', 'run', 'quit', 'audit.finish', ('guard.finish', True),
                     'audit.finish', ('guard.finish', True), 'transport.close', 'release']
    document = json.loads(plan.path.read_bytes())
    assert document['completed'] is True
    assert document['action'] == {'action': 'idle', 'source': 'environment'}
    assert document['observation_end']['reason'] == 'duration_elapsed'
    assert document['quit_requested'] is True and document['samples']['count'] >= 5
    assert _no_markers(tmp_path)


def test_thread_reader_counts_open_failures():
    reader = ThreadCpuReader(read=lambda thread: 1)
    reader._read, reader._api = None, (lambda _native: None, lambda _handle: None, lambda _handle: 1)
    sample = reader.sample()
    assert sample['open_failures'] == sample['tracked'] == len(threading.enumerate())
    assert sample['classes'] == dict.fromkeys(bridge_audit.THREAD_CLASSES, 0)


@pytest.mark.parametrize('current', [True, False])
def test_harness_frozen_preflight_checks_the_build_has_today_audit_sources(tmp_path, current):
    harness = _load_lab_module('run_sidecar_audit')
    exe = tmp_path / 'sidecar' / 'tesseract-runtime.exe'
    exe.parent.mkdir()
    exe.write_bytes(b'controlled frozen sidecar bytes')
    components = ({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                   for name in harness.AUDIT_ENTRY_SOURCES} if current
                  else {'bridge_audit.py': '0' * 64})
    (exe.parent / 'tier3_build_id.json').write_text(json.dumps(
        {'build_id': 'b' * 64, 'components': components,
         'declared_executable_sha256': hashlib.sha256(exe.read_bytes()).hexdigest()}),
        encoding='utf-8')
    result = _harness('dry-build', '--dry-run', '--sidecar', 'frozen', '--sidecar-exe', str(exe),
                      '--runs-dir', str(tmp_path / 'runs'))
    assert result.returncode == 0, result.stderr
    work = tmp_path / 'runs' / 'dry-build'
    invocation = json.loads((work / 'invocation.json').read_text(encoding='utf-8'))
    summary = json.loads((work / 'summary.json').read_text(encoding='utf-8'))
    build = invocation['preflight']['audit_build']
    assert build['current'] is current
    if current:
        assert 'warnings' not in summary and invocation['warnings'] == []
    else:
        assert build['stale'] == ['bridge_audit.py'] and build['missing'] == ['bridge_main.py']
        assert summary['warnings'][0].startswith('frozen_build_predates_audit_sources')
        assert 'WARNING' in result.stderr
        # A launch with a stale build is refused up front (nothing is started).
        launch = _harness('idle-stale', '--launch-idle', '--sidecar', 'frozen', '--sidecar-exe',
                          str(exe), '--runs-dir', str(tmp_path / 'runs'))
        assert launch.returncode == 2
        refused = json.loads((tmp_path / 'runs' / 'idle-stale' / 'summary.json').read_text(
            encoding='utf-8'))
        assert refused['launched'] is False and refused['ok'] is False


def test_harness_records_source_identity_for_ab_pairing(tmp_path):
    harness = _load_lab_module('run_sidecar_audit')
    result = _harness('dry-ident', '--dry-run', '--runs-dir', str(tmp_path))
    assert result.returncode == 0, result.stderr
    invocation = json.loads((tmp_path / 'dry-ident' / 'invocation.json').read_text(encoding='utf-8'))
    identity = invocation['source_identity']
    assert identity == harness.source_identity(harness.source_manifest())
    assert identity['files'] > 100 and len(identity['sha256']) == 64
    assert 'bridge_audit.py' in harness.source_manifest()


def test_harness_never_copies_alert_channel_credentials(tmp_path):
    import yaml
    harness = _load_lab_module('run_sidecar_audit')
    seed = tmp_path / 'seed'
    (seed / 'config').mkdir(parents=True)
    channel = {'type': 'ntfy', 'enabled': True,
               'config': {'enabled': True, 'topic': 'leak-topic', 'token': 'leak-token',
                          'webhook': 'https://example.invalid/leak-hook'}}
    (seed / 'config' / 'config.yaml').write_text(
        yaml.safe_dump({'alerts': {'channels': [channel]}}), encoding='utf-8')
    (seed / 'config' / 'config.local.yaml').write_text(
        yaml.safe_dump({'alerts': {'channels': [channel]}}), encoding='utf-8')
    work = tmp_path / 'work'
    work.mkdir()
    harness.prepare_profile(work, seed, [21001, 21002, 21003, 21004])
    for name in ('config.yaml', 'config.local.yaml'):
        text = (work / 'config' / name).read_text(encoding='utf-8')
        assert 'leak-' not in text
        assert yaml.safe_load(text)['alerts']['channels'] == []
