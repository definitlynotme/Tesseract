"""Scoped protocol fault fixture; never constructs a runtime or touches the network."""
import argparse
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bridge_transport import PipeTransport
import bridge_transport, json
# Fault injection deliberately bypasses outgoing DTO validation in this fixture process only.
bridge_transport.protocol.encode_frame=lambda frame: json.dumps(frame).encode()+b'\n'

parser = argparse.ArgumentParser()
parser.add_argument('--ipc-handle', type=int); parser.add_argument('--shell-pid', type=int)
parser.add_argument('--scope'); args = parser.parse_args()
pipe = PipeTransport(args.ipc_handle, args.shell_pid)
def send(frame):
    frame = dict(epoch='fault-fixture', produced_at_ms=1, sent_at_ms=1, **frame)
    if frame.pop('omit_time', False): frame.pop('sent_at_ms')
    while pipe.send(frame) == 'busy': time.sleep(.002)
    while pipe.poll_write() == 'pending': time.sleep(.002)
def event(rev, **extra):
    return dict(t='evt', topic='service.state', kind='snapshot', rev=rev, data={'fixture': rev}, **extra)
try:
    send(dict(t='hello', proto=1, runtime_pid=os.getpid(), scope={'kind': 'custom', 'id': args.scope},
              caps={'topics': ['service.state', 'route.published', 'tiers.summary', 'traffic.tick', 'traffic.history', 'core.info', 'logs.batch', 'jobs']}))
    assert pipe.receive(5)['t'] == 'hello_ack'
    assert pipe.receive(5)['t'] == 'subscribe'
    send(event(1)); send(event(2, omit_time=True)); send(event(2))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        frame = pipe.receive(.2)
        if frame and frame.get('cmd') == 'app.quit':
            send(dict(t='bye', exit_code=0)); break
        if frame and frame.get('t') == 'ping': send(dict(t='pong', n=frame['n']))
finally: pipe.close()
