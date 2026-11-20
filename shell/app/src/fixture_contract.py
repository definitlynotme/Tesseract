"""The real sidecar's ingress check, for the shell's stand-in sidecars ("替身").

bridge_main.dispatch validates every frame the shell sends. A request that fails is answered
with an error (bridge_main.reject_request) and never runs; any other frame that fails, or a
frame of another epoch, is dropped. The stand-ins used to answer whatever arrived, so a frame
the real sidecar refuses passed every shell test: 0.1.0-0.1.3 shipped an update acceptance
(update.exitResponse without reason_code) that the sidecar refused every time, and no in-app
update ever reached the installer.

`check` applies the same rules. Each violation is appended to <state>/contract-violations.jsonl
(the shell tests fail when that file exists) and the fixture answers like the real bridge. A
refused frame is never written to the fixture's frames / requests record: a harness that looks
for "the shell sent X" must not find a frame the real sidecar threw away.
"""
import json
import time
from pathlib import Path

import bridge_protocol
from bridge_main import request_error_code

FILE = 'contract-violations.jsonl'


def check(frame, state, epoch):
    """None: valid, handle it. Otherwise do not handle the frame; a non-empty dict is the error
    reply the real bridge sends (send it), an empty dict means the real bridge drops it."""
    errors = bridge_protocol.validate_frame(frame)
    if frame.get('epoch') != epoch:
        errors = errors + [f"epoch:{frame.get('epoch')!r}!={epoch!r}"]
    if not errors:
        return None
    entry = {'t': frame.get('t'), 'cmd': frame.get('cmd'), 'errors': errors}
    with (Path(state) / FILE).open('a', encoding='utf-8') as out:
        out.write(json.dumps(entry) + '\n')
    # bridge_main.dispatch: a frame of another epoch is dropped before anything else.
    if frame.get('epoch') == epoch and frame.get('t') == 'req' and bridge_protocol._request_id(frame.get('id')):
        code = request_error_code(errors)
        reply = {'t': 'error', 'id': frame['id'],
                 'error': {'code': code, 'message_key': 'errors.bridge.' + code, 'params': {}}}
        reply.update({key: frame[key] for key in ('req_id', 'owner') if key in frame})
        # bridge_main.reject_request sends its reply only if the reply itself validates (an empty
        # owner or req_id copied from the request does not): then the request goes unanswered.
        now = int(time.time() * 1000)
        if bridge_protocol.validate_frame(dict(reply, epoch=epoch, produced_at_ms=now, sent_at_ms=now)):
            return {}
        return reply
    return {}


def handshake(frame, kind, state, epoch):
    """The shell's hello_ack / subscribe: a refused one ends the real session, so it ends this one."""
    assert frame is not None and frame.get('t') == kind, frame
    assert check(frame, state, epoch) is None, f'{kind} refused by the real sidecar: see {FILE}'
    return frame
