// T-P4-02a: stale judgement (P4 architecture section 2.8) on the fake clock.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFakeClock } from '../../src/rt/clock.js';
import { createStale } from '../../src/rt/stale.js';
import { evt, live, makeT, svc } from './rt_helpers.mjs';

const tick = (t, running = true) => ({ t_ms: t, up_bps: 1, down_bps: 2, session: { up_bytes: 1, down_bytes: 2 }, running, error_code: null });
const V1 = { hello: { topics: ['service.state', 'traffic.tick', 'tiers.summary', 'network.mode', 'exit.identity', 'service.collect', 'jobs', 'logs.batch'] } };
const stale = T => T.store.get('stale') || {};

test('service.state: stale after 3 s without a frame while starting / running / stopping; not when stopped', async () => {
  const { T, fake, clock } = await live(V1, [evt('service.state', 1, 'snapshot', svc())]);
  assert.equal(stale(T)['service.state'], false);
  clock.advance(3000);
  assert.equal(stale(T)['service.state'], false, 'exactly 3 s is still fresh');
  clock.advance(1000);
  assert.equal(stale(T)['service.state'], true);
  const events = T.named('rt:stale').filter(e => e.topic === 'service.state');
  assert.deepEqual(events.map(e => e.stale).slice(-1), [true]);
  fake.deliver(evt('service.state', 2, 'snapshot', svc({ phase: 'stopped' })));
  assert.equal(stale(T)['service.state'], false);
  clock.advance(60000);
  assert.equal(stale(T)['service.state'], false, 'a stopped service does not age');
  for (const phase of ['collecting', 'stopping']) {
    fake.deliver(evt('service.state', phase === 'collecting' ? 3 : 4, 'snapshot', svc({ phase })));
    clock.advance(4000);
    assert.equal(stale(T)['service.state'], true, phase);
  }
});

test('service.state: freshness.stale=true is stale at once; the new epoch is stale until its first snapshot', async () => {
  const { T, fake } = await live(V1, []);
  assert.equal(stale(T)['service.state'], true, 'no snapshot yet');
  assert.equal(stale(T)['tiers.summary'], true);
  fake.deliver(evt('service.state', 1, 'snapshot', svc({ fresh: true })));
  assert.equal(stale(T)['service.state'], true);
  fake.deliver(evt('service.state', 2, 'snapshot', svc()));
  assert.equal(stale(T)['service.state'], false);
});

test('traffic.tick: ages only while the Core runs', async () => {
  const { T, fake, clock } = await live(V1, [evt('service.state', 1, 'snapshot', svc()), evt('traffic.tick', 1, 'snapshot', tick(1))]);
  clock.advance(2900);
  fake.deliver(evt('service.state', 2, 'snapshot', svc()));
  clock.advance(1000);
  assert.equal(stale(T)['traffic.tick'], true, '3.9 s without a sample while the Core runs');
  fake.deliver(evt('traffic.tick', 2, 'snapshot', tick(2)));
  assert.equal(stale(T)['traffic.tick'], false);
  fake.deliver(evt('service.state', 3, 'snapshot', svc({ phase: 'stopped' })));
  fake.deliver(evt('traffic.tick', 3, 'snapshot', tick(3, false)));
  clock.advance(120000);
  assert.equal(stale(T)['traffic.tick'], false, 'Core stopped: no aging');
});

test('change-driven topics never age with time', async () => {
  const { T, clock } = await live(V1, [evt('tiers.summary', 1, 'snapshot', { carrier: null, offline_reason: null, order: [], degraded: false, fallback: false,
    tiers: { vanguard: { enabled: true, total: 0, usable: 0 }, sentinel: { enabled: true, total: 0, usable: 0 }, bastion: { enabled: true, total: 0, usable: 0 }, gateway: { enabled: false, total: 0, usable: 0 } }, pool: { usable: 0, total: 0 } }),
  evt('network.mode', 1, 'snapshot', { mode: 'system', admin: false, switching: false, proxy_state: 'owned', drift: false })]);
  clock.advance(10 * 60000);
  assert.equal(stale(T)['tiers.summary'], false);
  assert.equal(stale(T)['network.mode'], false);
});

test('generation: a payload generation other than intent.runtime_generation is stale', async () => {
  const collect = gen => ({ generation: gen, observed_at_ms: 1, tiers: ['vanguard', 'sentinel', 'bastion', 'gateway'].map(tier => ({ tier, generation: gen, first_check: 'complete', source_count: null, healthy: null, required: null, observed_at_ms: null })) });
  const { T, fake } = await live(V1, [evt('service.state', 1, 'snapshot', svc({ gen: 4 })), evt('service.collect', 1, 'snapshot', collect(4))]);
  assert.equal(stale(T)['service.collect'], false);
  fake.deliver(evt('service.state', 2, 'snapshot', svc({ gen: 5 })));
  assert.equal(stale(T)['service.collect'], true, 'the runtime moved on; the collect facts are of the old generation');
  fake.deliver(evt('service.collect', 2, 'snapshot', collect(5)));
  assert.equal(stale(T)['service.collect'], false);
});

test('suspend: after a > 5 s gap between checks the time rules wait a 3 s grace', () => {
  const T = makeT();
  const clock = createFakeClock(0);
  let run = 'running';
  const s = createStale({ clock, store: T.store, emit: T.emit, getRun: () => run, getCoreRunning: () => true });
  const stopCheck = clock.every(1000, () => s.check());
  s.frame('service.state', null, { full: true });
  clock.advance(1000);
  assert.equal(s.isStale('service.state'), false);
  // the machine sleeps for 60 s: no timer runs, then everything resumes
  clock.jump(60000);
  clock.advance(0);
  assert.equal(s.isStale('service.state'), false, 'just woke: grace');
  clock.advance(2000);
  assert.equal(s.isStale('service.state'), false, 'still inside the 3 s grace');
  s.frame('service.state', null, { full: true });
  clock.advance(2000);
  assert.equal(s.isStale('service.state'), false, 'a frame arrived after waking');
  clock.advance(2000);
  assert.equal(s.isStale('service.state'), true, 'then the normal 3 s rule applies');
  run = 'stopped';
  s.check();
  assert.equal(s.isStale('service.state'), false);
  stopCheck();
});

test('jobs and logs.batch are never stale', async () => {
  const { T, fake, clock } = await live(V1, []);
  fake.deliver(evt('jobs', 1, 'edge', { op: 'a', name: 'service.start', state: 'queued', started_at_ms: null }, { seq: 1 }));
  fake.deliver(evt('logs.batch', 1, 'edge', { from_seq: 1, records: [], dropped: 0, dropped_total: 0, gap: false }, { seq: 1 }));
  clock.advance(60000);
  assert.equal(stale(T).jobs, undefined);
  assert.equal(stale(T)['logs.batch'], undefined);
});

test('exit.identity: its generation is the Core generation; idle (generation 0) and a Core generation other than the runtime generation are not stale', async () => {
  const leg = { status: 'ok', address: '203.0.113.1', country: 'JP', reason_code: null, observed_at_ms: 1 };
  const result = (status, generation) => ({ request_id: status === 'idle' ? 'idle' : 'q1', generation, binding_id: null, status, reason_code: null, v4: leg, v6: leg,
    fresh: status === 'ok', observed_at_ms: 9, native_fallback_verified: false, native_fallback_verified_commit_rejected: false,
    native_fallback_unverified_budget: false, manual_execution_binding_stale: false });
  // runtime_generation 4, core_generation 3 (rt_helpers svc)
  const { T, fake } = await live(V1, [evt('service.state', 1, 'snapshot', svc({ gen: 4 })), evt('exit.identity', 1, 'snapshot', { current: result('idle', 0) })]);
  assert.equal(stale(T)['exit.identity'], false, 'the idle result (generation 0) is not stale');
  fake.deliver(evt('exit.identity', 2, 'snapshot', { current: result('ok', 3) }));
  assert.equal(stale(T)['exit.identity'], false, 'Core generation 3 under runtime generation 4 is current');
  // the Core restarts (core_generation 3 -> 5): the old exit result belongs to the old Core
  const restarted = svc({ gen: 4 });
  restarted.core.core_generation = 5;
  fake.deliver(evt('service.state', 2, 'snapshot', restarted));
  assert.equal(stale(T)['exit.identity'], true, 'a result of another Core generation is stale');
  fake.deliver(evt('exit.identity', 3, 'snapshot', { current: result('idle', 0) }));
  assert.equal(stale(T)['exit.identity'], false, 'idle is never judged by generation');
  fake.deliver(evt('exit.identity', 4, 'snapshot', { current: result('ok', 5) }));
  assert.equal(stale(T)['exit.identity'], false);
});
