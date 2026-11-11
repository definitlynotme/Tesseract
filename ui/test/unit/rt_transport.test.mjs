// T-P4-02a: transports (order gate, Tauri invoke / Channel, FakeTransport replay) and the
// connect / replay path of the client.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFakeClock } from '../../src/rt/clock.js';
import { RtError } from '../../src/rt/errors.js';
import { createOrderGate, TauriTransport } from '../../src/rt/transport-tauri.js';
import { FakeTransport } from '../../src/rt/transport-fake.js';
import { E1, boot, evt, flush, hello, live, loadScenario, svc, tierSummary } from './rt_helpers.mjs';

test('order gate: first delivery is the baseline, then exactly +1', () => {
  const g = createOrderGate();
  assert.equal(g.check(57), null);
  assert.equal(g.check(58), null);
  assert.equal(g.check(60), 'order_jump');
  const h = createOrderGate();
  h.check(5); assert.equal(h.check(5), 'order_backward');
  assert.equal(createOrderGate().check(-1), 'order_invalid');
  assert.equal(createOrderGate().check('1'), 'order_invalid');
});

// A stand-in for window.__TAURI__ (core.invoke + core.Channel + event.listen).
function fakeTauri() {
  const t = { channels: [], invokes: [], listeners: [], results: {} };
  class Channel { constructor() { this.onmessage = null; t.channels.push(this); } }
  t.core = {
    Channel,
    invoke: (name, args) => {
      t.invokes.push([name, args]);
      const r = t.results[name];
      if (typeof r === 'function') return r(args);
      if (r instanceof Error || typeof r === 'string') return Promise.reject(r);
      return Promise.resolve(r === undefined ? null : r);
    },
  };
  t.event = { listen: (name, fn) => { t.listeners.push([name, fn]); return Promise.resolve(() => {}); } };
  t.send = (order, frame, ch = t.channels[t.channels.length - 1]) => ch.onmessage({ order, frame });
  return t;
}

test('TauriTransport: rt_connect with a Channel; order jump drops the Channel and reconnects', async () => {
  const tauri = fakeTauri();
  const clock = createFakeClock();
  const tr = new TauriTransport({ tauri, clock });
  const got = [], diags = [], reconnects = [];
  await tr.connect(d => got.push(d), { onDiag: d => diags.push(d), onReconnect: r => reconnects.push(r) });
  assert.equal(tr.kind, 'tauri');
  assert.deepEqual(tauri.invokes.map(i => i[0]), ['rt_connect']);
  assert.ok(tauri.invokes[0][1].channel instanceof tauri.core.Channel);
  const first = tauri.channels[0];
  tauri.send(10, { t: 'rt:reset', epoch: 'e', reason: 'reconnect' });
  tauri.send(11, { t: 'hello' });
  tauri.send(13, { t: 'evt' }); // jump: fault
  assert.equal(got.length, 2);
  assert.deepEqual(reconnects, ['order_jump']);
  assert.equal(diags[0].kind, 'transport_fault');
  assert.equal(tauri.invokes.filter(i => i[0] === 'rt_connect').length, 2);
  // the old Channel is dead: late deliveries are ignored
  tauri.send(14, { t: 'evt' }, first);
  assert.equal(got.length, 2);
  // the new Channel takes its own baseline
  tauri.send(100, { t: 'rt:reset', epoch: 'e', reason: 'reconnect' });
  assert.equal(got.length, 3);
});

test('TauriTransport: invoke string rejections become RtError{code}; host invokes go straight through', async () => {
  const tauri = fakeTauri();
  const tr = new TauriTransport({ tauri, clock: createFakeClock() });
  for (const code of ['runtime_unavailable', 'job_capacity', 'not_allowed', 'invalid_type', 'internal']) {
    tauri.results.rt = code;
    await assert.rejects(tr.invoke('rt', { req_id: 'p1.1', cmd: 'service.start', args: {} }), e => e instanceof RtError && e.code === code && e.message === code);
  }
  tauri.results.rt = { queued: true, id: 7 };
  assert.deepEqual(await tr.invoke('rt', { req_id: 'p1.2', cmd: 'service.start', args: {} }), { queued: true, id: 7 });
  tauri.results.host_force_restart = 'not_unresponsive';
  await assert.rejects(tr.invokeHost('host_force_restart'), e => e.code === 'not_unresponsive');
  assert.throws(() => new TauriTransport({ tauri: null }), e => e.code === 'runtime_unavailable');
});

test('TauriTransport: a stuck Channel (rt:reset event without the Channel copy) reconnects; a live one does not', async () => {
  const tauri = fakeTauri();
  const clock = createFakeClock();
  const tr = new TauriTransport({ tauri, clock, stuckMs: 1500 });
  const reasons = [];
  await tr.connect(() => {}, { onReconnect: r => reasons.push(r) });
  await flush();
  const [[name, fire]] = tauri.listeners;
  assert.equal(name, 'rt:reset');
  // Channel copy first, event second: alive.
  tauri.send(1, { t: 'rt:reset', epoch: 'e', reason: 'reconnect' });
  fire({});
  clock.advance(3000);
  assert.deepEqual(reasons, []);
  // Event first, Channel copy in time: alive.
  clock.advance(2000);
  fire({});
  tauri.send(2, { t: 'rt:reset', epoch: 'e', reason: 'reconnect' });
  clock.advance(3000);
  assert.deepEqual(reasons, []);
  // Event with no Channel copy: stuck.
  clock.advance(2000);
  fire({});
  clock.advance(1600);
  assert.deepEqual(reasons, ['channel_stuck']);
});

test('TauriTransport: a failed rt_connect is retried', async () => {
  const tauri = fakeTauri();
  const clock = createFakeClock();
  let n = 0;
  tauri.results.rt_connect = () => (++n === 1 ? Promise.reject('internal') : Promise.resolve(null));
  const tr = new TauriTransport({ tauri, clock, retryMs: 1000 });
  const diags = [];
  assert.equal(await tr.connect(() => {}, { onDiag: d => diags.push(d) }), false);
  assert.equal(diags[0].kind, 'rt_connect_failed');
  clock.advance(1000);
  await flush();
  assert.equal(n, 2);
});

test('connect replay: hello -> snapshots -> increments; first frame per topic is instant', async () => {
  const { T, rt, fake } = boot({ scenario: loadScenario('rt_connect_replay') });
  assert.equal(rt.state, 'connecting');
  await flush();
  assert.deepEqual(fake.failures, []);
  assert.equal(rt.state, 'live');
  assert.equal(rt.epoch, 'rt-fixture-e1');
  assert.equal(T.store.get('run'), 'running');
  assert.equal(T.store.get('lang'), 'zh_CN');
  assert.equal(T.store.get('appVersion'), '1.0.0');
  assert.ok(T.store.get('caps').topics.includes('service.state'));
  assert.equal(rt.can('service.start'), true);
  assert.equal(rt.can('settings.apply'), false);
  assert.equal(rt.has('traffic.tick'), true);
  assert.equal(rt.has('network.mode'), false);
  const states = T.named('rt:state');
  assert.equal(states.length, 1);
  assert.equal(states[0].instant, true, 'the replayed snapshot never lights up');
  const ticks = T.named('rt:tick');
  assert.deepEqual(ticks.map(t => t.instant), [true, false], 'the live increment after the replay is not instant');
  assert.equal(T.store.get('traffic').rateDown, 9000);
  assert.equal(T.store.get('uptime'), 121);
  assert.deepEqual(T.named('rt:logs').map(l => l.seq), [41]);
});

test('connect: an order jump on the Channel triggers a reconnect and a replay', async () => {
  const scenario = { steps: [
    { deliver: { t: 'rt:reset', epoch: E1, reason: 'reconnect' } },
    { deliver: hello() },
    { deliver: evt('service.state', 1, 'snapshot', svc()) },
    { deliver: evt('tiers.summary', 1, 'snapshot', tierSummary()), order: 9 },
  ], replay: [
    { t: 'rt:reset', epoch: E1, reason: 'reconnect' }, hello(),
    evt('service.state', 1, 'snapshot', svc()), evt('tiers.summary', 1, 'snapshot', tierSummary()),
  ] };
  const { T, rt, fake } = boot({ scenario });
  const conn = [];
  T.on('rt:conn', e => conn.push(e.state));
  await flush(6);
  assert.equal(fake.connects, 2);
  assert.ok(conn.includes('reconnecting'));
  assert.equal(rt.state, 'live');
  assert.equal(T.store.get('carrier'), 'vanguard', 'the replay delivered what the broken Channel lost');
  assert.ok(rt.diag().some(d => d.kind === 'transport_fault' && d.code === 'order_jump'));
});

test('FakeTransport: ?lang rewrites hello.lang; unmatched calls are failures', async () => {
  const clock = createFakeClock();
  const fake = new FakeTransport({ scenario: { lang: 'zh_CN', steps: [{ deliver: hello({ lang: 'zh_CN' }) }] }, clock, lang: 'de_DE' });
  const got = [];
  await fake.connect(d => got.push(d.frame));
  assert.equal(got[0].lang, 'de_DE');
  await assert.rejects(fake.invoke('rt', { req_id: 'p1.1', cmd: 'service.start', args: {} }), e => e.code === 'not_allowed');
  assert.deepEqual(fake.failures, [{ kind: 'unmatched_invoke', cmd: 'service.start', args: {} }]);
  await assert.rejects(fake.invokeHost('host_ready'), e => e.code === 'not_allowed');
  assert.equal(fake.failures[1].kind, 'unmatched_host');
  // scenario lang applies when ?lang is absent
  const f2 = new FakeTransport({ scenario: { lang: 'ja_JP', steps: [{ deliver: hello({ lang: 'zh_CN' }) }] } });
  const got2 = [];
  await f2.connect(d => got2.push(d.frame));
  assert.equal(got2[0].lang, 'ja_JP');
});

// T-P4-H (决定_第五波裁定 02b-3): the sim mode exists (T-P4-02b); what it cannot do is run
// without a backend, and the error says so. An unknown mode is named as such.
test('FakeTransport: sim mode requires a backend (replay, attach, invoke); with one it replays and answers through it', async () => {
  assert.throws(() => new FakeTransport({ mode: 'sim' }), /mode sim requires a sim backend with replay\(\), attach\(\) and invoke\(\)/);
  assert.throws(() => new FakeTransport({ mode: 'sim', sim: { replay: () => [], attach() {} } }), /requires a sim backend/, 'invoke() missing');
  assert.throws(() => new FakeTransport({ mode: 'live' }), /unknown mode live \(expected fixture or sim\)/);
  let push = null;
  const backend = {
    replay: () => [{ t: 'rt:reset', epoch: E1, reason: 'reconnect' }, hello()],
    attach(fn) { push = fn; },
    invoke(p) { push({ t: 'ack', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: p.id, req_id: p.req_id, accepted: true, op: 'op-1' }); },
  };
  const fake = new FakeTransport({ mode: 'sim', sim: backend, lang: 'de_DE' });
  assert.equal(fake.mode, 'sim');
  const got = [];
  await fake.connect(d => got.push(d.frame));
  assert.deepEqual(got.map(f => f.t), ['rt:reset', 'hello'], 'the backend replay is delivered on connect');
  assert.equal(got[1].lang, 'de_DE', '?lang rewrites hello.lang in sim mode too');
  const r = await fake.invoke('rt', { req_id: 'p1.1', cmd: 'service.start', args: {} });
  assert.equal(got.at(-1).t, 'ack');
  assert.equal(got.at(-1).id, r.id, 'the backend answers through push, echoing the invoke id');
  assert.deepEqual(fake.failures, [], 'sim-mode calls are never fixture failures');
});

test('FakeTransport: advance_ms drives the injected clock; jump_ms moves time without timers', async () => {
  const clock = createFakeClock(0);
  const fired = [];
  clock.after(500, () => fired.push(clock.now()));
  const fake = new FakeTransport({ scenario: { steps: [{ advance_ms: 400 }, { jump_ms: 1000 }, { advance_ms: 0 }] }, clock });
  await fake.connect(() => {});
  assert.deepEqual(fired, [1400]);
  assert.equal(clock.now(), 1400);
});

test('live(): reconnect on the same epoch keeps the connection id format', async () => {
  const { rt } = await live();
  assert.match(rt.reqId(), /^pabc\.\d+$/);
});
