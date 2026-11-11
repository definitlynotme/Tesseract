"""T-P3-06 scripted stand-in sidecar ("侧车替身") for save grants, the update hand-off and autostart.

Never constructs a runtime or touches the network. hello declares the shell-only topic
`update.prepareExit` and the shell commands shell.saveGrant, update.exitResponse,
update.installResult plus the query config.snapshot, then:
  - answers pings; records every frame received after the handshake to <state>/frames-<n>.jsonl
    (n = this launch, 1 for the first; epoch host-<n>);
  - shell.saveGrant: checks that `handle_id` is a disk-file handle of THIS process (GetFileType),
    resolves its path (GetFinalPathNameByHandleW), writes FIXTURE_BYTES through it, flushes and
    closes it, appends {"args", "path", "written"} to <state>/grants.jsonl and answers
    registered:true (<state>/refuse_grant present: closes the handle and answers not_allowed);
  - update.exitResponse / update.installResult: res {recorded:true};
  - config.snapshot: values.ui.start_in_tray_on_autostart = (<state>/tray exists);
  - app.quit: ack, a jobs edge, bye 0 (proxy/firewall not_needed), exit 0 (<state>/no_bye present:
    no bye for this one quit, the marker is consumed); app.sessionEnd: res, bye 0;
  - every <state>/push/*.json file (sorted by name) is sent once, then renamed to *.sent:
      {"topic": T, "kind": K, "data": {...}}  one evt (rev/seq assigned here per topic).
<state> is --state, else $TESSERACT_DATA_DIR/host-fixture (debug `--dev-sidecar-script`).
"""
import argparse
import ctypes
from ctypes import wintypes
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
SHELL_TOPICS = ['update.prepareExit']
COMMANDS = ['service.start', 'service.stop', 'service.restart', 'app.quit', 'app.sessionEnd', 'shell.saveGrant',
            'update.exitResponse', 'update.installResult', 'config.snapshot']
RECOVERY = {'proxy': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0},
            'firewall': {'state': 'not_needed', 'error_code': 'scope_isolated', 'remaining_count': 0}}
FIXTURE_BYTES = b'host-fixture-export\n'

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.GetFileType.argtypes = [wintypes.HANDLE]
kernel32.GetFileType.restype = wintypes.DWORD
kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

parser = argparse.ArgumentParser()
parser.add_argument('--ipc-handle', type=int)
parser.add_argument('--shell-pid', type=int)
parser.add_argument('--scope')
parser.add_argument('--state')
args, _unknown = parser.parse_known_args()  # the shell also passes --legacy-gate inherited
state = Path(args.state) if args.state else Path(os.environ['TESSERACT_DATA_DIR']) / 'host-fixture'
push = state / 'push'
push.mkdir(parents=True, exist_ok=True)
launch = 1 + len(list(state.glob('launch-*')))
(state / f'launch-{launch}').write_text(str(os.getpid()))
pipe = PipeTransport(args.ipc_handle, args.shell_pid)
epoch = f'host-{launch}'
revisions = {}
sequences = {}


def stamp(frame):
    now = int(time.time() * 1000)
    return dict(epoch=epoch, produced_at_ms=now, sent_at_ms=now, **frame)


def send(frame):
    frame = stamp(frame)
    while pipe.send(frame) == 'busy':
        time.sleep(.002)
    while pipe.poll_write() == 'pending':
        time.sleep(.002)


def record(frame, name=None):
    with (state / (name or f'frames-{launch}.jsonl')).open('a', encoding='utf-8') as out:
        out.write(json.dumps(frame) + '\n')


def event(topic, kind, data):
    revisions[topic] = revisions.get(topic, 0) + 1
    frame = dict(t='evt', topic=topic, kind=kind, rev=revisions[topic], data=data)
    if kind != 'snapshot':
        sequences[topic] = sequences.get(topic, 0) + 1
        frame['seq'] = sequences[topic]
    return frame


def grant(frame_args):
    handle = wintypes.HANDLE(frame_args['handle_id'])
    kind = kernel32.GetFileType(handle)
    buffer = ctypes.create_unicode_buffer(1024)
    length = kernel32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0)
    written = wintypes.DWORD(0)
    ok = kind == 1 and length > 0 and kernel32.WriteFile(handle, FIXTURE_BYTES, len(FIXTURE_BYTES), ctypes.byref(written), None)
    if ok:
        # The shell opens the target without truncating (OPEN_ALWAYS): the writer truncates at
        # its own write position, so an overwritten longer file keeps no stale tail.
        kernel32.SetEndOfFile(handle)
        kernel32.FlushFileBuffers(handle)
    kernel32.CloseHandle(handle)
    path = buffer.value[4:] if buffer.value.startswith('\\\\?\\') else buffer.value
    record({'args': frame_args, 'path': path, 'file_type': kind, 'written': written.value}, 'grants.jsonl')
    return bool(ok) and written.value == len(FIXTURE_BYTES)


def bye():
    send(dict(t='bye', exit_code=0, network_recovery=RECOVERY))


send(dict(t='hello', proto=1, runtime_pid=os.getpid(), lang='en_US', scope={'kind': 'custom', 'id': args.scope},
          caps={'commands': COMMANDS, 'topics': TOPICS + SHELL_TOPICS, 'features': []}, network_recovery=RECOVERY))
assert pipe.receive(5)['t'] == 'hello_ack'
subscribe = pipe.receive(5)
assert subscribe['t'] == 'subscribe'
record(subscribe)
send(dict(t='subscribed', id=subscribe['id'], topics=subscribe['topics']))
send(event('service.state', 'snapshot', {'fixture': launch}))
(state / f'ready-{launch}').write_text('1')
deadline = time.monotonic() + 600
try:
    while time.monotonic() < deadline:
        for path in sorted(push.glob('*.json')):
            spec = json.loads(path.read_text(encoding='utf-8'))
            send(event(spec['topic'], spec['kind'], spec['data']))
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
        envelope = dict(t='res', id=frame['id'], cmd=command, ok=True, req_id=frame.get('req_id'), owner=frame.get('owner'))
        if command == 'shell.saveGrant':
            if (state / 'refuse_grant').exists():
                kernel32.CloseHandle(wintypes.HANDLE(frame['args']['handle_id']))
                send(dict(envelope, ok=False, error={'code': 'not_allowed', 'message_key': 'errors.bridge.not_allowed', 'params': {}}))
            elif grant(frame['args']):
                send(dict(envelope, data={'registered': True}))
            else:
                send(dict(envelope, ok=False, error={'code': 'not_allowed', 'message_key': 'errors.bridge.not_allowed', 'params': {}}))
        elif command in ('update.exitResponse', 'update.installResult'):
            send(dict(envelope, data={'recorded': True}))
        elif command == 'config.snapshot':
            tray = (state / 'tray').exists()
            send(dict(envelope, data={'rev': 1, 'values': {'ui': {'language': 'en_US', 'start_in_tray_on_autostart': tray}},
                                      'secret_fields_set': [], 'read_errors': [], 'pending': None}))
        elif command == 'app.sessionEnd':
            send(dict(envelope, data={'writes_revoked': True, 'recovery': {'proxy': 'not_needed', 'firewall': 'not_needed'}, 'workers_pending': False}))
            bye()
            break
        elif command == 'app.quit':
            send(dict(t='ack', id=frame['id'], accepted=True, op='quit-op'))
            send(event('jobs', 'edge', {'op': 'quit-op', 'name': 'app.quit', 'state': 'done', 'started_at_ms': 1,
                                        'result': {'startup_phase': 'stopped', 'shutdown_exit_code': None, 'cleanup_pending': False}}))
            if (state / 'no_bye').exists():
                # One quit without bye (the update install then fails at once: bye_missing).
                (state / 'no_bye').unlink()
            else:
                bye()
            break
finally:
    pipe.close()
sys.exit(0)
