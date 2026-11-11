// T-P4-02a: projection v1 DTO -> store keys + rt:* events, one case per mapping_core 1.2 row
// at least, and the derived values (run / phase / cancelling, killswitch, tier state, rt:tier).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { cancellingOf, exitOf, killswitchOf, phaseOf, runOf, tierState, updateStateOf } from '../../src/rt/project.js';
import { E1, E2, boot, evt, flush, hello, job, live, loadScenario, python, svc, tierSummary } from './rt_helpers.mjs';

const ALL_TOPICS = ['service.state', 'route.published', 'tiers.summary', 'traffic.tick', 'traffic.history', 'logs.batch', 'core.info', 'jobs',
  'service.collect', 'tier.transition', 'exit.identity', 'protocols.mix', 'network.mode', 'killswitch.state', 'bastion.pool', 'gateway.current',
  'config.changed', 'update.state'];
const V1 = { hello: { topics: ALL_TOPICS, commands: ['service.start', 'service.stop', 'service.restart', 'mode.set', 'exit.verify'] } };
const S = T => T.store.get.bind(T.store);

test('run / phase / cancelling for every startup phase with and without stop_requested / validation', () => {
  const rows = [
    // phase,        validation, stop,  run,        phase, cancelling
    ['collecting', false, false, 'starting', 0, false],
    ['collecting', false, true, 'starting', 0, true],
    ['core_standby', true, false, 'starting', 1, false],
    ['core_standby', true, true, 'starting', 1, true],
    ['core_standby', false, false, 'starting', -1, false],
    ['core_standby', false, true, 'starting', -1, true],
    ['running', false, false, 'running', 2, false],
    ['running', true, true, 'running', 2, false],
    ['stopping', false, true, 'stopping', -1, false],
    ['stopped', false, false, 'stopped', -1, false],
    ['failed', false, false, 'failed', -1, false],
    ['failed', false, true, 'failed', -1, false],
  ];
  for (const [phase, validation, stop, run, ph, cancelling] of rows) {
    const d = svc({ phase, validation, stop });
    assert.equal(runOf(d), run, `${phase} run`);
    assert.equal(phaseOf(d), ph, `${phase}/${validation} phase`);
    assert.equal(cancellingOf(d), cancelling, `${phase}/${stop} cancelling`);
  }
  assert.equal(runOf(null), 'unknown');
  assert.equal(runOf(svc({ phase: 'brand_new_phase' })), 'unknown');
});

test('service.state row: run, phase, cancelling, uptime, failure, readiness, core; rt:state with cancelled', async () => {
  const { T, fake } = await live(V1, []);
  const get = S(T);
  assert.equal(get('run'), 'unknown', 'no snapshot yet');
  fake.deliver(evt('service.state', 1, 'snapshot', svc({ phase: 'collecting' })));
  assert.equal(get('run'), 'starting');
  assert.equal(get('phase'), 0);
  fake.deliver(evt('service.state', 2, 'edge', svc({ phase: 'collecting', stop: true }), { seq: 1 }));
  assert.equal(get('cancelling'), true);
  fake.deliver(evt('service.state', 3, 'edge', svc({ phase: 'stopped', stop: true }), { seq: 2 }));
  const st = T.named('rt:state');
  assert.deepEqual(st.map(s => [s.prev, s.run, s.cancelled, s.instant]), [['unknown', 'starting', false, true], ['starting', 'stopped', true, false]]);
  const failure = { code: 'runtime_listener_conflict', reason_code: 'listener_conflict', message_key: 'errors.start.listener_conflict', params: { port: 7890 } };
  const failed = svc({ phase: 'failed' });
  failed.runtime.failure = failure; failed.runtime.failure_code = failure.code;
  fake.deliver(evt('service.state', 4, 'edge', failed, { seq: 3 }));
  assert.equal(get('run'), 'failed');
  assert.deepEqual(get('failure'), failure);
  fake.deliver(evt('service.state', 5, 'edge', svc({ uptime: 77 }), { seq: 4 }));
  assert.equal(get('uptime'), 77);
  assert.equal(get('failure'), null);
  assert.equal(get('readiness').state, 'ready');
  assert.deepEqual(get('core'), { running: true, cleanup_pending: false, generation: 3, pid: 7 });
  assert.deepEqual(T.named('rt:phase').map(p => p.phase), [0, -1, 2]);
});

test('core.info row merges into store core with the version', async () => {
  const { T, fake } = await live(V1, [evt('service.state', 1, 'snapshot', svc())]);
  fake.deliver(evt('core.info', 1, 'snapshot', { running: true, pid: 7, version: 'v1.19.27', generation: 3, cleanup_pending: false }));
  assert.deepEqual(T.store.get('core'), { running: true, cleanup_pending: false, generation: 3, version: 'v1.19.27', pid: 7 });
});

test('restarting row: service.restart job queued/running -> true, terminal -> false; rt:restart begin/done for a call of this page', async () => {
  const scenario = { steps: [{ on_invoke: { cmd: 'service.restart' }, reply: [{ t: 'ack', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 2, accepted: true, op: 'r' }] }] };
  const { T, rt, fake } = await live({ ...V1, scenario }, []);
  const p = rt.call('service.restart');
  await flush();
  fake.deliver(evt('jobs', 1, 'edge', job('r', 'service.restart', 'queued'), { seq: 1 }));
  fake.deliver(evt('jobs', 2, 'edge', job('r', 'service.restart', 'running'), { seq: 2 }));
  assert.equal(T.store.get('restarting'), true);
  fake.deliver(evt('jobs', 3, 'edge', job('r', 'service.restart', 'done', { result: { applied: true, core_running: true, core_cleanup_pending: false, core_generation: 4 } }), { seq: 3 }));
  assert.equal(T.store.get('restarting'), false);
  assert.deepEqual(T.named('rt:restart').map(e => e.state), ['begin', 'done']);
  assert.equal((await p).applied, true);
  // a job that is not this page's (replayed history, an earlier page load) is instant: store only
  fake.deliver(evt('jobs', 4, 'edge', job('x', 'service.restart', 'running'), { seq: 4 }));
  assert.equal(T.store.get('restarting'), true);
  fake.deliver(evt('jobs', 5, 'edge', job('x', 'service.restart', 'done', { result: { applied: true, core_running: true, core_cleanup_pending: false, core_generation: 5 } }), { seq: 5 }));
  assert.equal(T.store.get('restarting'), false);
  assert.equal(T.named('rt:restart').length, 2);
  assert.deepEqual(T.named('rt:job').map(j => j.instant), [false, false, false, true, true]);
});

test('tiers.summary row: carrier, order, fallback, degraded, tiers{ok,total,state}, pool; counts are the runtime\'s', async () => {
  const { T, fake } = await live(V1, []);
  fake.deliver(evt('tiers.summary', 1, 'snapshot', tierSummary({ tiers: {
    vanguard: { enabled: true, total: 3, usable: 3 }, sentinel: { enabled: true, total: 5, usable: 2 },
    bastion: { enabled: true, total: 40, usable: 0 }, gateway: { enabled: false, total: 1, usable: 1 } } })));
  const get = S(T);
  assert.equal(get('carrier'), 'vanguard');
  assert.deepEqual(get('order'), ['vanguard', 'sentinel', 'bastion', 'gateway']);
  assert.equal(get('fallback'), false);
  assert.equal(get('degraded'), false);
  assert.deepEqual(get('tiers').vanguard, { ok: 3, total: 3, enabled: true, state: 'ok' });
  assert.deepEqual(get('tiers').sentinel, { ok: 2, total: 5, enabled: true, state: 'partial' });
  assert.equal(get('tiers').bastion.state, 'down');
  assert.equal(get('tiers').gateway.state, 'off');
  assert.deepEqual(get('pool'), { ok: 4, total: 5 });
});

test('tier state derivation', () => {
  assert.equal(tierState({ enabled: false, total: 5, usable: 5 }), 'off');
  assert.equal(tierState({ enabled: true, total: 5, usable: 0 }), 'down');
  assert.equal(tierState({ enabled: true, total: 0, usable: 0 }), 'down');
  assert.equal(tierState({ enabled: true, total: 5, usable: 3 }), 'partial');
  assert.equal(tierState({ enabled: true, total: 5, usable: 5 }), 'ok');
  assert.equal(tierState(null), 'off');
});

test('rt:tier kinds from tier.transition (fallback / recover) and tiers.summary (degraded / recover)', async () => {
  const { T, fake } = await live(V1, [evt('tiers.summary', 1, 'snapshot', tierSummary())]);
  // first transition frame is instant: no notice
  fake.deliver(evt('tier.transition', 1, 'edge', { from: 'vanguard', to: 'vanguard', observed_at_ms: 1, authority: 'observed' }, { seq: 1 }));
  fake.deliver(evt('tiers.summary', 2, 'snapshot', tierSummary({ degraded: true })));
  fake.deliver(evt('tier.transition', 2, 'edge', { from: 'vanguard', to: 'sentinel', observed_at_ms: 2, authority: 'observed' }, { seq: 2 }));
  fake.deliver(evt('tiers.summary', 3, 'snapshot', tierSummary({ carrier: 'sentinel', fallback: true, degraded: true })));
  fake.deliver(evt('tier.transition', 3, 'edge', { from: 'sentinel', to: 'vanguard', observed_at_ms: 3, authority: 'observed' }, { seq: 3 }));
  fake.deliver(evt('tiers.summary', 4, 'snapshot', tierSummary()));
  fake.deliver(evt('tiers.summary', 5, 'snapshot', tierSummary({ degraded: true })));
  fake.deliver(evt('tiers.summary', 6, 'snapshot', tierSummary()));
  const kinds = T.named('rt:tier').map(e => [e.kind, e.carrier, e.wasFallback === undefined ? null : e.wasFallback, e.observed]);
  assert.deepEqual(kinds, [
    ['degraded', 'vanguard', null, true],
    ['fallback', 'sentinel', null, true],
    ['recover', 'vanguard', true, true],
    ['degraded', 'vanguard', null, true],
    ['recover', 'vanguard', false, true],
  ]);
});

test('without tier.transition in caps, fallback edges come from tiers.summary', async () => {
  const { T, fake } = await live({}, [evt('tiers.summary', 1, 'snapshot', tierSummary())]);
  fake.deliver(evt('tiers.summary', 2, 'snapshot', tierSummary({ carrier: 'sentinel', fallback: true })));
  fake.deliver(evt('tiers.summary', 3, 'snapshot', tierSummary()));
  assert.deepEqual(T.named('rt:tier').map(e => [e.kind, e.wasFallback === undefined ? null : e.wasFallback]), [['fallback', null], ['recover', true]]);
});

/* ---- tier level reconciliation (T-P4-H; 决定_第五波裁定 02b-1), in the runtime's shapes:
   tiers.summary.order has the carrier first and includes gateway (desktop_runtime
   tiers_summary_status), fallback iff the carrier is Sentinel or Bastion, carrier null with an
   offline_reason while the route is invalid or the service is stopped. ---- */
const ORDER0 = ['vanguard', 'sentinel', 'bastion', 'gateway'];
function rtSum(carrier, offline_reason = null) {
  const s = tierSummary({ carrier, fallback: carrier === 'sentinel' || carrier === 'bastion',
    order: carrier ? [carrier, ...ORDER0.filter(t => t !== carrier)] : ORDER0.slice() });
  return { ...s, offline_reason: carrier ? null : offline_reason };
}
async function tierRig(first = 'vanguard') {
  const b = await live(V1, [evt('tiers.summary', 1, 'snapshot', rtSum(first)),
    // the first tier.transition frame of a connect is instant (the shell's cached last edge)
    evt('tier.transition', 1, 'edge', { from: first, to: first, observed_at_ms: 1, authority: 'observed' }, { seq: 1 })]);
  let rev = 1, seq = 1;
  return {
    ...b,
    sum: (carrier, reason) => b.fake.deliver(evt('tiers.summary', ++rev, 'snapshot', rtSum(carrier, reason))),
    tr: (from, to) => { seq += 1; b.fake.deliver(evt('tier.transition', seq, 'edge', { from, to, observed_at_ms: seq, authority: 'observed' }, { seq })); },
    // a gap frame of tier.transition: the sidecar dropped one edge (PROTOCOL section 2, edge,gap)
    gap: () => { seq += 2; b.fake.deliver(evt('tier.transition', seq, 'gap', { from_seq: seq - 1, to_seq: seq - 1, dropped: 1 }, { seq })); },
    notices: () => b.T.named('rt:tier').map(e => [e.kind, e.carrier, e.from || null, e.wasFallback === undefined ? null : e.wasFallback, e.source, e.instant]),
  };
}

test('rt:tier, transition first: v→s, s→b, b→s, from===to, b→v, s→gateway, gateway→v, v→gateway; the summary that follows is deduplicated', async () => {
  const r = await tierRig();
  const step = (from, to) => { r.tr(from, to); r.sum(to); };
  step('vanguard', 'sentinel');
  assert.equal(r.T.store.get('order')[0], 'sentinel', 'runtime shape: carrier first');
  assert.ok(r.T.store.get('order').includes('gateway'));
  step('sentinel', 'bastion');
  step('bastion', 'sentinel');
  r.tr('sentinel', 'sentinel');
  step('sentinel', 'bastion');
  step('bastion', 'vanguard');
  step('vanguard', 'sentinel');
  step('sentinel', 'gateway');
  step('gateway', 'vanguard');
  step('vanguard', 'gateway');
  step('gateway', 'vanguard');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', null, 'transition', false],
    ['fallback', 'bastion', 'sentinel', null, 'transition', false],
    ['fallback', 'sentinel', 'bastion', null, 'transition', false],   // Bastion -> Sentinel: still fallback, with from
    ['fallback', 'bastion', 'sentinel', null, 'transition', false],
    ['recover', 'vanguard', 'bastion', true, 'transition', false],
    ['fallback', 'sentinel', 'vanguard', null, 'transition', false],
    ['recover', 'gateway', 'sentinel', true, 'transition', false],    // backup -> Gateway is a recovery too
  ]);
  assert.ok(r.T.named('rt:tier').every(e => e.observed === true));
});

test('rt:tier, summary first: the same changes announced by tiers.summary (observed, source summary); the late transition is deduplicated', async () => {
  const r = await tierRig();
  const step = (from, to) => { r.sum(to); r.tr(from, to); };
  step('vanguard', 'sentinel');
  step('sentinel', 'bastion');
  step('bastion', 'sentinel');
  r.tr('sentinel', 'sentinel');
  step('sentinel', 'bastion');
  step('bastion', 'vanguard');
  step('vanguard', 'sentinel');
  step('sentinel', 'gateway');
  step('gateway', 'vanguard');
  step('vanguard', 'gateway');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', null, 'summary', false],
    ['fallback', 'bastion', 'sentinel', null, 'summary', false],
    ['fallback', 'sentinel', 'bastion', null, 'summary', false],
    ['fallback', 'bastion', 'sentinel', null, 'summary', false],
    ['recover', 'vanguard', 'bastion', true, 'summary', false],
    ['fallback', 'sentinel', 'vanguard', null, 'summary', false],
    ['recover', 'gateway', 'sentinel', true, 'summary', false],
  ]);
  assert.equal(r.T.store.get('fallback'), false);
});

test('rt:tier: a summary sampled before the transition does not undo it; transition and summary interleaved give one notice per change', async () => {
  const r = await tierRig();
  r.tr('vanguard', 'sentinel');
  r.sum('vanguard');            // sampled before the switch, delivered after it
  r.sum('sentinel');
  r.sum('bastion');             // summary sees Bastion first
  r.tr('sentinel', 'bastion');
  r.tr('bastion', 'vanguard');
  r.sum('bastion');             // late
  r.sum('vanguard');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', null, 'transition', false],
    ['fallback', 'bastion', 'sentinel', null, 'summary', false],
    ['recover', 'vanguard', 'bastion', true, 'transition', false],
  ]);
});

test('rt:tier: recovery inside a route-invalid window is announced by the summary alone (no tier.transition across the window)', async () => {
  // bridge_tiers.transition drops its baseline while the route is invalid: the switch back
  // has no edge, only tiers.summary's falling level shows it
  const r = await tierRig();
  r.tr('vanguard', 'sentinel');
  r.sum('sentinel');
  r.sum(null, 'route_invalid');
  assert.equal(r.T.store.get('fallback'), false, 'the store follows the frame');
  assert.equal(r.T.named('rt:tier').length, 1, 'a frame without a carrier is no level');
  r.sum('vanguard');
  // a fallback that the summary never saw (shorter than one sample), then a window, then back
  r.tr('vanguard', 'bastion');
  r.sum(null, 'route_invalid');
  r.sum('vanguard');
  // a fallback that only the summary sees, after a window
  r.sum(null, 'route_invalid');
  r.sum('sentinel');
  r.sum(null, 'unmanaged_route');
  r.sum('gateway');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', null, 'transition', false],
    ['recover', 'vanguard', 'sentinel', true, 'summary', false],
    ['fallback', 'bastion', 'vanguard', null, 'transition', false],
    ['recover', 'vanguard', 'bastion', true, 'summary', false],
    ['fallback', 'sentinel', 'vanguard', null, 'summary', false],
    ['recover', 'gateway', 'sentinel', true, 'summary', false],
  ]);
});

test('rt:tier: a stop is not a recovery; after a reconnect the first summary brings an announced fallback up to date (instant)', async () => {
  const r = await tierRig();
  r.tr('vanguard', 'sentinel');
  r.sum('sentinel');
  r.sum(null, 'stopping');
  r.sum(null, 'stopped');
  r.sum('vanguard');            // started again on Vanguard: nothing was announced since the stop
  assert.deepEqual(r.notices(), [['fallback', 'sentinel', 'vanguard', null, 'transition', false]]);
  // fallback announced, then the Channel reconnects (same epoch) and the service recovered meanwhile
  r.tr('vanguard', 'bastion');
  r.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  r.fake.deliver(hello({ epoch: E1, ...V1.hello }));
  r.fake.deliver(evt('tiers.summary', 50, 'snapshot', rtSum('vanguard')));
  // nothing announced and an instant summary in fallback: no notice (the state is in the store)
  r.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  r.fake.deliver(hello({ epoch: E1, ...V1.hello }));
  r.fake.deliver(evt('tiers.summary', 51, 'snapshot', rtSum('sentinel')));
  r.fake.deliver(evt('tiers.summary', 52, 'snapshot', rtSum('vanguard')));
  assert.deepEqual(r.notices().slice(1), [
    ['fallback', 'bastion', 'vanguard', null, 'transition', false],
    ['recover', 'vanguard', 'bastion', true, 'summary', true],
    ['recover', 'vanguard', 'sentinel', true, 'summary', false],
  ]);
});

test('rt:tier (fix round 1, S5): a generation change keeps the announced level, so the first summary of the new epoch brings the banner up to date (instant)', async () => {
  const r = await tierRig();
  r.tr('vanguard', 'bastion');
  // the runtime is lost while in fallback; the new generation starts on Vanguard
  r.fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  r.fake.deliver(hello({ epoch: E2, ...V1.hello }));
  r.fake.deliver({ ...evt('tiers.summary', 1, 'snapshot', rtSum('vanguard')), epoch: E2 });
  // later frames of the new epoch are live again: a real fallback is announced as usual
  r.fake.deliver({ ...evt('tiers.summary', 2, 'snapshot', rtSum('sentinel')), epoch: E2 });
  assert.deepEqual(r.notices(), [
    ['fallback', 'bastion', 'vanguard', null, 'transition', false],
    ['recover', 'vanguard', 'bastion', true, 'summary', true],
    ['fallback', 'sentinel', 'vanguard', null, 'summary', false],
  ]);
  // nothing announced before the change: the new epoch's first summary in fallback stays silent
  const q = await tierRig();
  q.fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  q.fake.deliver(hello({ epoch: E2, ...V1.hello }));
  q.fake.deliver({ ...evt('tiers.summary', 1, 'snapshot', rtSum('sentinel')), epoch: E2 });
  assert.deepEqual(q.notices(), []);
});

test('rt:tier (fix round 1, S6): a gap in tier.transition makes the next summary reconcile by level (lost recovery edge)', async () => {
  const r = await tierRig();
  // a fallback shorter than the summary's sampling period: the summary never shows Sentinel,
  // and the edge back to Vanguard is lost in a gap
  r.tr('vanguard', 'sentinel');
  r.gap();
  assert.equal(r.T.named('rt:gap').filter(g => g.topic === 'tier.transition').length, 1, 'the page still sees rt:gap');
  r.sum('vanguard');
  // without a gap, a summary that stays on its own level is no edge (the transition rules)
  r.tr('vanguard', 'bastion');
  r.sum('vanguard');
  r.sum('vanguard');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', null, 'transition', false],
    ['recover', 'vanguard', 'sentinel', true, 'summary', false],
    ['fallback', 'bastion', 'vanguard', null, 'transition', false],
  ]);
  // a gap while the level is unchanged announces nothing
  const q = await tierRig();
  q.gap();
  q.sum('vanguard');
  assert.deepEqual(q.notices(), []);
});

test('service.collect row: complete -> collected; unknown stays unlit; rt:collect on each new tier', async () => {
  const row = (tier, first_check) => ({ tier, generation: 4, first_check, source_count: null, healthy: null, required: null, observed_at_ms: null });
  const collect = (...done) => ({ generation: 4, observed_at_ms: 1, tiers: ['vanguard', 'sentinel', 'bastion', 'gateway'].map(t => row(t, done.includes(t) ? 'complete' : 'unknown')) });
  const { T, fake } = await live(V1, [evt('service.collect', 1, 'snapshot', collect())]);
  assert.deepEqual(T.store.get('collected'), {});
  fake.deliver(evt('service.collect', 2, 'edge', collect('vanguard'), { seq: 1 }));
  fake.deliver(evt('service.collect', 3, 'edge', collect('vanguard', 'sentinel'), { seq: 2 }));
  assert.deepEqual(T.store.get('collected'), { vanguard: true, sentinel: true });
  assert.deepEqual(T.named('rt:collect').map(e => e.tier), ['vanguard', 'sentinel']);
});

test('network.mode row: mode, admin, systemProxy; modeBusy from switching or a mode.set job; rt:mode with the job target', async () => {
  const scenario = { steps: [{ on_invoke: { cmd: 'mode.set' }, reply: [{ t: 'ack', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, id: 2, accepted: true, op: 'm1' }] }] };
  const { T, rt, fake } = await live({ ...V1, scenario }, []);
  const mode = (m, extra = {}) => ({ mode: m, admin: true, switching: false, proxy_state: 'owned', drift: false, ...extra });
  fake.deliver(evt('network.mode', 1, 'snapshot', mode('system')));
  assert.equal(T.store.get('mode'), 'system');
  assert.equal(T.store.get('admin'), true);
  assert.deepEqual(T.store.get('systemProxy'), { proxy_state: 'owned', drift: false });
  assert.deepEqual(T.named('rt:mode'), [], 'nothing for the instant first frame');
  const p = rt.call('mode.set', { mode: 'tun' });
  await flush();
  fake.deliver(evt('jobs', 1, 'edge', job('m1', 'mode.set', 'queued'), { seq: 1 }));
  fake.deliver(evt('jobs', 2, 'edge', job('m1', 'mode.set', 'running'), { seq: 2 }));
  assert.equal(T.store.get('modeBusy'), true);
  assert.deepEqual(T.named('rt:mode').pop(), { mode: 'tun', busy: true, instant: false });
  fake.deliver(evt('network.mode', 2, 'edge', mode('system', { switching: true }), { seq: 1 }));
  fake.deliver(evt('network.mode', 3, 'edge', mode('tun', { switching: false }), { seq: 2 }));
  assert.equal(T.store.get('modeBusy'), true, 'the job is not terminal yet');
  fake.deliver(evt('jobs', 3, 'edge', job('m1', 'mode.set', 'done', { result: { mode: 'tun', changed: true, saved: true, restarted: true, applied: true, apply_error: null } }), { seq: 3 }));
  assert.equal(T.store.get('modeBusy'), false);
  assert.deepEqual(T.named('rt:mode').pop(), { mode: 'tun', busy: false, instant: false });
  assert.equal((await p).applied, true);
  // a mode change with no busy phase (stopped service) is still announced
  fake.deliver(evt('network.mode', 4, 'edge', mode('mixed', { proxy_state: 'other', drift: true }), { seq: 3 }));
  assert.deepEqual(T.named('rt:mode').pop(), { mode: 'mixed', busy: false, instant: false });
  assert.deepEqual(T.store.get('systemProxy'), { proxy_state: 'other', drift: true });
});

// PROTOCOL section 11 killswitch.state (T-P4-C1 item 5; 决定_第五波裁定 C1-#3): active /
// triggered = block rules installed and blocking; armed = enabled && admin && no error && not
// triggered. A frozen sidecar (no `triggered` field) is judged by `active`.
test('killswitch: triggered ?? active is triggered; armed = enabled, admin, no error, not triggered (new and frozen sidecar frames)', () => {
  // the new sidecar sends triggered (== active); the frozen one has no triggered field
  const fresh = (enabled, triggered, admin, extra = {}) => ({ enabled, active: triggered, admin, error: null, triggered, ...extra });
  const frozen = (enabled, active, admin, extra = {}) => ({ enabled, active, admin, error: null, ...extra });
  for (const [name, ks] of [['new', fresh], ['frozen', frozen]]) {
    assert.deepEqual(killswitchOf(ks(false, false, true), 'running'), { killswitch: 'disabled', ksEnabled: false, ksBlocked: null }, name);
    assert.equal(killswitchOf(ks(true, false, true, { error: { code: 'io' } }), 'running').killswitch, 'error', name);
    assert.equal(killswitchOf(ks(true, false, true), 'running').killswitch, 'armed', `${name}: enabled, admin, no error, not triggered`);
    assert.equal(killswitchOf(ks(true, true, true), 'running').killswitch, 'triggered', `${name}: rules in force`);
    assert.equal(killswitchOf(ks(true, true, true), 'stopped').killswitch, 'triggered', `${name}: blocking whatever the run state`);
    assert.equal(killswitchOf(ks(true, true, true), 'failed').killswitch, 'triggered', name);
    assert.equal(killswitchOf(ks(true, false, true), 'stopped').killswitch, 'inactive', name);
    assert.equal(killswitchOf(ks(true, false, true), 'starting').killswitch, 'inactive', name);
    assert.deepEqual(killswitchOf(ks(true, false, false), 'running'), { killswitch: 'blocked', ksEnabled: true, ksBlocked: 'admin' }, name);
    assert.equal(killswitchOf(ks(true, false, false), 'stopped').ksBlocked, 'admin', `${name}: blocked reason holds while stopped`);
    assert.equal(killswitchOf(ks(false, true, true), 'running').killswitch, 'disabled', `${name}: disabled wins`);
    assert.equal(killswitchOf(ks(true, true, true, { error: { code: 'io' } }), 'running').killswitch, 'error', `${name}: error wins`);
  }
  // the explicit field wins over active when both are present
  assert.equal(killswitchOf({ enabled: true, active: true, admin: true, error: null, triggered: false }, 'running').killswitch, 'armed');
  assert.equal(killswitchOf({ enabled: true, active: false, admin: true, error: null, triggered: true }, 'running').killswitch, 'triggered');
  // no source for a `state` field: it is ignored
  assert.equal(killswitchOf({ enabled: true, active: false, admin: true, error: null, state: 'triggered' }, 'running').killswitch, 'armed');
  assert.equal(killswitchOf(null, 'running'), null);
});

test('killswitch row follows run changes (new sidecar frame, then a frozen one)', async () => {
  const { T, fake } = await live(V1, [evt('killswitch.state', 1, 'snapshot', { enabled: true, active: false, admin: true, error: null, triggered: false })]);
  assert.equal(T.store.get('killswitch'), 'inactive', 'run unknown');
  fake.deliver(evt('service.state', 1, 'snapshot', svc()));
  assert.equal(T.store.get('killswitch'), 'armed');
  assert.equal(T.store.get('ksEnabled'), true);
  fake.deliver(evt('killswitch.state', 2, 'edge', { enabled: true, active: true, admin: true, error: null, triggered: true }, { seq: 1 }));
  assert.equal(T.store.get('killswitch'), 'triggered');
  fake.deliver(evt('service.state', 2, 'snapshot', svc({ phase: 'stopped' })));
  assert.equal(T.store.get('killswitch'), 'triggered', 'the rules still block while stopped');
  // a frozen sidecar: no triggered field, active is the fact
  fake.deliver(evt('killswitch.state', 3, 'edge', { enabled: true, active: false, admin: true, error: null }, { seq: 2 }));
  assert.equal(T.store.get('killswitch'), 'inactive');
  fake.deliver(evt('service.state', 3, 'snapshot', svc()));
  assert.equal(T.store.get('killswitch'), 'armed');
  fake.deliver(evt('killswitch.state', 4, 'edge', { enabled: true, active: true, admin: true, error: null }, { seq: 3 }));
  assert.equal(T.store.get('killswitch'), 'triggered');
});

test('bastion.pool row: renamed fields; enabled from tiers.summary', async () => {
  const { T, fake } = await live(V1, [evt('tiers.summary', 1, 'snapshot', tierSummary())]);
  fake.deliver(evt('bastion.pool', 1, 'snapshot', { runtime_alive: 12, user_alive: 1, capacity: 100, min_alive_nodes: 3, target_alive: 45, collecting: true, observed_at_ms: 5 }));
  assert.deepEqual(T.store.get('bastion'), { enabled: true, ok: 12, user: 1, capacity: 100, target: 45, min: 3, collecting: true, observed_at_ms: 5 });
});

test('exit.identity row: status -> prototype state, partial flag, previous kept as last result', () => {
  const leg = { status: 'ok', address: '203.0.113.1', country: 'JP', reason_code: null, observed_at_ms: 1 };
  const cur = (status, extra = {}) => ({ request_id: 'q1', generation: 4, binding_id: null, status, reason_code: null, v4: leg, v6: leg, fresh: true, observed_at_ms: 9,
    native_fallback_verified: false, native_fallback_verified_commit_rejected: false, native_fallback_unverified_budget: false, manual_execution_binding_stale: false, ...extra });
  const map = { running: 'pending', ok: 'ok', partial: 'ok', idle: 'none', failed: 'failed', cancelled: 'cancelled', superseded: 'superseded' };
  for (const [status, state] of Object.entries(map)) assert.equal(exitOf({ current: cur(status) }).state, state, status);
  const e = exitOf({ current: cur('running'), previous: cur('partial') });
  assert.equal(e.had, true);
  assert.equal(e.previous.status, 'partial');
  assert.equal(exitOf({ current: cur('partial') }).partial, true);
  assert.equal(exitOf({ current: cur('ok') }).how, 'auto');
  assert.equal(exitOf({ current: cur('ok') }, new Set(['q1'])).how, 'manual');
  assert.deepEqual(exitOf(null), { state: 'none', had: false });
});

test('update.state row: every v1 state maps to the footer state', () => {
  const u = (state, extra = {}) => ({ state, version: '0.2.0', update_available: false, installable: false, error: null, ...extra });
  assert.equal(updateStateOf(u('idle')), 'latest');
  assert.equal(updateStateOf(u('idle', { update_available: true })), 'found');
  assert.equal(updateStateOf(u('available', { update_available: true })), 'found');
  assert.equal(updateStateOf(u('checking')), 'checking');
  assert.equal(updateStateOf(u('downloading')), 'dl');
  assert.equal(updateStateOf(u('verified')), 'verify');
  assert.equal(updateStateOf(u('handoff')), 'ready');
  assert.equal(updateStateOf(u('failed', { error: { code: 'io' } })), 'fail');
  assert.equal(updateStateOf(u('idle', { error: { code: 'io' } })), 'fail');
});

test('update.state / protocols.mix / gateway.current / route.published / config.changed rows', async () => {
  const { T, fake } = await live(V1, []);
  fake.deliver(evt('update.state', 1, 'snapshot', { state: 'available', version: '0.2.0', update_available: true, installable: true, error: null }));
  assert.deepEqual(T.store.get('update'), { state: 'found', version: '0.1.0', latest: '0.2.0', available: true, installable: true, error: null, raw: 'available' });
  fake.deliver(evt('protocols.mix', 1, 'snapshot', { source: 'configured', counts: { vless: 3 }, observed_at_ms: 2 }));
  assert.deepEqual(T.store.get('protocols'), { source: 'configured', counts: { vless: 3 }, observed_at_ms: 2 });
  const gw = { selected: 'gw-1', effective: 'gw-1', missing: false, applied: true, pending: null };
  fake.deliver(evt('gateway.current', 1, 'snapshot', gw));
  assert.deepEqual(T.store.get('gateway'), gw);
  const route = { publication_revision: 1, owner: null, selected_chain: [], leaf: null, managed_tier: null, observed_at_ms: 1, valid: false, invalid_reason: 'no_published_route', fail_closed_confirmed: false };
  fake.deliver(evt('route.published', 1, 'snapshot', route));
  assert.deepEqual(T.store.get('route'), route);
  fake.deliver(evt('config.changed', 1, 'edge', { rev: 7, files: ['config.yaml'], sections: [], pending: null, external_detection: 'bridge_writes_only' }, { seq: 1 }));
  assert.equal(T.store.get('configRev'), 7);
  assert.equal(T.named('rt:config')[0].rev, 7);
});

test('traffic.tick / traffic.history rows: traffic, the live series (300 points) and the 24 h buckets', async () => {
  const { T, fake } = await live(V1, []);
  fake.deliver(evt('traffic.history', 1, 'snapshot', { sampled_at_ms: 1, source: 'temporary_snapshot',
    h24: [{ t_ms: 10, down_bps: 5, up_bps: 1, sample_count: 3 }], live: [{ t_ms: 11, down_bps: 6, up_bps: 2 }] }));
  const hist = T.store.get('history');
  assert.equal(hist.placeholder, false);
  assert.deepEqual(hist.h24, [{ t: 10, d: 5, u: 1, n: 3 }]);
  assert.deepEqual(hist.live, [{ t: 11, d: 6, u: 2 }]);
  for (let i = 1; i <= 305; i++) {
    fake.deliver(evt('traffic.tick', i, 'snapshot', { t_ms: 100 + i, up_bps: 1, down_bps: i, session: { up_bytes: 10, down_bytes: 20 }, running: true, error_code: null }));
  }
  assert.deepEqual(T.store.get('traffic'), { down: 20, up: 10, rateDown: 305, rateUp: 1 });
  assert.equal(hist.live.length, 300);
  assert.equal(hist.live[299].t, 405);
  const ticks = T.named('rt:tick');
  assert.deepEqual(ticks[1].sample, { t: 102, d: 2, u: 1 });
  // a gap:true history replaces everything
  fake.deliver(evt('traffic.history', 2, 'snapshot', { sampled_at_ms: 2, source: 'bucketed', h24: [], live: [], gap: true }));
  assert.equal(hist.live.length, 0);
  assert.equal(hist.gap, true);
  assert.equal(T.named('rt:history').pop().gap, true);
});

test('hello row: caps, lang, recovery, appVersion; ui.language is never projected', async () => {
  const { T, fake } = await live(V1, []);
  assert.equal(T.store.get('lang'), 'zh_CN');
  assert.equal(T.store.get('recovery').proxy.state, 'not_needed');
  assert.equal(T.store.get('appVersion'), '0.1.0');
  assert.deepEqual(T.store.get('caps').commands, V1.hello.commands);
  fake.deliver(evt('ui.language', 1, 'snapshot', { language: 'ru_RU', config_rev: 3 }));
  assert.equal(T.store.get('lang'), 'zh_CN');
  fake.deliver({ t: 'host:language', language: 'ja_JP', epoch: E1 });
  assert.equal(T.store.get('lang'), 'ja_JP');
});

test('the p2 projection scenario replays cleanly and fills every P2 store key', async () => {
  const { T, fake } = boot({ scenario: loadScenario('rt_p2_projection') });
  await flush();
  assert.deepEqual(fake.failures, []);
  for (const k of ['mode', 'killswitch', 'bastion', 'exit', 'collected', 'update', 'protocols', 'gateway', 'configRev']) {
    assert.notEqual(T.store.get(k), undefined, k);
  }
});

test('the start scenario: page call -> ack -> jobs -> done, run moves to running', async () => {
  const { T, rt, fake } = boot({ scenario: loadScenario('rt_start_job') });
  await flush();
  assert.equal(T.store.get('run'), 'stopped');
  const r = await rt.call('service.start');
  assert.deepEqual(r, { startup_phase: 'running', failure_code: null });
  assert.equal(T.store.get('run'), 'running');
  assert.deepEqual(T.named('rt:state').map(s => s.run), ['stopped', 'starting', 'running']);
  assert.deepEqual(fake.failures, []);
});

test('validate_ui_fixtures rejects a page scenario carrying ui.language or a shell-only command', () => {
  const dir = mkdtempSync(join(tmpdir(), 'rt-fixture-'));
  try {
    const h = hello();
    const lang = { name: 'bad', steps: [{ deliver: h }, { deliver: { ...evt('ui.language', 1, 'snapshot', { language: 'en_US', config_rev: 1 }) } }] };
    const desk = { name: 'bad2', steps: [{ deliver: h }, { deliver: { t: 'batch', epoch: E1, produced_at_ms: 1, sent_at_ms: 1, tick_seq: 1,
      items: [evt('alerts.desktop', 1, 'edge', { delivery_id: 'd', event_ids: [], severity: 'info', title: 't', detail: 'd', members: [], target: 'overview', version: null }, { seq: 1 })] } }] };
    const quit = { name: 'bad3', steps: [{ on_invoke: { cmd: 'app.quit' } }] };
    const shutdown = { name: 'bad4', steps: [{ deliver: evt('shutdown', 1, 'snapshot', { phase: 'draining', recovery: { proxy: 'unknown', firewall: 'unknown' }, workers_pending: false }) }] };
    const files = { 'lang.json': lang, 'desk.json': desk, 'quit.json': quit, 'shutdown.json': shutdown };
    for (const [name, doc] of Object.entries(files)) writeFileSync(join(dir, name), JSON.stringify(doc));
    for (const name of Object.keys(files)) {
      const r = python(['ui/tools/validate_ui_fixtures.py', join(dir, name)]);
      assert.equal(r.status, 1, name + ': ' + r.stdout + r.stderr);
      assert.match(r.stderr, /shell_only_(topic|command)/, name);
    }
    // the same frame shapes without the shell-only items pass
    writeFileSync(join(dir, 'ok.json'), JSON.stringify({ name: 'ok', steps: [{ deliver: h }, { deliver: evt('service.state', 1, 'snapshot', svc()) }] }));
    const ok = python(['ui/tools/validate_ui_fixtures.py', join(dir, 'ok.json')]);
    assert.equal(ok.status, 0, ok.stdout + ok.stderr);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
