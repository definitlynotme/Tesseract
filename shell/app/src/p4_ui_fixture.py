"""T-P4-11 scripted stand-in sidecar ("侧车替身") for the P4 page end-to-end flows.

Debug shell only (`--dev-sidecar-script shell/app/src/p4_ui_fixture.py`, a release build refuses
the flag). Never constructs a runtime, never touches the network, the HKCU proxy or the firewall.
Same style as notify_fixture.py / host_fixture.py; outgoing DTO validation is bypassed in this
process only (the shell and the page still check every frame).

hello declares the v1 page capabilities (bridge_schema/fixtures/v1/valid/hello_v1_caps.json:
every v1 command, query and topic). Options are read at every launch from <state>/options.json:
  ui_language   true (default) | false: without it hello.caps.topics lacks `ui.language`, so the
                shell neither subscribes nor forwards host:language (criticism B3 negative case)
  lang          hello.lang and the stored ui.language (default zh_CN)
  phase         the first service.state: running (default) | stopped | failed
  start         service.start outcome: done (default) | failed (no_sources, like the isolated real
                sidecar) | hold (accepted, never finished)
  hold          command names that are accepted and never finished (in flight until the epoch ends)
  autostart_supported  autostart.state.supported (default true)
After a crash (op crash) the next launch starts `stopped` (the shell restarts the sidecar in the
stopped state). Epoch = "p4ui-<launch>-<pid>"; <launch> counts launches in <state>.

World data: every resyncable topic gets a snapshot right after `subscribed` (so the shell's seed
ends with rt_seed_done reason=all): v1 valid topic fixtures, overridden by the coherent running
world of ui/test/fixtures/scenarios/overview_running.json. While the service runs it sends
service.state and traffic.tick every second (the page and the tray judge them stale after 3 s).

Commands: queries answer from the v1 response fixtures (config.snapshot / autostart.state /
logs.tail / logs.query from this fixture's state); jobs answer ack + jobs edge (service.start /
stop / restart move service.state; settings.apply merges the changes into <state>/config.json,
sends ui.language when the language changed and the topic is declared, then config.changed);
snapshot_req answers snapshot_res with the current value; app.quit / app.sessionEnd end with bye 0.
The separate cancel frame (PROTOCOL section 1 / 6: {t:'cancel', id, op}) is answered by an ack with
the same id: accepted with a jobs edge state=cancelled when the op is a held (in-flight) job,
otherwise accepted=false with skipped_reason=not_in_flight.
Shell-only commands (alerts.desktopDelivered, ui.presence, config.snapshot from the shell) are
answered and recorded.

Control: every <state>/control/*.json (sorted by name) is executed once, then renamed to *.done
(a failure is written to *.error instead). Ops:
  {"op": "push", "topic", "kind", "data", "key"?}   one evt (rev / seq assigned here)
  {"op": "service", "phase", "failure"?}            a service.state snapshot for that phase
  {"op": "desktop", "delivery_id", "severity", "title", "detail", "target"}   alerts.desktop edge
  {"op": "language", "language"}                    ui.language edge (only when declared)
  {"op": "logs", "n", "level"?, "category"?}        one logs.batch with n records now (default info /
                                                    runtime; level error feeds the log page's sidebar badge)
  {"op": "log_rate", "per_s"}                       a steady logs.batch stream (0 stops it)
  {"op": "stale", "topic", "key"?, "fails"}         (T-P4-11 10-13, list_stale) an error{snapshot_required,
                                                    params:{topic, tier?}} without id for a list slot: the
                                                    shell drops the slot and asks for a replacement
                                                    (snapshot_req) itself; the next `fails` replacement
                                                    requests of that slot are answered snapshot_res with
                                                    error snapshot_required (3 in a row: the shell says
                                                    rt:stale exhausted), later ones with the snapshot
  {"op": "job", "name", "state", "error"?, "result"?, "owner"?, "req_id"?}
                                                    (T-P4-11 10-13) a jobs edge of a job this page did not
                                                    start (default owner 'shell', req_id shell-ext-<n>:
                                                    the shell's own requests use owner shell, e.g. a
                                                    tray stop; PROTOCOL has no 'tray' owner)
  {"op": "crash", "code"}                           exit without bye (the shell restarts us)
  {"op": "options", "values": {...}}                change options of this launch (e.g. start: failed)
<state>/requests-<launch>.jsonl keeps a summary of every request received (t, cmd, from, a few
argument facts); never the raw frames. <state>/ready-<launch> is written once the seed is sent.
<state> is --state, else $TESSERACT_DATA_DIR/p4-ui-fixture.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
import bridge_transport
from bridge_transport import PipeTransport

# This fixture bypasses outgoing DTO validation in its own process only.
bridge_transport.protocol.encode_frame = lambda frame: json.dumps(frame).encode() + b'\n'
VALID = REPO / 'bridge_schema' / 'fixtures' / 'v1' / 'valid'
WORLD = REPO / 'ui' / 'test' / 'fixtures' / 'scenarios' / 'overview_running.json'
RECOVERY = {'proxy': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0},
            'firewall': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0}}
LIST_TOPICS = {'bastion.sources', 'config.items', 'nodes.list', 'subscriptions.list'}
KEYED = {'nodes.list', 'subscriptions.list'}
LAYERS = ['vanguard', 'sentinel', 'bastion', 'gateway']
EVENT_ONLY = {'tier.transition', 'config.changed', 'jobs', 'logs.batch'}
SHELL_ONLY = {'alerts.desktop', 'update.prepareExit', 'shutdown', 'ui.language'}
NOT_FOUND = {'code': 'not_allowed', 'message_key': 'errors.bridge.not_allowed', 'params': {}}

parser = argparse.ArgumentParser()
parser.add_argument('--ipc-handle', type=int)
parser.add_argument('--shell-pid', type=int)
parser.add_argument('--scope')
parser.add_argument('--state')
args, _unknown = parser.parse_known_args()  # the shell also passes --legacy-gate inherited
state = Path(args.state) if args.state else Path(os.environ['TESSERACT_DATA_DIR']) / 'p4-ui-fixture'
control = state / 'control'
control.mkdir(parents=True, exist_ok=True)
launch = 1 + len(list(state.glob('launch-*')))
(state / f'launch-{launch}').write_text(str(os.getpid()))
try:
    options = json.loads((state / 'options.json').read_text(encoding='utf-8'))
except (OSError, ValueError):
    options = {}
crashed = state / 'crashed'
restarted = crashed.exists()
if restarted:
    crashed.unlink()
LANG = options.get('lang', 'zh_CN')
DECLARE_LANGUAGE = options.get('ui_language', True) is not False
HOLD = set(options.get('hold') or [])
HELD = {}   # op -> (request frame, command name) of the held jobs still in flight (cancel frame)
FAIL_SNAP = {}  # (topic, key) -> replacement requests still to answer with snapshot_required (op stale)
EXTERNAL = {'n': 0}  # jobs started elsewhere (op job)
pipe = PipeTransport(args.ipc_handle, args.shell_pid)
epoch = f'p4ui-{launch}-{os.getpid()}'
revisions, sequences = {}, {}
now_ms = lambda: int(time.time() * 1000)


def load(name):
    return json.loads((VALID / name).read_text(encoding='utf-8'))


HELLO = load('hello_v1_caps.json')
COMMANDS = list(HELLO['caps']['commands'])
TOPICS = [t for t in HELLO['caps']['topics'] if DECLARE_LANGUAGE or t != 'ui.language']

# ---- the world: v1 topic fixtures, then the coherent running overview -------------------------
world = {}
for path in VALID.glob('topic_*.json'):
    frame = json.loads(path.read_text(encoding='utf-8'))
    world[(frame['topic'], frame.get('key'))] = frame['data']
try:
    for step in json.loads(WORLD.read_text(encoding='utf-8'))['steps']:
        frame = step.get('deliver') or {}
        if frame.get('t') == 'evt':
            world[(frame['topic'], frame.get('key'))] = frame['data']
except (OSError, ValueError, KeyError):
    pass
for topic in KEYED:
    for layer in LAYERS:
        world.setdefault((topic, layer), {'items': []})
RUNNING = copy.deepcopy(world[('service.state', None)])

# ---- this fixture's saved settings (config.snapshot), kept across launches ----------------------
config_file = state / 'config.json'
if config_file.exists():
    config = json.loads(config_file.read_text(encoding='utf-8'))
else:
    snap = load('p4c1_response_config.snapshot.json')['data']
    config = {'rev': 1, 'values': copy.deepcopy(snap['values']), 'secret_fields_set': list(snap['secret_fields_set'])}
    config['values']['ui'] = {'language': LANG, 'start_in_tray_on_autostart': False}
    config['values']['autostart'] = {'enabled': False}
    config_file.write_text(json.dumps(config), encoding='utf-8')

logs = []  # records sent this launch (logs.tail / logs.query answer from them)
stream = {'per_s': 0, 'next': 0.0}


def save_config():
    tmp = config_file.with_suffix('.tmp')
    tmp.write_text(json.dumps(config), encoding='utf-8')
    tmp.replace(config_file)


def stamp(frame):
    now = now_ms()
    return dict(epoch=epoch, produced_at_ms=now, sent_at_ms=now, **frame)


def send(frame):
    frame = stamp(frame)
    while pipe.send(frame) == 'busy':
        time.sleep(.002)
    while pipe.poll_write() == 'pending':
        time.sleep(.002)


def record(entry):
    with (state / f'requests-{launch}.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(entry) + '\n')


def event(topic, kind, data, key=None):
    slot = f'{topic}#{key}' if key else topic
    revisions[slot] = revisions.get(slot, 0) + 1
    frame = dict(t='evt', topic=topic, kind=kind, rev=revisions[slot], data=data)
    if key:
        frame['key'] = key
    if kind not in ('snapshot', 'patch'):
        sequences[topic] = sequences.get(topic, 0) + 1
        frame['seq'] = sequences[topic]
    return frame


def emit(topic, kind, data, key=None):
    send(event(topic, kind, data, key))


# ---- service state --------------------------------------------------------------------------
phase = {'value': 'stopped' if restarted else options.get('phase', 'running'), 'failure': None}
FAILURE = {'code': 'no_sources', 'reason_code': 'no_sources', 'message_key': 'errors.start.no_sources', 'params': {}}


def service_state():
    s = copy.deepcopy(RUNNING)
    p = phase['value']
    live = p in ('starting', 'running')
    s['intent']['desired_running'] = live
    s['intent']['stop_requested'] = p == 'stopping'
    s['runtime'].update(runtime_running=p == 'running', startup_phase='collecting' if p == 'starting' else p,
                        shutdown_cleanup_pending=False, failure_code=None, failure=None)
    if p == 'failed':
        failure = phase['failure'] or FAILURE
        s['runtime'].update(failure_code=failure['code'], failure=failure)
    s['core']['running'] = p == 'running'
    if p != 'running':
        s['core']['pid'] = None
    s['readiness']['state'] = 'ready' if p == 'running' else 'fail_closed'
    s['freshness'].update(sampled_at_ms=now_ms(), stale=False, uptime_s=120 if p == 'running' else None)
    return s


def send_state():
    emit('service.state', 'snapshot', service_state())


def tick():
    data = copy.deepcopy(world.get(('traffic.tick', None)) or {})
    data.update(t_ms=now_ms(), running=phase['value'] == 'running')
    emit('traffic.tick', 'snapshot', data)


def current(topic, key=None):
    if topic == 'service.state':
        return service_state()
    data = copy.deepcopy(world.get((topic, key)) or world.get((topic, None)) or {})
    if topic in LIST_TOPICS and 'items' not in data:
        data = {'items': []}
    return data


def seed():
    for topic in TOPICS:
        if topic in EVENT_ONLY or topic in SHELL_ONLY:
            continue
        if topic in KEYED:
            for layer in LAYERS:
                emit(topic, 'snapshot', current(topic, layer), layer)
        else:
            emit(topic, 'snapshot', current(topic))
    if DECLARE_LANGUAGE:
        emit('ui.language', 'snapshot', {'language': config['values']['ui'].get('language', LANG), 'config_rev': config['rev']})
    push_logs(3, 'fixture seed')


def push_logs(n, title='fixture line', level='info', category='runtime'):
    records = []
    base = logs[-1]['seq'] if logs else 0
    for i in range(n):
        seq = base + i + 1
        ts = now_ms()
        # a key-event record (not log.raw): it shows in the log page's event view (关键事件)
        records.append({'seq': seq, 'ts_ms': ts, 'rx_ms': ts, 'key': 'fixture.line', 'source': 'TESSERACT', 'level': level,
                        'category': category, 'title': f'{title} {seq}', 'detail': 'p4 e2e', 'raw': '', 'is_key_event': True})
    logs.extend(records)
    del logs[:-4000]
    emit('logs.batch', 'edge', {'from_seq': records[0]['seq'], 'records': records, 'dropped': 0, 'dropped_total': 0, 'gap': False})


# ---- requests -------------------------------------------------------------------------------
def job(frame, name, state_, result=None, error=None):
    data = {'op': frame['_op'], 'req_id': frame.get('req_id'), 'owner': frame.get('owner'), 'name': name, 'state': state_,
            'started_at_ms': frame['_started']}
    if result is not None:
        data['result'] = result
    if error is not None:
        data['error'] = error
    emit('jobs', 'edge', data)


def answer(frame, data=None, error=None):
    envelope = dict(t='res', id=frame['id'], cmd=frame.get('cmd'), ok=error is None, req_id=frame.get('req_id'), owner=frame.get('owner'))
    send(dict(envelope, data=data) if error is None else dict(envelope, error=error))


def log_page(req):
    since = req.get('since_seq') or 0
    n = max(1, min(int(req.get('n') or 200), 4000))
    rows = [r for r in logs if r['seq'] > since][-n:]
    return {'records': rows, 'watermark': logs[-1]['seq'] if logs else 0, 'next_cursor': None, 'gap': False, 'dropped': 0}


def query(frame):
    cmd, req = frame['cmd'], frame.get('args') or {}
    if cmd == 'config.snapshot':
        return {'rev': config['rev'], 'values': copy.deepcopy(config['values']), 'secret_fields_set': list(config['secret_fields_set']),
                'read_errors': [], 'pending': None}
    if cmd == 'autostart.state':
        on = bool(config['values'].get('autostart', {}).get('enabled'))
        return {'supported': options.get('autostart_supported', True) is not False, 'enabled': on,
                'target': 'shell' if on else 'none', 'mismatch': False, 'error': None}
    if cmd in ('logs.tail', 'logs.query'):
        return log_page(req)
    if cmd == 'app.info':
        return {'version': '0.1.0', 'language': config['values']['ui'].get('language', LANG), 'caps': COMMANDS, 'listeners': []}
    path = VALID / f'response_{cmd}.json'
    return json.loads(path.read_text(encoding='utf-8'))['data'] if path.exists() else None


def apply_settings(frame):
    sections, changed_language = [], None
    values = config['values']
    for change in (frame.get('args') or {}).get('changes') or []:
        section, new = change.get('section'), change.get('values') or {}
        if section == 'ui' and 'language' in new and new['language'] != values.get('ui', {}).get('language'):
            changed_language = new['language']
        if isinstance(values.get(section), dict) and isinstance(new, dict):
            values[section].update(copy.deepcopy(new))
        else:
            values[section] = copy.deepcopy(new)
        sections.append({'section': section, 'saved': True, 'applied': True, 'effect': 'none', 'error': None})
    config['rev'] += 1
    save_config()
    if changed_language and DECLARE_LANGUAGE:
        emit('ui.language', 'edge', {'language': changed_language, 'config_rev': config['rev']})
    result = {'rev': config['rev'], 'sections': sections, 'pending': None}
    emit('config.changed', 'edge', dict(result, files=['local'], external_detection='bridge_writes_only'))
    return result


def service(frame, cmd):
    if cmd == 'service.stop':
        phase['value'] = 'stopping'; send_state()
        time.sleep(.2)
        phase['value'] = 'stopped'; send_state()
        return job(frame, cmd, 'done', {'startup_phase': 'stopped', 'shutdown_exit_code': 0, 'cleanup_pending': False})
    outcome = options.get('start', 'done') if cmd == 'service.start' else 'done'
    if outcome == 'hold':
        return
    phase['value'] = 'starting'; send_state()
    time.sleep(.3)
    if outcome == 'failed':
        phase.update(value='failed', failure=FAILURE); send_state()
        return job(frame, cmd, 'failed', {'startup_phase': 'failed', 'failure_code': 'no_sources'},
                   {'code': 'needs_route', 'message_key': 'errors.start.no_sources', 'params': {}, 'reason_code': 'no_sources'})
    phase.update(value='running', failure=None); send_state()
    job(frame, cmd, 'done', {'startup_phase': 'running', 'failure_code': None})


def command(frame):
    cmd = frame.get('cmd')
    frame['_op'], frame['_started'] = f'op-{launch}-{frame["id"]}', now_ms()
    send(dict(t='ack', id=frame['id'], req_id=frame.get('req_id'), owner=frame.get('owner'), accepted=True, op=frame['_op']))
    if cmd in HOLD:
        HELD[frame['_op']] = (frame, cmd)
        return job(frame, cmd, 'running')
    if cmd in ('service.start', 'service.stop', 'service.restart'):
        return service(frame, cmd)
    if cmd == 'settings.apply':
        return job(frame, cmd, 'done', apply_settings(frame))
    path = VALID / f'result_{cmd}.json'
    if path.exists():
        result = json.loads(path.read_text(encoding='utf-8'))['data']
        return job(frame, cmd, result.get('state', 'done'), result.get('result'), result.get('error'))
    return job(frame, cmd, 'failed', None, NOT_FOUND)


def summary(frame):
    a = frame.get('args') or {}
    facts = {k: a[k] for k in ('delivery_id', 'outcome', 'surface', 'visible', 'focused', 'page', 'reason', 'topic', 'key') if k in a}
    if frame.get('cmd') == 'settings.apply':
        facts['sections'] = [c.get('section') for c in a.get('changes') or []]
        facts['values'] = {c.get('section'): c.get('values') for c in a.get('changes') or [] if c.get('section') in ('ui', 'autostart')}
    return {'t': frame.get('t'), 'cmd': frame.get('cmd'), 'from': frame.get('from'), 'topic': frame.get('topic'),
            'key': frame.get('key'), 'args': facts, 'at_ms': now_ms()}


def handle(frame):
    t = frame.get('t')
    if t == 'snapshot_req':
        topic, key = frame.get('topic'), frame.get('key')
        if FAIL_SNAP.get((topic, key), 0) > 0:
            # op stale: this replacement request is refused (PROTOCOL section 11 / 13: the shell
            # counts it as a failed attempt; the third in a row is rt:stale exhausted)
            FAIL_SNAP[(topic, key)] -= 1
            out = dict(t='snapshot_res', id=frame['id'], topic=topic,
                       error={'code': 'snapshot_required', 'message_key': 'errors.bridge.snapshot_required', 'params': {}})
            if key:
                out['key'] = key
            return send(out)
        snap = event(topic, 'snapshot', current(topic, key), key)
        out = dict(t='snapshot_res', id=frame['id'], topic=topic, snapshot=stamp(snap))
        if key:
            out['key'] = key
        return send(out)
    if t == 'cancel':
        # the separate control frame (the shell already checked the op's owner); answered by an ack
        # with the same id, the held job then ends cancelled
        op = frame.get('op')
        held = HELD.pop(op, None)
        if held is None:
            return send(dict(t='ack', id=frame['id'], accepted=False, skipped_reason='not_in_flight'))
        send(dict(t='ack', id=frame['id'], accepted=True, op=op))
        return job(held[0], held[1], 'cancelled')
    if t != 'req':
        return None
    cmd = frame.get('cmd')
    if cmd == 'app.quit':
        frame['_op'], frame['_started'] = 'quit-op', now_ms()
        send(dict(t='ack', id=frame['id'], req_id=frame.get('req_id'), owner=frame.get('owner'), accepted=True, op='quit-op'))
        job(frame, 'app.quit', 'done', {'startup_phase': 'stopped', 'shutdown_exit_code': None, 'cleanup_pending': False})
        send(dict(t='bye', exit_code=0, network_recovery=RECOVERY))
        return 'exit'
    if cmd == 'app.sessionEnd':
        answer(frame, {'writes_revoked': True, 'recovery': {'proxy': 'not_needed', 'firewall': 'not_needed'}, 'workers_pending': False})
        send(dict(t='bye', exit_code=0, network_recovery=RECOVERY))
        return 'exit'
    if cmd == 'alerts.desktopDelivered':
        return answer(frame, {'recorded': True, 'duplicate': False})
    if cmd in ('ui.presence', 'update.exitResponse', 'update.installResult'):
        return answer(frame, {'recorded': True})
    if cmd == 'settings.validate':
        return answer(frame, {'ok': True, 'error': None})
    if cmd in ('config.snapshot', 'autostart.state', 'app.info', 'geo.status', 'alerts.catalog', 'alerts.deliveries',
               'import.specs', 'logs.tail', 'logs.query', 'nodes.detail'):
        data = query(frame)
        return answer(frame, data) if data is not None else answer(frame, error=NOT_FOUND)
    if cmd in ('logs.setMihomoLevel', 'nodes.cancelDiagnose', 'alerts.onboarding.cancel', 'import.preview') or cmd not in COMMANDS:
        return answer(frame, error=NOT_FOUND)
    if cmd in ('nodes.reveal', 'alerts.onboarding.start', 'alerts.onboarding.poll', 'alerts.onboarding.complete'):
        return answer(frame, error=NOT_FOUND)
    return command(frame)


def run_control(spec):
    op = spec.get('op')
    if op == 'push':
        emit(spec['topic'], spec['kind'], spec['data'], spec.get('key'))
        if spec['kind'] == 'snapshot' and spec['topic'] != 'service.state':
            world[(spec['topic'], spec.get('key'))] = spec['data']
    elif op == 'service':
        phase.update(value=spec['phase'], failure=spec.get('failure'))
        send_state()
    elif op == 'desktop':
        data = {'delivery_id': spec['delivery_id'], 'event_ids': [spec['delivery_id']], 'severity': spec.get('severity', 'info'),
                'title': spec.get('title', ''), 'detail': spec.get('detail', ''), 'members': [], 'target': spec.get('target'), 'version': None}
        emit('alerts.desktop', 'edge', data)
    elif op == 'language':
        if not DECLARE_LANGUAGE:
            raise ValueError('ui.language is not declared in this launch')
        config['values']['ui']['language'] = spec['language']
        save_config()
        emit('ui.language', 'edge', {'language': spec['language'], 'config_rev': config['rev']})
    elif op == 'logs':
        push_logs(int(spec.get('n', 1)), level=spec.get('level', 'info'), category=spec.get('category', 'runtime'))
    elif op == 'stale':
        topic, key = spec['topic'], spec.get('key')
        if topic not in LIST_TOPICS or (topic in KEYED) != bool(key):
            raise ValueError(f'not a list slot: {topic} {key}')
        FAIL_SNAP[(topic, key)] = int(spec.get('fails', 3))
        params = {'topic': topic}
        if key:
            params['tier'] = key
        send(dict(t='error', error={'code': 'snapshot_required', 'message_key': 'errors.bridge.snapshot_required', 'params': params}))
    elif op == 'job':
        EXTERNAL['n'] += 1
        data = {'op': f'op-ext-{launch}-{EXTERNAL["n"]}', 'req_id': spec.get('req_id', f'shell-ext-{EXTERNAL["n"]}'), 'owner': spec.get('owner', 'shell'),
                'name': spec['name'], 'state': spec['state'], 'started_at_ms': now_ms()}
        for k in ('result', 'error'):
            if spec.get(k) is not None:
                data[k] = spec[k]
        emit('jobs', 'edge', data)
    elif op == 'log_rate':
        stream.update(per_s=int(spec.get('per_s', 0)), next=time.monotonic())
    elif op == 'options':
        options.update(spec.get('values') or {})
        HOLD.clear()
        HOLD.update(options.get('hold') or [])
    elif op == 'crash':
        crashed.write_text('1')
        raise SystemExit(int(spec.get('code', 3)))
    else:
        raise ValueError(f'unknown op {op!r}')


send(dict(t='hello', proto=1, app_version='0.1.0', runtime_pid=os.getpid(), lang=config['values']['ui'].get('language', LANG),
          scope={'kind': 'custom', 'id': args.scope or 'p4-ui-fixture'},
          caps={'commands': COMMANDS, 'topics': TOPICS, 'features': []}, network_recovery=RECOVERY))
assert pipe.receive(5)['t'] == 'hello_ack'
subscribe = pipe.receive(5)
assert subscribe['t'] == 'subscribe'
record({'t': 'subscribe', 'topics': subscribe.get('topics'), 'at_ms': now_ms()})
send(dict(t='subscribed', id=subscribe['id'], topics=subscribe['topics']))
seed()
(state / f'ready-{launch}').write_text('1')
deadline = time.monotonic() + 1800
beat = time.monotonic()
exit_code = 0
try:
    while time.monotonic() < deadline:
        for path in sorted(control.glob('*.json')):
            spec = json.loads(path.read_text(encoding='utf-8'))
            try:
                run_control(spec)
            except SystemExit as stop:
                path.replace(path.with_suffix('.done'))
                exit_code = stop.code
                raise
            except Exception as exc:  # reported to the driver, never fatal
                path.with_suffix('.error').write_text(repr(exc), encoding='utf-8')
                path.unlink()
                continue
            path.replace(path.with_suffix('.done'))
        now = time.monotonic()
        if now - beat >= 1:
            beat = now
            if phase['value'] in ('starting', 'running', 'stopping'):
                send_state()
            if phase['value'] == 'running':
                tick()
        catch_up = 0
        while stream['per_s'] and now >= stream['next'] and catch_up < 5:
            # 10 batches a second on a fixed schedule (a slow pass catches up, at most 5 at once)
            stream['next'] += .1
            catch_up += 1
            push_logs(max(1, stream['per_s'] // 10), 'fixture stream')
        frame = pipe.receive(.05)
        if not frame:
            continue
        if frame.get('t') == 'ping':
            send(dict(t='pong', n=frame['n']))
            continue
        record(summary(frame))
        if handle(frame) == 'exit':
            break
except SystemExit:
    pass
finally:
    pipe.close()
sys.exit(exit_code)
