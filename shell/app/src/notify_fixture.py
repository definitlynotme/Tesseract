"""T-P3-05 scripted stand-in sidecar ("侧车替身") for notices, presence and language.

Never constructs a runtime or touches the network. It declares the shell-only topic
`alerts.desktop`, `ui.language` and the shell commands `alerts.desktopDelivered` and
`ui.presence` in hello.caps, then:
  - answers pings; records every frame received after the handshake to <state>/frames-<n>.jsonl
    (n = this launch, 1 for the first; epoch notify-<n>);
  - answers alerts.desktopDelivered / ui.presence with a res, app.quit with ack + jobs edge +
    bye 0, app.sessionEnd with res + bye 0 (then exits 0);
  - sends a `ui.language` snapshot after `subscribed`, then writes <state>/ready;
  - every <state>/push/*.json file (sorted by name) is sent once, then renamed to *.sent:
      {"topic": T, "kind": K, "data": {...}}       one evt (rev/seq assigned here), or
      {"batch": [{"topic": T, "kind": K, "data": {...}}, ...]}  one batch frame.
<state> is --state, else $TESSERACT_DATA_DIR/notify-fixture (debug `--dev-sidecar-script`).
--mode crash_once: the first launch answers its first ui.presence, then exits 3 without bye
(a crash the shell restarts); later launches behave normally.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import bridge_transport
from bridge_transport import PipeTransport

# This fixture bypasses outgoing DTO validation in its own process only.
bridge_transport.protocol.encode_frame = lambda frame: json.dumps(frame).encode() + b'\n'
TOPICS = ['service.state', 'route.published', 'tiers.summary', 'traffic.tick', 'traffic.history', 'core.info', 'logs.batch', 'jobs']
SHELL_TOPICS = ['alerts.desktop', 'ui.language']
COMMANDS = ['service.start', 'service.stop', 'service.restart', 'app.quit', 'app.sessionEnd', 'alerts.desktopDelivered', 'ui.presence']
RECOVERY = {'proxy': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0},
            'firewall': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0}}

parser = argparse.ArgumentParser()
parser.add_argument('--ipc-handle', type=int)
parser.add_argument('--shell-pid', type=int)
parser.add_argument('--scope')
parser.add_argument('--state')
parser.add_argument('--lang', default='zh_CN')
parser.add_argument('--mode', default='normal', choices=['normal', 'crash_once'])
args, _unknown = parser.parse_known_args()  # the shell also passes --legacy-gate inherited
state = Path(args.state) if args.state else Path(os.environ['TESSERACT_DATA_DIR']) / 'notify-fixture'
push = state / 'push'
push.mkdir(parents=True, exist_ok=True)
launch = 1 + len(list(state.glob('launch-*')))
(state / f'launch-{launch}').write_text(str(os.getpid()))
pipe = PipeTransport(args.ipc_handle, args.shell_pid)
epoch = f'notify-{launch}'
crash = args.mode == 'crash_once' and launch == 1
exit_code = 0
revisions = {}
sequences = {}
ticks = [0]


def stamp(frame):
    now = int(time.time() * 1000)
    return dict(epoch=epoch, produced_at_ms=now, sent_at_ms=now, **frame)


def send(frame):
    frame = stamp(frame)
    while pipe.send(frame) == 'busy':
        time.sleep(.002)
    while pipe.poll_write() == 'pending':
        time.sleep(.002)


def record(frame):
    with (state / f'frames-{launch}.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(frame) + '\n')


def event(spec):
    topic, kind = spec['topic'], spec['kind']
    revisions[topic] = revisions.get(topic, 0) + 1
    frame = dict(t='evt', topic=topic, kind=kind, rev=revisions[topic], data=spec['data'])
    if kind != 'snapshot':
        sequences[topic] = sequences.get(topic, 0) + 1
        frame['seq'] = sequences[topic]
    return frame


def bye():
    send(dict(t='bye', exit_code=0, network_recovery=RECOVERY))


send(dict(t='hello', proto=1, runtime_pid=os.getpid(), lang=args.lang, scope={'kind': 'custom', 'id': args.scope},
          caps={'commands': COMMANDS, 'topics': TOPICS + SHELL_TOPICS, 'features': []}, network_recovery=RECOVERY))
assert pipe.receive(5)['t'] == 'hello_ack'
subscribe = pipe.receive(5)
assert subscribe['t'] == 'subscribe'
record(subscribe)
send(dict(t='subscribed', id=subscribe['id'], topics=subscribe['topics']))
send(dict(t='evt', topic='service.state', kind='snapshot', rev=1, data={'fixture': 1}))
revisions['service.state'] = 1
send(event({'topic': 'ui.language', 'kind': 'snapshot', 'data': {'language': args.lang, 'config_rev': 1}}))
(state / 'ready').write_text('1')
deadline = time.monotonic() + 600
try:
    while time.monotonic() < deadline:
        for path in sorted(push.glob('*.json')):
            spec = json.loads(path.read_text(encoding='utf-8'))
            if 'batch' in spec:
                ticks[0] += 1
                items = [stamp(event(item)) for item in spec['batch']]
                send(dict(t='batch', tick_seq=ticks[0], items=items))
            else:
                send(event(spec))
            path.replace(path.with_suffix('.sent'))
        frame = pipe.receive(.05)
        if not frame:
            continue
        if frame.get('t') == 'ping':
            send(dict(t='pong', n=frame['n']))
            continue
        record(frame)
        if frame.get('t') != 'req':
            continue
        command = frame.get('cmd')
        envelope = dict(t='res', id=frame['id'], ok=True, req_id=frame.get('req_id'), owner=frame.get('owner'))
        if command == 'alerts.desktopDelivered':
            send(dict(envelope, data={'recorded': True, 'duplicate': False}))
        elif command == 'ui.presence':
            send(dict(envelope, data={'recorded': True}))
            if crash:
                exit_code = 3
                break
        elif command == 'app.sessionEnd':
            send(dict(envelope, data={'writes_revoked': True, 'recovery': {'proxy': 'not_needed', 'firewall': 'not_needed'}, 'workers_pending': False}))
            bye()
            break
        elif command == 'app.quit':
            send(dict(t='ack', id=frame['id'], accepted=True, op='quit-op'))
            send(dict(t='evt', topic='jobs', kind='edge', rev=1, seq=1,
                      data={'op': 'quit-op', 'name': 'app.quit', 'state': 'done', 'started_at_ms': 1,
                            'result': {'startup_phase': 'stopped', 'shutdown_exit_code': None, 'cleanup_pending': False}}))
            bye()
            break
finally:
    pipe.close()
sys.exit(exit_code)
