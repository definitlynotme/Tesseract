// T-P4-02a: the two kinds of rt:reset (criticism B9), epoch filtering, host:* exemption.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { E1, E2, evt, flush, hello, job, live, loadScenario, boot, svc, tierSummary } from './rt_helpers.mjs';

const startScenario = () => ({ steps: [
  { on_invoke: { cmd: 'service.start' }, reply: [{ t: 'ack', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 2, accepted: true, op: 'op-1' }] },
] });

test('rt:reset runtime_gone: in-flight promises reject runtime_gone, run goes unknown, the cache is cleared', async () => {
  const { T, rt, fake } = await live({ scenario: startScenario() }, [
    evt('service.state', 1, 'snapshot', svc()), evt('tiers.summary', 1, 'snapshot', tierSummary()),
  ]);
  const p = rt.call('service.start');
  await flush();
  assert.equal(T.store.get('run'), 'running');
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'runtime_gone' });
  await assert.rejects(p, e => e.code === 'runtime_gone');
  assert.equal(T.store.get('run'), 'unknown');
  assert.deepEqual(T.named('rt:state').pop(), { run: 'unknown', prev: 'running', cancelled: false, instant: true }, 'the shell hears run go unknown');
  assert.equal(rt.topic('service.state').get(), undefined, 'cache cleared');
  assert.equal(rt.state, 'resetting');
  const reset = T.named('rt:reset').pop();
  assert.equal(reset.generation_changed, true);
  assert.equal(T.store.get('stale')['service.state'], true, 'stale until the first snapshot of the new generation');
  // the new generation
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  fake.deliver(hello({ epoch: E2 }));
  assert.equal(rt.state, 'live');
  fake.deliver({ ...evt('service.state', 1, 'snapshot', svc({ phase: 'stopped' })), epoch: E2 });
  assert.equal(T.store.get('run'), 'stopped');
  assert.equal(T.store.get('stale')['service.state'], false);
  // a late frame of the old epoch is dropped
  fake.deliver(evt('service.state', 9, 'snapshot', svc({ phase: 'running' })));
  assert.equal(T.store.get('run'), 'stopped');
  assert.ok(rt.diag().some(d => d.kind === 'epoch_mismatch'));
});

test('same-epoch rt:reset reconnect: cache, store and waiters unchanged; replayed duplicates dropped; logs not repeated', async () => {
  const { T, rt, fake } = await live({ scenario: startScenario() }, [
    evt('service.state', 1, 'snapshot', svc()),
    evt('logs.batch', 1, 'edge', { from_seq: 1, records: [], dropped: 0, dropped_total: 0, gap: false }, { seq: 1 }),
  ]);
  const p = rt.call('service.start');
  await flush();
  fake.deliver(evt('jobs', 1, 'edge', job('op-1', 'service.start', 'running'), { seq: 1 }));
  const statesBefore = T.named('rt:state').length;
  const before = T.store.snapshot();
  // the shell's replay after a reconnect
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  fake.deliver(hello());
  fake.deliver(evt('service.state', 1, 'snapshot', svc()));
  fake.deliver(evt('jobs', 1, 'edge', job('op-1', 'service.start', 'running'), { seq: 1 }));
  fake.deliver(evt('logs.batch', 1, 'edge', { from_seq: 1, records: [], dropped: 0, dropped_total: 0, gap: false }, { seq: 1 }));
  assert.equal(T.named('rt:reset').pop().generation_changed, false);
  assert.equal(T.store.get('run'), before.run);
  assert.equal(T.named('rt:state').length, statesBefore, 'no second rt:state from the duplicate');
  assert.equal(T.named('rt:logs').length, 1, 'the replayed log batch is not repeated');
  assert.equal(rt.topic('service.state').get().runtime.startup_phase, 'running');
  // the waiter survived and completes from the live job edge
  fake.deliver(evt('jobs', 2, 'edge', job('op-1', 'service.start', 'done', { result: { startup_phase: 'running', failure_code: null } }), { seq: 2 }));
  assert.deepEqual(await p, { startup_phase: 'running', failure_code: null });
});

test('an epoch change with reason reconnect is still a generation change', async () => {
  const { T, rt, fake } = await live({ scenario: startScenario() }, [evt('service.state', 1, 'snapshot', svc())]);
  const p = rt.call('service.start');
  await flush();
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'reconnect' });
  await assert.rejects(p, e => e.code === 'runtime_gone');
  assert.equal(T.named('rt:reset').pop().generation_changed, true);
  assert.equal(T.store.get('run'), 'unknown');
  assert.equal(rt.epoch, E2);
});

test('a hello with a new epoch and no rt:reset before it is treated as a generation change', async () => {
  const { T, rt, fake } = await live({}, [evt('service.state', 1, 'snapshot', svc())]);
  fake.deliver(hello({ epoch: E2 }));
  assert.equal(rt.epoch, E2);
  assert.equal(T.store.get('run'), 'unknown');
  assert.equal(T.named('rt:reset').pop().generation_changed, true);
});

test('the first connect is not reported as a generation change', async () => {
  const { T } = await live();
  assert.deepEqual(T.named('rt:reset').map(r => r.generation_changed), [false]);
});

test('host:* frames are never epoch-filtered; host:language writes store lang; rt:log-gap is forwarded', async () => {
  const { T, fake } = boot({ scenario: loadScenario('rt_host_frames') });
  await flush();
  assert.deepEqual(fake.failures, []);
  assert.equal(T.store.get('lang'), 'en_US');
  assert.equal(T.named('host:banner')[0].kind, 'sidecar_unresponsive');
  assert.equal(T.named('host:notify')[0].delivery_id, 'd-1');
  assert.equal(T.named('host:navigate')[0].page, 'update');
  assert.equal(T.named('host:confirmQuit')[0].token, 7);
  assert.equal(T.named('host:visibility')[0].visible, false);
  assert.equal(T.named('rt:log-gap')[0].to_seq, 40);
  fake.deliver({ t: 'host:banner', kind: 'sidecar_crashed', active: true, state: 'restarting', exit_code: 1, restarts: 1, limit: 3, epoch: 'other-epoch' });
  assert.equal(T.named('host:banner').length, 2, 'a host frame stamped with another epoch still arrives');
});

test('runtime_gone scenario: crash banner, new epoch, run follows the new generation', async () => {
  const { T, rt, fake } = boot({ scenario: loadScenario('rt_runtime_gone') });
  await flush();
  assert.deepEqual(fake.failures, []);
  assert.equal(rt.epoch, 'rt-fixture-e2');
  assert.equal(T.store.get('run'), 'stopped');
  assert.deepEqual(T.named('rt:reset').map(r => r.generation_changed), [false, true, false], 'one lost generation, one generation_changed');
  assert.deepEqual(T.named('host:banner').map(b => b.state), ['restarting', 'restarted']);
});

test('host:caps feeds hostHas (invoke names); before it every hostHas is false', async () => {
  const { rt, fake } = await live();
  assert.equal(rt.hostHas('rt_resync'), false);
  assert.equal(rt.hostHas('host_ready'), false);
  fake.deliver({ t: 'host:caps', epoch: E1, invokes: ['rt', 'rt_connect', 'rt_resync', 'host_ready'], events: ['rt:replayed'] });
  assert.equal(rt.hostHas('rt_resync'), true);
  assert.equal(rt.hostHas('host_ready'), true);
  assert.equal(rt.hostHas('host.ready'), false);
});

test('with rt:replayed announced, every frame before it is instant and none after', async () => {
  const { T, fake } = await live();
  fake.deliver({ t: 'host:caps', epoch: E1, invokes: [], events: ['rt:replayed'] });
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  fake.deliver(hello());
  fake.deliver(evt('service.state', 1, 'snapshot', svc({ phase: 'stopped' })));
  fake.deliver(evt('service.state', 2, 'snapshot', svc({ phase: 'stopped', gen: 4 })));
  fake.deliver({ t: 'rt:replayed', epoch: E1 });
  fake.deliver(evt('service.state', 3, 'snapshot', svc({ phase: 'collecting' })));
  const states = T.named('rt:state');
  assert.deepEqual(states.map(s => [s.run, s.instant]), [['stopped', true], ['starting', false]]);
});

test('same-epoch reconnect: the replayed rt:log-gap is forwarded once per (epoch, from_seq, to_seq)', async () => {
  const gap = (to) => ({ t: 'rt:log-gap', epoch: E1, data: { from_seq: 1, to_seq: to, dropped: 2, record_from_seq: 1, record_to_seq: 9, record_dropped: 9 } });
  const { T, fake } = await live({}, [gap(40)]);
  assert.equal(T.named('rt:log-gap').length, 1);
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  fake.deliver(hello());
  fake.deliver(gap(40));
  assert.equal(T.named('rt:log-gap').length, 1, 'the replay rebuilds the same gap: not forwarded again');
  fake.deliver(gap(52));
  assert.equal(T.named('rt:log-gap').length, 2, 'a gap that grew is new');
  // a new generation forgets the seen gaps
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  fake.deliver(hello({ epoch: E2 }));
  fake.deliver({ ...gap(40), epoch: E2 });
  assert.equal(T.named('rt:log-gap').length, 3);
});

// T-P4-H (决定_第五波裁定 C3-2): a new epoch's seed window ends with rt:replayed (up to the
// shell's SEED_GRACE = 15 s). A job this page starts inside that window is this page's own
// call, not a replay: its frames are live (instant:false). Replayed jobs and jobs of another
// page stay instant until rt:replayed; a call of the previous epoch is nobody's after the
// generation change.
test('seed window: own calls are live, replay and other pages\' jobs instant; an old epoch\'s call is not ours', async () => {
  const ack = (epoch, op) => ({ t: 'ack', epoch, produced_at_ms: 1, sent_at_ms: 1, id: 2, accepted: true, op });
  const scenario = { steps: [
    { on_invoke: { cmd: 'service.restart' }, reply: [ack(E1, 'old-1')] },
    { on_invoke: { cmd: 'service.restart' }, reply: [ack(E2, 'own-1')] },
  ] };
  const { T, rt, fake } = await live({ scenario }, [evt('service.state', 1, 'snapshot', svc())]);
  const j2 = (seq, op, state, extra = {}, name = 'service.restart') => ({ ...evt('jobs', seq, 'edge', job(op, name, state, extra), { seq }), epoch: E2 });
  const done = { result: { applied: true, core_running: true, core_cleanup_pending: false, core_generation: 4 } };
  // a call of epoch E1 that the generation change ends
  const old = rt.call('service.restart');
  await flush();
  assert.equal(rt.hostHas('rt_resync'), false);
  // the new epoch: rt:reset, host:caps (rt:replayed announced), hello, then the seed window
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  await assert.rejects(old, e => e.code === 'runtime_gone');
  fake.deliver({ t: 'host:caps', epoch: E2, invokes: ['rt', 'rt_connect'], events: ['rt:replayed'] });
  fake.deliver(hello({ epoch: E2 }));
  fake.deliver({ ...evt('service.state', 1, 'snapshot', svc()), epoch: E2 });
  const before = T.named('rt:job').length;
  // 1. the replay / another page's job inside the window: instant
  fake.deliver(j2(1, 'other-1', 'running', {}, 'geo.update'));
  // 2. this page starts a job inside the window: its frames are live
  const own = rt.call('service.restart');
  await flush();
  fake.deliver(j2(2, 'own-1', 'queued'));
  fake.deliver(j2(3, 'own-1', 'running'));
  // 3. the op of the E1 call, seen again in E2 (an op name reused by the new sidecar): not ours
  fake.deliver(j2(4, 'old-1', 'running', {}, 'geo.update'));
  fake.deliver(j2(5, 'own-1', 'done', done));
  assert.equal((await own).applied, true);
  const seen = T.named('rt:job').slice(before).map(e => [e.job.op, e.job.state, e.instant]);
  assert.deepEqual(seen, [
    ['other-1', 'running', true],
    ['own-1', 'queued', false],
    ['own-1', 'running', false],
    ['old-1', 'running', true],
    ['own-1', 'done', false],
  ]);
  assert.deepEqual(T.named('rt:restart').map(e => e.state), ['begin', 'done'], 'the own restart lights up inside the window');
  // after rt:replayed every job frame is live, as before
  fake.deliver({ t: 'rt:replayed', epoch: E2 });
  fake.deliver(j2(6, 'other-1', 'done', { result: { triggered: true, completed: true, status: { running: false, updated_at_ms: 1, errors: [] } } }, 'geo.update'));
  assert.deepEqual(T.named('rt:job').slice(-1).map(e => [e.job.op, e.instant]), [['other-1', false]]);
});

test('no rt:replayed announced: own calls live, every other job frame instant; after a new epoch an old call is not ours', async () => {
  const scenario = { steps: [
    { on_invoke: { cmd: 'service.restart' }, reply: [{ t: 'ack', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 2, accepted: true, op: 'old-1' }] },
  ] };
  const { T, rt, fake } = await live({ scenario }, [evt('service.state', 1, 'snapshot', svc())]);
  const old = rt.call('service.restart');
  await flush();
  fake.deliver(evt('jobs', 1, 'edge', job('old-1', 'service.restart', 'queued'), { seq: 1 }));
  fake.deliver(evt('jobs', 2, 'edge', job('x', 'service.restart', 'queued'), { seq: 2 }));
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  await assert.rejects(old, e => e.code === 'runtime_gone');
  fake.deliver(hello({ epoch: E2 }));
  fake.deliver({ ...evt('jobs', 1, 'edge', job('old-1', 'service.restart', 'running'), { seq: 1 }), epoch: E2 });
  assert.deepEqual(T.named('rt:job').map(e => [e.job.op, e.instant]), [['old-1', false], ['x', true], ['old-1', true]]);
});
