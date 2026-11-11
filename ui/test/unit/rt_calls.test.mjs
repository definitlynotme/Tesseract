// T-P4-02a: calls (job / res / private / host), unknown ops, owners, cancel, error texts.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RtError, errorText, reasonText } from '../../src/rt/errors.js';
import { REQ_ID_RE, createIds, isReqId } from '../../src/rt/ids.js';
import { E1, E2, evt, flush, job, live, hello } from './rt_helpers.mjs';

const P2_COMMANDS = ['service.start', 'service.stop', 'service.restart', 'mode.set', 'exit.verify', 'nodes.reveal', 'nodes.diagnose',
  'nodes.cancelDiagnose', 'alerts.onboarding.start', 'alerts.onboarding.cancel', 'settings.validate'];
const ack = (extra) => ({ t: 'ack', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 2, ...extra });
const res = (extra) => ({ t: 'res', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 3, ...extra });
const onInvoke = (cmd, reply = [], extra = {}) => ({ on_invoke: { cmd }, reply, ...extra });
const withSteps = (...steps) => ({ scenario: { steps }, hello: { commands: P2_COMMANDS } });
const jobs = (seq, data) => evt('jobs', seq, 'edge', data, { seq });

test('req_id: p<conn>.<n>, always inside bridge_diagnostics._REQUEST_ID', () => {
  const ids = createIds({ random: () => 0.123456 });
  const a = ids.next(), b = ids.next();
  assert.match(a, /^p[a-z0-9]+\.1$/);
  assert.match(b, /^p[a-z0-9]+\.2$/);
  for (const id of [a, b, ids.owner('drawer: node A/B'), ids.pageOwner]) assert.ok(REQ_ID_RE.test(id), id);
  assert.equal(isReqId('-x'), false);
  assert.equal(isReqId('a'.repeat(129)), false);
  assert.ok(ids.owner('x'.repeat(300)).length <= 128);
  assert.throws(() => createIds({ conn: 'BAD!' }));
});

test('the pending entry exists before invoke: an ack that overtakes the invoke result still lands', async () => {
  const { rt, fake } = await live(withSteps(onInvoke('service.start', [ack({ req_id: '$req_id', accepted: true, op: 'op-1' })])));
  const p = rt.call('service.start');
  assert.equal(fake.invocations[0], undefined, 'invoke is issued after registration, on the next turn');
  await flush();
  assert.equal(fake.invocations[0].cmd, 'service.start');
  assert.match(fake.invocations[0].req_id, /^pabc\.\d+$/);
  assert.equal(fake.invocations[0].owner, 'page.abc');
  fake.deliver(jobs(1, job('op-1', 'service.start', 'done', { result: { startup_phase: 'running', failure_code: null } })));
  assert.equal((await p).startup_phase, 'running');
});

test('job ack refused: skipped_reason and job_capacity', async () => {
  const { rt } = await live(withSteps(
    onInvoke('service.stop', [ack({ accepted: false, skipped_reason: 'already_stopped' })]),
    onInvoke('service.start', [ack({ accepted: false, error: { code: 'job_capacity', message_key: 'errors.bridge.job_capacity', params: {} } })]),
  ));
  await assert.rejects(rt.call('service.stop'), e => e instanceof RtError && e.code === 'already_stopped' && e.reason_code === 'already_stopped' && e.skipped_reason === 'already_stopped');
  await assert.rejects(rt.call('service.start'), e => e.code === 'job_capacity' && e.message_key === 'errors.bridge.job_capacity');
});

test('job lifecycle: running(progress) -> done; failed keeps the partial result; cancelled; superseded', async () => {
  const { rt, fake } = await live(withSteps(
    onInvoke('service.start', [ack({ accepted: true, op: 'a' })]),
    onInvoke('service.stop', [ack({ accepted: true, op: 'b' })]),
    onInvoke('service.restart', [ack({ accepted: true, op: 'c' })]),
    onInvoke('mode.set', [ack({ accepted: true, op: 'd' })]),
  ));
  const progress = [];
  const pa = rt.call('service.start', {}, { onProgress: p => progress.push(p) });
  const pb = rt.call('service.stop');
  const pc = rt.call('service.restart');
  const pd = rt.call('mode.set', { mode: 'tun' });
  await flush();
  fake.deliver(jobs(1, job('a', 'service.start', 'queued')));
  fake.deliver(jobs(2, job('a', 'service.start', 'running', { progress: { step: 1 } })));
  fake.deliver(jobs(3, job('a', 'service.start', 'running', { progress: { step: 2 } })));
  fake.deliver(jobs(4, job('a', 'service.start', 'done', { result: { startup_phase: 'running', failure_code: null } })));
  fake.deliver(jobs(5, job('b', 'service.stop', 'failed', { result: { startup_phase: 'stopping', shutdown_exit_code: null, cleanup_pending: true },
    error: { code: 'io', message_key: 'errors.io', params: {} } })));
  fake.deliver(jobs(6, job('c', 'service.restart', 'superseded')));
  fake.deliver(jobs(7, job('d', 'mode.set', 'cancelled')));
  assert.deepEqual(await pa, { startup_phase: 'running', failure_code: null });
  assert.deepEqual(progress, [{ step: 1 }, { step: 2 }]);
  await assert.rejects(pb, e => e.code === 'io' && e.result.cleanup_pending === true);
  await assert.rejects(pc, e => e.code === 'superseded');
  await assert.rejects(pd, e => e.code === 'cancelled');
});

test('a job DTO carrying our req_id links directly, before any ack', async () => {
  const { rt, fake } = await live(withSteps(onInvoke('service.start')));
  const p = rt.call('service.start');
  await flush();
  const reqId = fake.invocations[0].req_id;
  fake.deliver(jobs(1, job('z', 'service.start', 'done', { req_id: reqId, result: { startup_phase: 'collecting', failure_code: null } })));
  assert.equal((await p).startup_phase, 'collecting');
});

test('unknown op: events first, the ack within 5 s replays them; after 5 s they are dropped', async () => {
  const { rt, fake, clock } = await live(withSteps(onInvoke('service.start'), onInvoke('service.restart')));
  const p = rt.call('service.start');
  await flush();
  const reqId = fake.invocations[0].req_id;
  fake.deliver(jobs(1, job('op-x', 'service.start', 'done', { result: { startup_phase: 'running', failure_code: null } })));
  clock.advance(4000);
  fake.deliver(ack({ req_id: reqId, accepted: true, op: 'op-x' }));
  assert.equal((await p).startup_phase, 'running');

  const q = rt.call('service.restart');
  await flush();
  const reqId2 = fake.invocations[1].req_id;
  fake.deliver(jobs(2, job('op-y', 'service.restart', 'done', { result: { applied: true, core_running: true, core_cleanup_pending: false, core_generation: 2 } })));
  clock.advance(5001);
  assert.ok(rt.diag().some(d => d.kind === 'stranger_expired' && d.op === 'op-y'));
  fake.deliver(ack({ req_id: reqId2, accepted: true, op: 'op-y' }));
  let settled = false;
  q.then(() => { settled = true; }, () => { settled = true; });
  await flush();
  assert.equal(settled, false, 'the expired events are gone; the call waits for its own terminal');
  fake.deliver(jobs(3, job('op-y', 'service.restart', 'failed', { error: { code: 'timeout', message_key: 'errors.timeout', params: {} } })));
  await assert.rejects(q, e => e.code === 'timeout');
});

test('res and host res: data, errors, reason_code dialog_open', async () => {
  const { rt } = await live(withSteps(
    onInvoke('settings.validate', [res({ cmd: 'settings.validate', ok: true, data: { valid: true, errors: [] } })]),
    onInvoke('host.openUrl', [res({ cmd: 'host.openUrl', ok: false, error: { code: 'conflict', message_key: 'errors.host.conflict', params: {}, reason_code: 'dialog_open' } })]),
    onInvoke('host.openFolder', [res({ cmd: 'host.openFolder', ok: true, data: { opened: true } })]),
    onInvoke('app.info', [res({ cmd: 'app.info', ok: true, data: { version: '0.1.0', language: 'zh_CN', caps: [], listeners: [] } })]),
  ));
  assert.deepEqual(await rt.call('settings.validate', { section: 'dns', field: 'x', value: 1 }), { valid: true, errors: [] });
  await assert.rejects(rt.call('host.openUrl', { url: 'https://github.com' }), e => e.code === 'conflict' && e.reason_code === 'dialog_open' && e.message_key === 'errors.host.conflict');
  assert.deepEqual(await rt.call('host.openFolder', { kind: 'logs' }), { opened: true });
  assert.equal((await rt.query('app.info')).version, '0.1.0');
  await assert.rejects(rt.query('service.start'), e => e.code === 'not_allowed', 'query() only takes queries');
});

test('invoke rejected synchronously by the shell: every code becomes an RtError and the waiter ends', async () => {
  const codes = ['runtime_unavailable', 'job_capacity', 'not_allowed', 'invalid_type', 'internal'];
  const { rt } = await live(withSteps(...codes.map(c => ({ on_invoke: { cmd: 'service.start' }, reject: c }))));
  for (const code of codes) await assert.rejects(rt.call('service.start'), e => e instanceof RtError && e.code === code);
});

test('shell-only commands and unknown names are refused locally', async () => {
  const { rt, fake } = await live();
  for (const cmd of ['app.quit', 'ui.presence', 'alerts.desktopDelivered', 'shell.saveGrant', 'no.such.cmd']) {
    await assert.rejects(rt.call(cmd), e => e.code === 'not_allowed', cmd);
  }
  assert.equal(fake.invocations.length, 0);
});

test('an error frame for our req_id (the shell refusing a queued request) rejects the call', async () => {
  const { rt, fake } = await live(withSteps(onInvoke('service.start', [{ t: 'error', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 5,
    error: { code: 'job_capacity', message_key: 'errors.bridge.job_capacity', params: {} } }])));
  await assert.rejects(rt.call('service.start'), e => e.code === 'job_capacity');
  assert.equal(fake.failures.length, 0);
});

test('owner: revoke rejects in-flight calls with owner_revoked, drops late results, sends onRevoke once', async () => {
  const { rt, fake } = await live(withSteps(
    onInvoke('nodes.diagnose', [ack({ accepted: true, op: 'diag-1' })]),
    onInvoke('nodes.cancelDiagnose', [res({ cmd: 'nodes.cancelDiagnose', ok: true, data: { requested: true, cleanup_pending: false } })]),
  ));
  const owner = rt.owner('panel', { onRevoke: () => ({ cmd: 'nodes.cancelDiagnose', args: { request_id: 'r1' } }) });
  assert.match(owner.id, /^oabc\.\d+\.panel$/);
  const p = rt.call('nodes.diagnose', { node_id: 'n1', probe_mode: 'direct', request_id: 'r1' }, { owner });
  await flush();
  assert.equal(fake.invocations[0].owner, owner.id);
  assert.equal(owner.revoke(), true);
  await assert.rejects(p, e => e.code === 'owner_revoked');
  assert.equal(owner.revoke(), false, 'revoke is idempotent');
  await flush();
  const sent = fake.invocations.filter(i => i.cmd === 'nodes.cancelDiagnose');
  assert.equal(sent.length, 1, 'the registered cancel goes out exactly once');
  assert.equal(sent[0].owner, owner.id);
  assert.ok(rt.diag().some(d => d.kind === 'on_revoke_sent' && d.cmd === 'nodes.cancelDiagnose' && d.code === 'ok'));
  // the late terminal of the revoked call is dropped silently
  fake.deliver(jobs(1, job('diag-1', 'nodes.diagnose', 'done', { result: { request_id: 'r1', status_applied: true, reason_code: null, cleanup_pending: false } })));
  await assert.rejects(rt.call('nodes.diagnose', { node_id: 'n1', probe_mode: 'direct', request_id: 'r2' }, { owner }), e => e.code === 'owner_revoked');
  assert.equal(fake.failures.length, 0);
});

test('owner onRevoke may cancel a job by its promise (cancel carries the original owner)', async () => {
  const { rt, fake } = await live(withSteps(
    onInvoke('exit.verify', [ack({ accepted: true, op: 'ex-1' })]),
    onInvoke('cancel', [ack({ accepted: true })]),
  ));
  let p;
  const owner = rt.owner('exit', { onRevoke: () => ({ cancel: p }) });
  p = rt.call('exit.verify', { trigger: 'manual' }, { owner });
  p.catch(() => {});
  await flush();
  owner.revoke();
  await flush();
  const c = fake.invocations.find(i => i.cmd === 'cancel');
  assert.ok(c, 'cancel sent');
  assert.deepEqual(c.args, { op: 'ex-1' });
  assert.equal(c.owner, owner.id);
});

test('cancel(promise) before the ack waits for the op, then sends cancel{op} with the original owner', async () => {
  const { rt, fake } = await live(withSteps(
    onInvoke('mode.set'),
    onInvoke('cancel', [ack({ accepted: true })]),
  ));
  const owner = rt.owner('mode');
  const p = rt.call('mode.set', { mode: 'tun' }, { owner });
  p.catch(() => {});
  const c = rt.cancel(p);
  await flush();
  assert.equal(fake.invocations.filter(i => i.cmd === 'cancel').length, 0);
  fake.deliver(ack({ req_id: fake.invocations[0].req_id, accepted: true, op: 'm-1' }));
  await flush();
  const sent = fake.invocations.find(i => i.cmd === 'cancel');
  assert.deepEqual(sent.args, { op: 'm-1' });
  assert.equal(sent.owner, owner.id);
  assert.deepEqual(await c, { accepted: true });
});

const SECRET = 'S3cr3t-pa55word-xyzzy';
test('private result: only the Promise gets it; nothing in the store, events or diag()', async () => {
  const reveal = res({ cmd: 'nodes.reveal', ok: true, data: { item_id: 'n1', fields: { password: SECRET } } });
  const { T, rt } = await live(withSteps(onInvoke('nodes.reveal', [{ ...reveal, req_id: '$req_id', owner: '$owner' }])));
  const owner = rt.owner('reveal');
  const out = await rt.call('nodes.reveal', { item_id: 'n1', fields: ['password'] }, { owner });
  assert.equal(out.fields.password, SECRET);
  assert.ok(!JSON.stringify(T.store.snapshot()).includes(SECRET), 'store');
  assert.ok(!JSON.stringify(T.events).includes(SECRET), 'events');
  const ring = rt.diag();
  assert.ok(!JSON.stringify(ring).includes(SECRET), 'diag');
  const entry = ring.find(d => d.cmd === 'nodes.reveal');
  assert.deepEqual(Object.keys(entry).sort(), ['at', 'cmd', 'code', 'kind'], 'only cmd and result code for a private command');
});

test('private result: dropped on owner / cmd / epoch mismatch and after revoke; a mismatch still ends the waiter', async () => {
  const mk = (extra) => ({ ...res({ cmd: 'nodes.reveal', ok: true, data: { item_id: 'n1', fields: { password: SECRET } } }), req_id: '$req_id', ...extra });
  const { T, rt, fake } = await live(withSteps(
    onInvoke('nodes.reveal', [mk({ owner: 'someone-else' })]),
    onInvoke('nodes.reveal', [mk({ owner: '$owner', cmd: 'alerts.onboarding.poll' })]),
    onInvoke('nodes.reveal', [mk({ owner: '$owner', epoch: E2 })]),
    onInvoke('nodes.reveal'),
  ));
  const owner = rt.owner('reveal');
  // owner and cmd mismatches: the data is dropped and the waiter ends with internal
  for (let i = 0; i < 2; i++) {
    await assert.rejects(rt.call('nodes.reveal', { item_id: 'n1' }, { owner }), e => e.code === 'internal' && e.result === undefined, `mismatch ${i}`);
  }
  // an answer stamped with another epoch never reaches the call (dropped at the epoch filter):
  // the waiter stays for the answer of its own epoch
  let done = false;
  rt.call('nodes.reveal', { item_id: 'n1' }, { owner }).then(() => { done = true; }, () => { done = true; });
  await flush();
  assert.equal(done, false, 'another epoch is not ours');
  assert.ok(rt.diag().some(d => d.kind === 'epoch_mismatch' && d.t === 'res'));
  const late = rt.call('nodes.reveal', { item_id: 'n1' }, { owner });
  await flush();
  owner.revoke();
  await assert.rejects(late, e => e.code === 'owner_revoked');
  fake.deliver(mk({ req_id: fake.invocations[3].req_id, owner: owner.id }));
  assert.ok(!JSON.stringify(T.store.snapshot()).includes(SECRET));
  assert.ok(!JSON.stringify(T.events).includes(SECRET));
  assert.ok(!JSON.stringify(rt.diag()).includes(SECRET));
  assert.ok(rt.diag().filter(d => d.kind === 'private_dropped').every(d => Object.keys(d).sort().join() === 'at,cmd,code,kind'));
});

test('private call before the first hello (no epoch to match against) is refused locally', async () => {
  const { boot } = await import('./rt_helpers.mjs');
  const b = boot({ scenario: { steps: [] } });
  await flush();
  await assert.rejects(b.rt.call('nodes.reveal', { item_id: 'n1' }), e => e.code === 'runtime_unavailable');
  assert.equal(b.fake.invocations.length, 0);
});

test('cancel(promise) before the ack ends when the call ends without an op: ack refused, runtime_gone, owner revoked', async () => {
  // ack refused: the cancel ends with the same code
  const a = await live(withSteps(onInvoke('service.start', [ack({ accepted: false, skipped_reason: 'lifecycle_busy' })])));
  const pa = a.rt.call('service.start');
  const ca = a.rt.cancel(pa);
  await assert.rejects(pa, e => e.code === 'lifecycle_busy');
  await assert.rejects(ca, e => e.code === 'lifecycle_busy' && e.reason_code === 'lifecycle_busy');
  assert.equal(a.fake.invocations.filter(i => i.cmd === 'cancel').length, 0);
  // runtime_gone before the ack
  const b = await live(withSteps(onInvoke('service.start')));
  const pb = b.rt.call('service.start');
  pb.catch(() => {});
  const cb = b.rt.cancel(pb);
  await flush();
  b.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'runtime_gone' });
  await assert.rejects(pb, e => e.code === 'runtime_gone');
  await assert.rejects(cb, e => e.code === 'runtime_gone');
  // the owner is revoked before the ack
  const c = await live(withSteps(onInvoke('mode.set')));
  const owner = c.rt.owner('mode');
  const pc = c.rt.call('mode.set', { mode: 'tun' }, { owner });
  pc.catch(() => {});
  const cc = c.rt.cancel(pc);
  await flush();
  owner.revoke();
  await assert.rejects(cc, e => e.code === 'owner_revoked');
  // an error frame from the shell and a synchronous invoke rejection end it too
  const d = await live(withSteps(onInvoke('service.start'), onInvoke('service.restart', [], { reject: 'job_capacity' })));
  const pd = d.rt.call('service.start');
  pd.catch(() => {});
  const cd = d.rt.cancel(pd);
  await flush();
  d.fake.deliver({ t: 'error', epoch: E1, id: 9, req_id: d.fake.invocations[0].req_id, error: { code: 'job_capacity', message_key: 'errors.bridge.job_capacity', params: {} } });
  await assert.rejects(cd, e => e.code === 'job_capacity');
  const pe = d.rt.call('service.restart');
  pe.catch(() => {});
  await assert.rejects(d.rt.cancel(pe), e => e.code === 'job_capacity');
  // a terminal job DTO (with our req_id) that overtakes the ack: nothing left to cancel
  const f = await live(withSteps(onInvoke('service.start')));
  const pf = f.rt.call('service.start');
  const cf = f.rt.cancel(pf);
  await flush();
  f.fake.deliver(jobs(1, job('op-f', 'service.start', 'done', { req_id: f.fake.invocations[0].req_id, result: { startup_phase: 'running', failure_code: null } })));
  assert.equal((await pf).startup_phase, 'running');
  await assert.rejects(cf, e => e.code === 'not_found');
});

test('cancel(promise) twice before the ack (double click): both end, one cancel{op} is sent, ack accepted or refused', async () => {
  const settledState = (pr) => { const s = { done: false }; pr.then(v => { s.done = true; s.ok = true; s.v = v; }, e => { s.done = true; s.ok = false; s.e = e; }); return s; };
  // ack accepted: one cancel{op}, both cancel Promises resolve with its outcome
  const a = await live(withSteps(
    onInvoke('service.start'),
    onInvoke('cancel', [ack({ accepted: true })]),
    onInvoke('cancel', [ack({ accepted: true })]),
  ));
  const pa = a.rt.call('service.start');
  pa.catch(() => {});
  const c1 = a.rt.cancel(pa), c2 = a.rt.cancel(pa);
  const s1 = settledState(c1), s2 = settledState(c2);
  await flush();
  assert.equal(a.fake.invocations.filter(i => i.cmd === 'cancel').length, 0, 'no cancel before the op is known');
  a.fake.deliver(ack({ req_id: a.fake.invocations[0].req_id, accepted: true, op: 'op-dbl' }));
  await flush();
  const sent = a.fake.invocations.filter(i => i.cmd === 'cancel');
  assert.equal(sent.length, 1, 'cancel sent exactly once');
  assert.deepEqual(sent[0].args, { op: 'op-dbl' });
  assert.ok(s1.done && s1.ok, 'first cancel resolved');
  assert.ok(s2.done && s2.ok, 'second cancel resolved');
  assert.deepEqual(s1.v, { accepted: true });
  assert.deepEqual(s2.v, { accepted: true });
  // the shared Promise only merges clicks while the op is unknown: a third cancel after the
  // op is linked sends a fresh cancel{op} with its own Promise
  const c3 = a.rt.cancel(pa);
  assert.notEqual(c3, c1);
  assert.deepEqual(await c3, { accepted: true });
  assert.equal(a.fake.invocations.filter(i => i.cmd === 'cancel').length, 2);
  assert.deepEqual(a.fake.failures, []);
  // ack refused: both cancel Promises reject with the call's code, nothing is sent
  const b = await live(withSteps(onInvoke('service.start')));
  const pb = b.rt.call('service.start');
  pb.catch(() => {});
  const d1 = b.rt.cancel(pb), d2 = b.rt.cancel(pb);
  const t1 = settledState(d1), t2 = settledState(d2);
  await flush();
  b.fake.deliver(ack({ req_id: b.fake.invocations[0].req_id, accepted: false, skipped_reason: 'lifecycle_busy' }));
  await flush();
  await assert.rejects(pb, e => e.code === 'lifecycle_busy');
  assert.ok(t1.done && !t1.ok && t1.e.code === 'lifecycle_busy', 'first cancel rejected');
  assert.ok(t2.done && !t2.ok && t2.e.code === 'lifecycle_busy', 'second cancel rejected');
  assert.equal(b.fake.invocations.filter(i => i.cmd === 'cancel').length, 0);
});

test('a pre-ack cancel that fails after the op is linked can be retried: cancel(promise) and cancel(op) send a fresh cancel{op}', async () => {
  const settledState = (pr) => { const s = { done: false }; pr.then(v => { s.done = true; s.ok = true; s.v = v; }, e => { s.done = true; s.ok = false; s.e = e; }); return s; };
  const cancelsOf = (fake) => fake.invocations.filter(i => i.cmd === 'cancel');
  // A: the cancel{op} sent on the ack is rejected synchronously by invoke (job_capacity)
  const a = await live(withSteps(
    onInvoke('service.start'),
    onInvoke('cancel', [], { reject: 'job_capacity' }),
    onInvoke('cancel', [ack({ accepted: true })]),
    onInvoke('cancel', [ack({ accepted: true })]),
  ));
  const pa = a.rt.call('service.start');
  pa.catch(() => {});
  const c1 = a.rt.cancel(pa);
  await flush();
  a.fake.deliver(ack({ req_id: a.fake.invocations[0].req_id, accepted: true, op: 'op-a' }));
  await assert.rejects(c1, e => e.code === 'job_capacity');
  assert.equal(cancelsOf(a.fake).length, 1);
  const c2 = a.rt.cancel(pa);
  assert.notEqual(c2, c1, 'the failed cancel is not handed back');
  const s2 = settledState(c2);
  await flush();
  assert.equal(cancelsOf(a.fake).length, 2, 'cancel(promise) sends cancel{op} again');
  assert.deepEqual(cancelsOf(a.fake)[1].args, { op: 'op-a' });
  assert.ok(s2.done && s2.ok, 'the new cancel ends with its own ack');
  assert.deepEqual(s2.v, { accepted: true });
  const c3 = a.rt.cancel('op-a');
  assert.notEqual(c3, c1);
  assert.deepEqual(await c3, { accepted: true });
  assert.equal(cancelsOf(a.fake).length, 3, 'cancel(op) sends cancel{op} again');
  assert.deepEqual(cancelsOf(a.fake)[2].args, { op: 'op-a' });
  assert.deepEqual(a.fake.failures, []);
  // B: the cancel{op} is in flight when an order jump forces a reconnect (endUnanswered)
  const sb = withSteps(
    onInvoke('service.start'),
    onInvoke('cancel'),
    onInvoke('cancel', [ack({ accepted: true })]),
    onInvoke('cancel', [ack({ accepted: true })]),
  );
  sb.scenario.replay = [{ t: 'rt:reset', epoch: E1, reason: 'reconnect' }, hello({ commands: P2_COMMANDS })];
  const b = await live(sb);
  const pb = b.rt.call('service.start');
  let jobDone = false;
  pb.then(() => { jobDone = true; }, () => { jobDone = true; });
  const d1 = b.rt.cancel(pb);
  await flush();
  b.fake.deliver(ack({ req_id: b.fake.invocations[0].req_id, accepted: true, op: 'op-b' }));
  await flush();
  assert.equal(cancelsOf(b.fake).length, 1, 'cancel{op} sent on the ack');
  b.fake.deliver({ t: 'ping', epoch: E1 }, b.fake.order + 5); // an order jump: a delivery was lost
  await assert.rejects(d1, e => e.code === 'runtime_unavailable' && e.reason_code === 'reconnected');
  await flush(6);
  assert.equal(b.fake.connects, 2);
  assert.equal(jobDone, false, 'the job keeps running');
  assert.equal(b.rt.state, 'live');
  const d2 = b.rt.cancel(pb);
  assert.notEqual(d2, d1);
  assert.deepEqual(await d2, { accepted: true });
  assert.equal(cancelsOf(b.fake).length, 2, 'cancel(promise) sends cancel{op} again after the reconnect');
  const d3 = b.rt.cancel('op-b');
  assert.notEqual(d3, d1);
  assert.deepEqual(await d3, { accepted: true });
  assert.equal(cancelsOf(b.fake).length, 3, 'cancel(op) sends cancel{op} again after the reconnect');
  for (const c of cancelsOf(b.fake)) assert.deepEqual(c.args, { op: 'op-b' });
  assert.deepEqual(b.fake.failures, []);
});

test('a transport fault ends res / host / private / cancel waiters with a retryable code; job waiters stay', async () => {
  const { rt, fake } = await live({ scenario: { steps: [{ on_invoke: { cmd: 'nodes.reveal' } }, { on_invoke: { cmd: 'service.start' } }, { on_invoke: { cmd: 'config.snapshot' } }] },
    hello: { commands: P2_COMMANDS } });
  const owner = rt.owner('reveal');
  const pr = rt.call('nodes.reveal', { item_id: 'n1' }, { owner });
  const pj = rt.call('service.start');
  let jobDone = false;
  pj.then(() => { jobDone = true; }, () => { jobDone = true; });
  const pq = rt.query('config.snapshot', {});
  await flush();
  fake.deliver({ t: 'ping', epoch: E1 }, fake.order + 5); // an order jump: a delivery was lost
  await assert.rejects(pr, e => e.code === 'runtime_unavailable' && e.reason_code === 'reconnected');
  await assert.rejects(pq, e => e.code === 'runtime_unavailable' && e.reason_code === 'reconnected');
  await flush();
  assert.equal(jobDone, false, 'the job waiter waits for the replayed job history (req_id)');
  assert.ok(rt.diag().some(d => d.kind === 'reconnect_unanswered' && d.reason === 'order_jump'));
});

test('errorText: message_key -> errors.<code> -> errors.unknown; reasonText: <ns>.<code> -> common.reason_unknown', () => {
  const catalog = { 'errors.start.listener_conflict': 'port busy', 'errors.io': 'io failed', 'errors.unknown': 'unknown error',
    'tiers.reason.timeout': 'timed out', 'common.reason_unknown': 'unknown reason' };
  const T = { t: (id) => (id in catalog ? catalog[id] : id) };
  assert.equal(errorText(T, new RtError('runtime_listener_conflict', { message_key: 'errors.start.listener_conflict' })), 'port busy');
  assert.equal(errorText(T, { code: 'io', message_key: 'errors.bridge.not_in_catalog', params: {} }), 'io failed');
  assert.equal(errorText(T, { code: 'weird_new_code', message_key: 'errors.nope', params: {} }), 'unknown error');
  assert.equal(reasonText(T, 'tiers.reason', 'timeout'), 'timed out');
  assert.equal(reasonText(T, 'tiers.reason', 'brand_new'), 'unknown reason');
  // T.i18n.has takes precedence over the "returns its id" heuristic
  const T2 = { t: id => `[${id}]`, i18n: { has: id => id === 'errors.io' } };
  assert.equal(errorText(T2, { code: 'io', message_key: 'errors.zzz' }), '[errors.io]');
  // T.i18n.hasAny (the T.t chain: current → en_US → zh_CN) is asked before the current-language has
  // (T-P4-H4, P5 #34)
  const T3 = { t: id => `[${id}]`, i18n: { has: () => false, hasAny: id => id === 'errors.zzz' } };
  assert.equal(errorText(T3, { code: 'io', message_key: 'errors.zzz' }), '[errors.zzz]');
  // without T.t (before T-P4-03): the message_key itself
  assert.equal(errorText({}, { code: 'io', message_key: 'errors.start.core_missing' }), 'errors.start.core_missing');
  assert.equal(errorText({}, new RtError('runtime_gone')), 'errors.runtime_gone');
  assert.equal(reasonText({}, 'shell.host_reason', 'dialog_open'), 'shell.host_reason.dialog_open');
  // RtError carries only the code in message
  const e = RtError.from({ code: 'validation', message_key: 'errors.validation', params: { n: 1 }, field: 'dns.x', hold: true, detail: 'secret detail' });
  assert.equal(e.message, 'validation');
  assert.equal(e.field, 'dns.x');
  assert.equal(e.hold, true);
  assert.equal(e.detail, undefined, 'detail is diagnostic only');
});

test('T.rt.errorText / reasonText use T.t when it exists', async () => {
  const { T, rt } = await live();
  assert.equal(rt.errorText({ code: 'io', message_key: 'errors.io' }), 'errors.io');
  T.t = id => (id === 'errors.io' ? 'IO' : id);
  assert.equal(rt.errorText({ code: 'io', message_key: 'errors.io' }), 'IO');
  assert.equal(rt.reasonText('x', 'y'), 'common.reason_unknown');
});

test('calls before connect reject runtime_unavailable', async () => {
  const { makeT } = await import('./rt_helpers.mjs');
  const { createRt } = await import('../../src/rt/client.js');
  const rt = createRt({ T: makeT() });
  await assert.rejects(rt.call('service.start'), e => e.code === 'runtime_unavailable');
  assert.equal(rt.state, 'idle');
  assert.equal(hello().proto, 1);
});

test('owner revoked before the ack: the job is cancelled with the original owner as soon as its op is known', async () => {
  const { rt, fake } = await live(withSteps(onInvoke('exit.verify'), onInvoke('cancel', [ack({ accepted: true })])));
  let p;
  const owner = rt.owner('exit', { onRevoke: () => ({ cancel: p }) });
  p = rt.call('exit.verify', { trigger: 'manual' }, { owner });
  await flush();
  owner.revoke();
  await assert.rejects(p, e => e.code === 'owner_revoked');
  assert.equal(fake.invocations.filter(i => i.cmd === 'cancel').length, 0, 'no op yet');
  fake.deliver(ack({ req_id: fake.invocations[0].req_id, accepted: true, op: 'late-op' }));
  await flush();
  const c = fake.invocations.filter(i => i.cmd === 'cancel');
  assert.equal(c.length, 1);
  assert.deepEqual(c[0].args, { op: 'late-op' });
  assert.equal(c[0].owner, owner.id);
});
