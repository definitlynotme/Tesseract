"""T-P3-04 scripted sidecar for the shell worker tests; never constructs a runtime or touches the network.

Modes (per launch; the launch number comes from files in --state):
  quit           answer pings; app.quit -> ack, a jobs edge, bye 0, exit 0
  session_end    like quit, and app.sessionEnd -> res, bye 0, exit 0
  crash_once     launch 1 exits 3 right after the first snapshot, later launches behave like quit
  crash_always   every launch exits 3 right after the first snapshot
  exit74         exits 74 after the first snapshot (no quit requested)
  prehello       exits 5 before hello
  silent_once    launch 1 never answers pings (hang); later launches behave like quit
  quit_stuck     app.quit -> ack only (stuck, no bye); app.sessionEnd -> res, bye 0, exit 0 (T-P3-05)
Every frame received after the handshake is appended to <state>/frames-<launch>.jsonl.
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
RECOVERY = {'proxy': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0},
            'firewall': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0}}

parser = argparse.ArgumentParser()
parser.add_argument('--ipc-handle', type=int)
parser.add_argument('--shell-pid', type=int)
parser.add_argument('--scope')
parser.add_argument('--mode')
parser.add_argument('--state')
args = parser.parse_args()
state = Path(args.state)
state.mkdir(parents=True, exist_ok=True)
launch = len(list(state.glob('launch-*'))) + 1
(state / f'launch-{launch}').write_text(str(os.getpid()))
mode = args.mode
if mode == 'prehello':
    sys.exit(5)
pipe = PipeTransport(args.ipc_handle, args.shell_pid)
epoch = f'lifecycle-{launch}'


def send(frame):
    now = int(time.time() * 1000)
    frame = dict(epoch=epoch, produced_at_ms=now, sent_at_ms=now, **frame)
    while pipe.send(frame) == 'busy':
        time.sleep(.002)
    while pipe.poll_write() == 'pending':
        time.sleep(.002)


def record(frame):
    with (state / f'frames-{launch}.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(frame) + '\n')


def bye():
    send(dict(t='bye', exit_code=0, network_recovery=RECOVERY))


send(dict(t='hello', proto=1, runtime_pid=os.getpid(), lang='en_US', scope={'kind': 'custom', 'id': args.scope},
          caps={'commands': ['service.start', 'service.stop', 'service.restart', 'app.quit'], 'topics': TOPICS, 'features': []},
          network_recovery=RECOVERY))
assert pipe.receive(5)['t'] == 'hello_ack'
subscribe = pipe.receive(5)
assert subscribe['t'] == 'subscribe'
send(dict(t='subscribed', id=subscribe['id'], topics=subscribe['topics']))
send(dict(t='evt', topic='service.state', kind='snapshot', rev=1, data={'fixture': launch}))
if mode == 'crash_always' or (mode == 'crash_once' and launch == 1):
    time.sleep(.3)
    os._exit(3)
if mode == 'exit74':
    time.sleep(.3)
    os._exit(74)
silent = mode == 'silent_once' and launch == 1
deadline = time.monotonic() + 60
code = 0
try:
    while time.monotonic() < deadline:
        frame = pipe.receive(.1)
        if not frame:
            continue
        if frame.get('t') == 'ping':
            if not silent:
                send(dict(t='pong', n=frame['n']))
            continue
        record(frame)
        if frame.get('t') == 'req' and frame.get('cmd') == 'app.sessionEnd':
            send(dict(t='res', id=frame['id'], cmd='app.sessionEnd', ok=True, req_id=frame.get('req_id'), owner=frame.get('owner'),
                      data={'writes_revoked': True, 'recovery': {'proxy': 'not_needed', 'firewall': 'not_needed'}, 'workers_pending': False}))
            bye()
            break
        if frame.get('t') == 'req' and frame.get('cmd') == 'app.quit':
            send(dict(t='ack', id=frame['id'], accepted=True, op='quit-op'))
            if mode == 'quit_stuck':
                continue
            send(dict(t='evt', topic='jobs', kind='edge', rev=1, seq=1,
                      data={'op': 'quit-op', 'name': 'app.quit', 'state': 'done', 'started_at_ms': 1,
                            'result': {'startup_phase': 'stopped', 'shutdown_exit_code': None, 'cleanup_pending': False}}))
            bye()
            break
finally:
    pipe.close()
sys.exit(code)
