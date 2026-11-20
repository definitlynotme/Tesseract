"""Scoped protocol fault fixture; never constructs a runtime or touches the network."""
import argparse
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(1, str(Path(__file__).resolve().parent / 'src'))
from bridge_transport import PipeTransport
import bridge_transport, json
import fixture_contract  # the shell's frames still meet the real sidecar's ingress check
# Fault injection deliberately bypasses outgoing DTO validation in this fixture process only.
bridge_transport.protocol.encode_frame=lambda frame: json.dumps(frame).encode()+b'\n'
EPOCH = 'fault-fixture'

parser = argparse.ArgumentParser()
parser.add_argument('--ipc-handle', type=int); parser.add_argument('--shell-pid', type=int)
parser.add_argument('--scope'); parser.add_argument('--state', required=True); args = parser.parse_args()
state = Path(args.state); state.mkdir(parents=True, exist_ok=True)
pipe = PipeTransport(args.ipc_handle, args.shell_pid)
def send(frame):
    frame = dict(epoch=EPOCH, produced_at_ms=1, sent_at_ms=1, **frame)
    if frame.pop('omit_time', False): frame.pop('sent_at_ms')
    while pipe.send(frame) == 'busy': time.sleep(.002)
    while pipe.poll_write() == 'pending': time.sleep(.002)
def event(rev, **extra):
    return dict(t='evt', topic='service.state', kind='snapshot', rev=rev, data={'fixture': rev}, **extra)
try:
    send(dict(t='hello', proto=1, runtime_pid=os.getpid(), scope={'kind': 'custom', 'id': args.scope},
              caps={'topics': ['service.state', 'route.published', 'tiers.summary', 'traffic.tick', 'traffic.history', 'core.info', 'logs.batch', 'jobs']}))
    fixture_contract.handshake(pipe.receive(5), 'hello_ack', state, EPOCH)
    fixture_contract.handshake(pipe.receive(5), 'subscribe', state, EPOCH)
    send(event(1)); send(event(2, omit_time=True)); send(event(2))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        frame = pipe.receive(.2)
        if not frame: continue
        verdict = fixture_contract.check(frame, state, EPOCH)
        if verdict is not None:  # refused as the real sidecar refuses it: never handled
            if verdict: send(verdict)
            continue
        if frame.get('cmd') == 'app.quit':
            send(dict(t='bye', exit_code=0)); break
        if frame.get('t') == 'ping': send(dict(t='pong', n=frame['n']))
finally: pipe.close()
