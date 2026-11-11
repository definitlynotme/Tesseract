// T-P4-H2 (8th-wave ruling): the rt layer's part.
//   item 1  the shell's rt:stale frame is forwarded as the page event rt:slot-stale (PROTOCOL
//           section 13 rt:stale), epoch-filtered; shellc's list-stale banner gets it end to end
//   item 3  entering Gateway takes a fallback banner down without "recovered" (quiet recover);
//           a fallback that starts on Gateway names no Gateway as its origin; both arrival
//           orders (tier.transition first / tiers.summary first)
//   item 3  the overview's Gateway row: "current" only while running with an effective Gateway;
//           a selection that is not in force is "selected"
//   item 4  T.rt.requestResync: the page's way to ask for one slot's snapshot again
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gatewayRow } from '../../src/rt/project_overview.js';
import { HOST_INVOKE_NAMES, PAGE_RESYNC_GAP_MS } from '../../src/rt/client.js';
import { HOST_INVOKES, installShellc } from '../../src/shellc/index.js';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { E1, E2, boot, evt, flush, hello, live, loadScenario, python, tierSummary, UI } from './rt_helpers.mjs';

const ALL = { hello: { topics: ['service.state', 'route.published', 'tiers.summary', 'tier.transition', 'gateway.current', 'nodes.list', 'subscriptions.list'],
  commands: ['service.start', 'service.stop'] } };
const CAPS = (invokes = ['rt', 'rt_connect', 'rt_resync', 'host_ready']) => ({ t: 'host:caps', epoch: E1, invokes, events: ['rt:replayed', 'rt:stale'] });

/* ---- item 1: rt:stale -> rt:slot-stale ---------------------------------------------------- */
test('rt:stale is forwarded as rt:slot-stale with its payload as it is (key null, exhausted too); another epoch is dropped', async () => {
  const { T, rt, fake } = await live(ALL);
  fake.deliver({ t: 'rt:stale', epoch: E1, topic: 'nodes.list', key: 'sentinel', reason: 'resync' });
  fake.deliver({ t: 'rt:stale', epoch: E1, topic: 'config.items', key: null, reason: 'exhausted' });
  fake.deliver({ t: 'rt:stale', epoch: E1, topic: 'bastion.sources', reason: 'resync' });
  fake.deliver({ t: 'rt:stale', epoch: E2, topic: 'nodes.list', key: 'vanguard', reason: 'resync' });
  assert.deepEqual(T.named('rt:slot-stale'), [
    { epoch: E1, topic: 'nodes.list', key: 'sentinel', reason: 'resync' },
    { epoch: E1, topic: 'config.items', key: null, reason: 'exhausted' },
    { epoch: E1, topic: 'bastion.sources', key: null, reason: 'resync' },
  ]);
  const d = rt.diag();
  assert.ok(d.some(x => x.kind === 'epoch_mismatch' && x.t === 'rt:stale'), 'the other epoch is noted');
  assert.ok(!d.some(x => x.kind === 'unknown_frame'), 'no longer an unknown frame');
  // the page-internal rt:stale of stale.js ({topic, stale}) is another event and was not emitted for these
  assert.ok(!T.named('rt:stale').some(e => e && e.reason));
});

test('rt:stale end to end: the shell layer shows "may be out of date", Reload asks rt_resync per slot, the fresh list takes it down', async () => {
  const b = boot({ scenario: { steps: [], allow_host: ['host_notify_ready', 'host_set_page', 'host_ready', 'rt_resync'] } });
  const { T, fake } = b;
  const shown = new Map();
  T.notify = o => { if (o.id) shown.set(o.id, o); return { el: {}, dismiss() {} }; };
  T.dismiss = id => shown.delete(id);
  T.router = { current: null };
  T.t = (id, p) => (p ? `${id}${JSON.stringify(p)}` : id);
  T.i18n = { lang: 'zh_CN', set: () => Promise.resolve() };
  T.ui = {};
  installShellc(T, { win: null, doc: null, storage: null });
  await flush();
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  fake.deliver(CAPS());
  fake.deliver(hello({ ...ALL.hello }));
  fake.deliver(evt('nodes.list', 1, 'snapshot', { items: [] }, { key: 'sentinel' }));
  fake.deliver({ t: 'rt:replayed', epoch: E1 });
  fake.deliver({ t: 'rt:stale', epoch: E1, topic: 'nodes.list', key: 'sentinel', reason: 'exhausted' });
  const banner = shown.get('list-stale');
  assert.ok(banner, 'the list-stale banner is up');
  assert.equal(T.shellc.listStale('nodes.list', 'sentinel'), 'exhausted');
  banner.actions[0].run({});
  // fix round 1 (SHOULD): Reload and T.shellc.reload go through T.rt.requestResync, so a second
  // click (or a page's reload) inside the shell's 1 s rate limit is not sent again
  banner.actions[0].run({});
  assert.equal(await T.shellc.reload('nodes.list', 'sentinel'), null);
  await flush();
  assert.deepEqual(fake.invocations.filter(i => i.name === 'rt_resync').map(i => i.args), [{ topic: 'nodes.list', key: 'sentinel' }]);
  assert.ok(T.rt.diag().some(d => d.kind === 'resync_throttled'), 'the repeats are throttled by the rt layer');
  fake.deliver(evt('nodes.list', 2, 'snapshot', { items: [] }, { key: 'sentinel' }));
  assert.equal(shown.has('list-stale'), false, 'a complete value of the slot takes it down');
  assert.equal(T.shellc.listStale('nodes.list', 'sentinel'), null);
});

test('T.rt.invokeHost refuses a name outside PROTOCOL section 13 (the same list as shellc HOST_INVOKES; fix round 1)', async () => {
  assert.deepEqual([...HOST_INVOKE_NAMES].sort(), [...HOST_INVOKES].sort());
  const { rt, fake } = await live(ALL);
  const before = fake.invocations.length;
  await assert.rejects(rt.invokeHost('host_open_anything', {}), e => e.code === 'not_allowed');
  assert.equal(fake.invocations.length, before, 'nothing reaches the shell');
  assert.ok(rt.diag().some(d => d.kind === 'host_unknown' && d.name === 'host_open_anything'));
});

/* ---- item 4: T.rt.requestResync -------------------------------------------------------------- */
test('T.rt.requestResync: rt_resync when the shell has it (never a reconnect), once per slot per second, not while the slot\'s own resync is in flight', async () => {
  const { rt, fake, clock } = await live({ ...ALL, scenario: { steps: [
    { on_invoke: { host: 'rt_resync', args: { topic: 'nodes.list', key: 'sentinel' } }, result: { source: 'cache' } },
    { on_invoke: { host: 'rt_resync', args: { topic: 'subscriptions.list', key: 'sentinel' } }, reject: 'rate_limited' },
    { on_invoke: { host: 'rt_resync', args: { topic: 'nodes.list', key: 'sentinel' } }, result: { source: 'request' } },
  ] } });
  const hostCalls = () => fake.invocations.filter(i => i.kind === 'host' && i.name === 'rt_resync').map(i => i.args);
  // no host:caps: the shell cannot be asked; nothing is sent, nothing reconnects
  const reconnects = () => rt.diag().filter(d => d.kind === 'reconnect' || d.kind === 'resync_reconnect').length;
  assert.equal(await rt.requestResync('nodes.list', 'sentinel'), null);
  assert.deepEqual(hostCalls(), []);
  assert.equal(reconnects(), 0);
  assert.ok(rt.diag().some(d => d.kind === 'resync_unsupported'));
  fake.deliver(CAPS());
  assert.deepEqual(await rt.requestResync('nodes.list', 'sentinel'), { source: 'cache' });
  assert.equal(await rt.requestResync('subscriptions.list', 'sentinel'), null, 'a refusal resolves null (never rejects)');
  assert.ok(rt.diag().some(d => d.kind === 'resync' && d.code === 'rate_limited' && d.by === 'page'));
  assert.equal(await rt.requestResync('nodes.list', 'sentinel'), null, 'inside the shell\'s 1 s rate limit: not sent');
  assert.ok(rt.diag().some(d => d.kind === 'resync_throttled'));
  clock.advance(PAGE_RESYNC_GAP_MS);
  assert.deepEqual(await rt.requestResync('nodes.list', 'sentinel'), { source: 'request' });
  assert.deepEqual(hostCalls(), [{ topic: 'nodes.list', key: 'sentinel' }, { topic: 'subscriptions.list', key: 'sentinel' }, { topic: 'nodes.list', key: 'sentinel' }]);
  assert.equal(reconnects(), 0);
  assert.deepEqual(fake.failures, []);
});

test('T.rt.requestResync: a slot whose own mismatch already asked the shell is not asked again', async () => {
  const { rt, fake } = await live({ ...ALL, scenario: { steps: [], allow_host: ['rt_resync'] } });
  fake.deliver(CAPS());
  fake.deliver(evt('nodes.list', 3, 'snapshot', { items: [{ id: 'a' }] }, { key: 'vanguard' }));
  // a patch against an unknown base: the client asks rt_resync itself
  fake.deliver(evt('nodes.list', 5, 'patch', { changed: [], removed: ['a'] }, { key: 'vanguard', base_rev: 4 }));
  await flush();
  const n = () => fake.invocations.filter(i => i.name === 'rt_resync').length;
  assert.equal(n(), 1);
  assert.equal(await rt.requestResync('nodes.list', 'vanguard'), null);
  assert.equal(n(), 1);
  assert.ok(rt.diag().some(d => d.kind === 'resync_inflight'));
  // a generation change forgets every page request
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });
  fake.deliver(hello({ epoch: E2, ...ALL.hello }));
  fake.deliver({ ...CAPS(), epoch: E2 });
  await rt.requestResync('nodes.list', 'vanguard');
  assert.equal(n(), 2);
});

/* ---- item 3: Gateway in the tier level reconciliation -------------------------------------- */
const ORDER0 = ['vanguard', 'sentinel', 'bastion', 'gateway'];
function rtSum(carrier) {
  return { ...tierSummary({ carrier, fallback: carrier === 'sentinel' || carrier === 'bastion', order: [carrier, ...ORDER0.filter(t => t !== carrier)] }), offline_reason: null };
}
async function rig(first = 'vanguard') {
  const b = await live(ALL, [evt('tiers.summary', 1, 'snapshot', rtSum(first)),
    evt('tier.transition', 1, 'edge', { from: first, to: first, observed_at_ms: 1, authority: 'observed' }, { seq: 1 })]);
  let rev = 1, seq = 1;
  return {
    ...b,
    sum: c => b.fake.deliver(evt('tiers.summary', ++rev, 'snapshot', rtSum(c))),
    tr: (from, to) => { seq += 1; b.fake.deliver(evt('tier.transition', seq, 'edge', { from, to, observed_at_ms: seq, authority: 'observed' }, { seq })); },
    notices: () => b.T.named('rt:tier').map(e => [e.kind, e.carrier, e.from || null, e.quiet === true, e.source]),
  };
}

test('Gateway, transition first: backup -> Gateway is a quiet recover (the banner goes, no "recovered"); Gateway -> backup is a fallback with no Gateway origin', async () => {
  const r = await rig();
  const step = (from, to) => { r.tr(from, to); r.sum(to); };
  step('vanguard', 'sentinel');
  step('sentinel', 'gateway');     // the user chose a Gateway: FINAL-EXIT no longer goes through PROXY-FALLBACK
  step('gateway', 'bastion');      // the Gateway was cancelled and PROXY-FALLBACK sits on Bastion
  step('bastion', 'gateway');
  step('gateway', 'vanguard');     // no level change
  step('vanguard', 'gateway');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', false, 'transition'],
    ['recover', 'gateway', 'sentinel', true, 'transition'],
    ['fallback', 'bastion', null, false, 'transition'],
    ['recover', 'gateway', 'bastion', true, 'transition'],
  ]);
  assert.ok(r.T.named('rt:tier').filter(e => e.kind === 'recover').every(e => e.wasFallback === true));
});

test('Gateway, summary first: the same notices from tiers.summary; the late transition adds nothing', async () => {
  const r = await rig();
  const step = (from, to) => { r.sum(to); r.tr(from, to); };
  step('vanguard', 'sentinel');
  step('sentinel', 'gateway');
  step('gateway', 'bastion');
  step('bastion', 'gateway');
  step('gateway', 'vanguard');
  step('vanguard', 'gateway');
  assert.deepEqual(r.notices(), [
    ['fallback', 'sentinel', 'vanguard', false, 'summary'],
    ['recover', 'gateway', 'sentinel', true, 'summary'],
    ['fallback', 'bastion', null, false, 'summary'],
    ['recover', 'gateway', 'bastion', true, 'summary'],
  ]);
});

test('Gateway after a gap or a reconnect: the reconciled level follows the same rules (quiet into Gateway, no Gateway origin)', async () => {
  const r = await rig('gateway');
  // a fallback that only the summary sees, after a route-invalid window, starting on Gateway
  r.fake.deliver(evt('tiers.summary', 20, 'snapshot', { ...rtSum('vanguard'), carrier: null, offline_reason: 'route_invalid' }));
  r.fake.deliver(evt('tiers.summary', 21, 'snapshot', rtSum('sentinel')));
  // an instant summary after a reconnect, now on Gateway: brings the announced fallback down quietly
  r.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  r.fake.deliver(hello({ epoch: E1, ...ALL.hello }));
  r.fake.deliver(evt('tiers.summary', 22, 'snapshot', rtSum('gateway')));
  assert.deepEqual(r.T.named('rt:tier').map(e => [e.kind, e.carrier, e.from || null, e.quiet === true, e.instant]), [
    ['fallback', 'sentinel', null, false, false],
    ['recover', 'gateway', 'sentinel', true, true],
  ]);
});

test('degraded / latency recover only within one carrier: Vanguard(degraded) <-> Gateway(healthy) says neither "degraded" nor "recovered" (fix round 1, M1)', async () => {
  const r = await rig();
  const sumD = (c, degraded) => r.fake.deliver(evt('tiers.summary', ++r.rev, 'snapshot', { ...rtSum(c), degraded }));
  r.rev = 1;
  sumD('vanguard', true);    // same carrier, level rises: degraded
  sumD('gateway', false);    // the user chose a Gateway: no "Gateway's latency recovered"
  sumD('gateway', true);     // same carrier: degraded on Gateway
  sumD('vanguard', false);   // the Gateway was cancelled, back to a healthy Vanguard: no "Vanguard recovered"
  sumD('vanguard', true);
  sumD('vanguard', false);   // same carrier, level falls: the latency recover
  // a carrier-less frame (route invalid) and the frame after it are no edge either
  r.fake.deliver(evt('tiers.summary', ++r.rev, 'snapshot', { ...rtSum('vanguard'), carrier: null, offline_reason: 'route_invalid', degraded: true }));
  sumD('vanguard', false);
  const kinds = r.T.named('rt:tier').map(e => [e.kind, e.carrier, e.wasFallback === undefined ? null : e.wasFallback]);
  assert.deepEqual(kinds, [
    ['degraded', 'vanguard', null],
    ['degraded', 'gateway', null],
    ['degraded', 'vanguard', null],
    ['recover', 'vanguard', false],
  ]);
});

test('degraded / latency recover across a carrier switch, the other direction alone: Gateway(degraded) -> Vanguard(healthy) is no recover', async () => {
  const r = await rig('gateway');
  r.fake.deliver(evt('tiers.summary', 2, 'snapshot', { ...rtSum('gateway'), degraded: true }));
  r.fake.deliver(evt('tiers.summary', 3, 'snapshot', { ...rtSum('vanguard'), degraded: false }));
  r.fake.deliver(evt('tiers.summary', 4, 'snapshot', { ...rtSum('gateway'), degraded: true }));
  assert.deepEqual(r.T.named('rt:tier').map(e => [e.kind, e.carrier]), [['degraded', 'gateway']]);
  assert.ok(!r.T.named('rt:tier').some(e => e.kind === 'recover'));
});

/* ---- item 3: the Gateway row ----------------------------------------------------------------- */
test('gatewayRow: "current" only while running with an effective Gateway; selected but not in force is "selected"', () => {
  const tiers = { gateway: { ok: 3, total: 3, enabled: true } };
  const row = gw => gatewayRow({ run: 'running', tiers, collected: { gateway: true }, phase: 2, gateway: gw });
  assert.deepEqual([row({ selected: 'GW-Tokyo', effective: 'GW-Tokyo', missing: false, applied: true }).kind, row({ selected: 'GW-Tokyo', effective: 'GW-Tokyo', missing: false, applied: true }).name], ['current', 'GW-Tokyo']);
  // a route-invalid window (bridge_gateway_status clears effective / missing / applied) or the
  // switch-over before the chain goes through the Gateway: selected, never "current"
  const sel = row({ selected: 'GW-Tokyo', effective: null, missing: false, applied: false, pending: null });
  assert.deepEqual([sel.kind, sel.name, sel.ok, sel.total], ['selected', 'GW-Tokyo', 3, 3]);
  assert.equal(row({ selected: 'GW-Tokyo', effective: null, missing: false, applied: true }).kind, 'selected');
  // the other states keep their precedence
  assert.equal(row({ selected: 'GW-Tokyo', effective: null, missing: true, applied: false }).kind, 'missing');
  assert.equal(row({ selected: 'GW-2', effective: null, missing: false, applied: false, pending: { selected: 'GW-2' } }).kind, 'pending');
  assert.equal(row({ selected: null, effective: null }).kind, 'off');
  assert.equal(row(null).kind, 'unknown');
  // not running: the saved selection is shown as before (stopped), never "current"
  assert.equal(gatewayRow({ run: 'stopped', tiers, gateway: { selected: 'GW-Tokyo', effective: null, missing: false, applied: false } }).kind, 'stopped');
});

/* ---- item 3: the runtime invariants in tools/validate_ui_fixtures.py -------------------------- */
test('validate_ui_fixtures.py: the five runtime invariants and the rt:stale shape (a scenario that breaks each one fails)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'rt-h2-inv-'));
  try {
    const sc = loadScenario('overview_gateway_carrier');
    const at = topic => sc.steps.find(x => x.deliver && x.deliver.topic === topic).deliver.data;
    const bad = (name, fn) => { const c = JSON.parse(JSON.stringify(sc)); fn(c, topic => c.steps.find(x => x.deliver && x.deliver.topic === topic).deliver.data); return [name, c]; };
    assert.equal(at('tiers.summary').carrier, 'gateway');
    // the shell's replay on a reconnect: its cached frames of the three topics, in the order
    // gateway.current, route.published, tiers.summary (fn edits the replayed tiers.summary)
    const replay = (c, fn = () => {}) => {
      const fr = topic => JSON.parse(JSON.stringify(c.steps.find(x => x.deliver && x.deliver.topic === topic).deliver));
      const ep = fr('tiers.summary').epoch;
      const ts = fr('tiers.summary'); fn(ts.data);
      return [{ t: 'rt:reset', epoch: ep, reason: 'reconnect' }, fr('gateway.current'), fr('route.published'), ts, { t: 'rt:replayed', epoch: ep }];
    };
    const cases = [
      ['ok', sc],
      bad('effective_needs_gateway', (c, d) => { Object.assign(d('tiers.summary'), { carrier: 'vanguard', order: ['vanguard', 'sentinel', 'bastion', 'gateway'] }); }),
      bad('managed_tier', (c, d) => { d('route.published').managed_tier = 'vanguard'; }),
      bad('order', (c, d) => { d('tiers.summary').order = ['vanguard', 'gateway', 'sentinel', 'bastion']; }),
      bad('fallback', (c, d) => { d('tiers.summary').fallback = true; }),
      bad('leaf', (c, d) => { d('route.published').leaf = 'GW-other'; }),
      // fix round 1: the replay segment is checked as one unit (at rt:replayed)
      bad('replay_ok', c => { c.replay = replay(c); }),
      bad('replay_bad', c => { c.replay = replay(c, s => Object.assign(s, { carrier: 'vanguard', order: ['vanguard', 'sentinel', 'bastion', 'gateway'] })); }),
      ['stale', { steps: [{ deliver: { t: 'rt:stale', epoch: 'e', topic: 'nodes.list', key: null, reason: 'gap' } }] }],
      ['stale_ok', { steps: [{ deliver: { t: 'rt:stale', epoch: 'e', topic: 'nodes.list', key: 'gateway', reason: 'exhausted' } },
        { deliver: { t: 'rt:stale', epoch: 'e', topic: 'config.items', key: null, reason: 'resync' } }] }],
    ];
    const out = {};
    for (const [name, doc] of cases) {
      const f = join(dir, `${name}.json`);
      writeFileSync(f, JSON.stringify(doc), 'utf8');
      const r = python([join(UI, 'tools', 'validate_ui_fixtures.py'), f]);
      out[name] = { status: r.status, text: (r.stdout || '') + (r.stderr || '') };
    }
    assert.equal(out.ok.status, 0, out.ok.text);
    assert.equal(out.stale_ok.status, 0, out.stale_ok.text);
    assert.equal(out.replay_ok.status, 0, out.replay_ok.text);
    assert.equal(out.replay_bad.status, 1, out.replay_bad.text);
    assert.ok(/replay\[4\].*invariant:gateway_effective_needs_carrier_gateway/.test(out.replay_bad.text), out.replay_bad.text);
    const want = { effective_needs_gateway: 'invariant:gateway_effective_needs_carrier_gateway', managed_tier: 'invariant:carrier_is_managed_tier',
      order: 'invariant:order_starts_with_carrier', fallback: 'invariant:fallback_iff_backup_carrier', leaf: 'invariant:gateway_effective_is_leaf',
      stale: 'enum:reason' };
    for (const [name, needle] of Object.entries(want)) {
      assert.equal(out[name].status, 1, name);
      assert.ok(out[name].text.includes(needle), `${name}: ${out[name].text}`);
    }
    assert.ok(out.stale.text.includes('missing:key'), out.stale.text);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
