// T-P4-02b: the design target's simulator behind FakeTransport (sim mode). The real design
// boot (boot/data-design.js + boot/rt-design.js, with data/static.js, design/*.js and rt/*.js)
// is bundled with esbuild and run on mocked timers in a page-less window. Every frame the
// simulator sends goes through the same Python oracle as the scenario fixtures
// (tools/validate_ui_fixtures.py); the store sequence is compared with the prototype's
// (T-P4-01) recorded baseline; the command / query mapping and static.js are checked.
import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as esbuild from 'esbuild';
import { defineFor } from '../../build.mjs';
import { UI, python } from './rt_helpers.mjs';

const T0 = 1790812800000; // 2026-09-01T00:00:00Z
const KB = 1024, MB = KB * KB;

/* ---- a page-less window with the two core.js primitives the boot needs ---- */
function makeWindowT() {
  const bus = new Map(), state = Object.create(null), subs = new Map();
  const T = { events: [] };
  T.on = (e, fn) => { let s = bus.get(e); if (!s) bus.set(e, s = new Set()); s.add(fn); return () => s.delete(fn); };
  T.emit = (e, p) => { T.events.push([e, p]); const s = bus.get(e); if (s) for (const fn of [...s]) fn(p); };
  T.store = {
    get: k => state[k],
    set(k, v) { const prev = state[k]; if (Object.is(prev, v)) return; state[k] = v; const s = subs.get(k); if (s) for (const fn of [...s]) fn(v, prev); },
    patch(o) { for (const k in o) T.store.set(k, o[k]); },
    update(k, fn) { T.store.set(k, fn(state[k])); },
    sub(k, fn) { let s = subs.get(k); if (!s) subs.set(k, s = new Set()); s.add(fn); return () => s.delete(fn); },
    snapshot: () => Object.assign({}, state),
  };
  T.util = {
    clamp: (v, a, b) => Math.max(a, Math.min(b, v)),
    rng(seed) { // core.js T.util.rng
      let a = seed >>> 0;
      return function () { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
    },
  };
  T.fmt = { KB, MB, time: ts => new Date(ts).toISOString().slice(11, 19), rate: n => `${(n / MB).toFixed(2)} MB/s`, bytes: n => `${(n / MB).toFixed(2)} MB` };
  T.named = name => T.events.filter(([e]) => e === name).map(([, p]) => p);
  return T;
}

/* ---- the real design boot, bundled; FakeTransport.push is tapped to record every frame ---- */
const bundles = new Map();
async function bundle(entry, target = 'design') {
  const key = target + '\0' + entry;
  if (bundles.has(key)) return bundles.get(key);
  const tap = {
    name: 'tap-fake-transport',
    setup(b) {
      b.onLoad({ filter: /transport-fake\.js$/ }, args => ({
        contents: readFileSync(args.path, 'utf8') + `
const __push = FakeTransport.prototype.push;
FakeTransport.prototype.push = function (gen, frame, order) { (globalThis.__designFrames || (globalThis.__designFrames = [])).push(JSON.parse(JSON.stringify(frame))); return __push.call(this, gen, frame, order); };
`,
        loader: 'js',
      }));
    },
  };
  const r = await esbuild.build({ absWorkingDir: UI, stdin: { contents: entry, resolveDir: join(UI, 'src', 'boot'), loader: 'js' }, bundle: true, write: false,
    format: 'iife', target: 'chrome120', logLevel: 'silent', define: defineFor(target, []), plugins: [tap], metafile: true });
  const out = { code: r.outputFiles[0].text, inputs: Object.keys(r.metafile.inputs).map(p => p.replace(/\\/g, '/')) };
  bundles.set(key, out);
  return out;
}
const DESIGN_BOOT = "import './data-design.js';\nimport './rt-design.js';\n";

const drain = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

async function bootDesign({ search = '' } = {}) {
  globalThis.__designFrames = [];
  const { code } = await bundle(DESIGN_BOOT);
  const T = makeWindowT();
  new Function('window', 'location', code)({ T }, { search });
  await drain();
  return T;
}
async function advance(ms, step = 100) {
  for (let t = 0; t < ms; t += step) { mock.timers.tick(Math.min(step, ms - t)); await drain(); }
}
function withTimers(fn) {
  return async () => {
    mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: T0 });
    try {
      await fn();
      // T-P4-H fix round 1 (review S7-2): the common tail of every simulator case checks all the
      // frames the simulator sent (demo scenarios included), not only the ones a case inspects.
      assertCatalogKeys(sentFrames());
    } finally { mock.timers.reset(); globalThis.__designFrames = []; }
  };
}

function validate(frames) {
  const dir = mkdtempSync(join(tmpdir(), 'design-sim-'));
  try {
    const file = join(dir, 'design_sim_frames.json');
    writeFileSync(file, JSON.stringify({ frames }), 'utf8');
    const r = python([join(UI, 'tools', 'validate_ui_fixtures.py'), file]);
    return { status: r.status, out: (r.stdout || '') + (r.stderr || '') };
  } finally { rmSync(dir, { recursive: true, force: true }); }
}

/* ---- the scenario both sides run (ms after boot): start → collect → ready → fallback →
   recover → stop, mode switches, a restart, manual checks, page-written settings ---- */
const SCRIPT = [
  [1000, T => T.sim.verifyExit()],
  [4000, T => T.sim.setMode('tun')],
  [8000, T => T.sim.restart()],
  [13000, T => T.sim.scenario('fallback')],
  [22000, T => T.sim.scenario('recover')],
  [27000, T => T.sim.setMode('system')],
  [32000, T => T.store.set('admin', false)],
  [35000, T => T.store.set('admin', true)],
  [38000, T => T.store.set('ksEnabled', false)],
  [41000, T => T.store.set('ksEnabled', true)],
  [44000, T => T.sim.stop()],
  [47000, T => T.sim.start()],
  [56000, T => T.sim.scenario('fallback')],
  [62000, T => T.sim.scenario('recover')],
  [70000, T => T.store.set('latCeiling', 20)],
  [75000, T => T.store.set('latCeiling', 800)],
  [80000, T => T.sim.bastion.enable(true)],
  [90000, T => T.sim.bastion.setMin(50)],
  [100000, T => T.sim.stop()],
  [108000, T => T.sim.start()],
  [115000, T => T.sim.stop()],
];
const END = 120000;

// Store keys both sides write with the same meaning (criticism E4). Left out on purpose (the
// report's difference table): phase, collected, degraded, uptime, traffic, history, bastion
// and the Bastion tier, latCeiling, exit details other than its state.
const KEYS = {
  run: v => v, cancelling: v => v, carrier: v => v, fallback: v => v, restarting: v => v, mode: v => v, modeBusy: v => v,
  admin: v => v, ksEnabled: v => v, ksBlocked: v => v, killswitch: v => v,
  // the simulator's order has the runtime's four ids since T-P4-05a (ruling 5th wave 02b-4: gateway
  // last, the carrier first); the prototype baseline lists the three fallback tiers
  order: v => (Array.isArray(v) ? v.filter(t => t !== 'gateway').join(',') : v), exit: v => (v ? v.state : v), update: v => (v ? v.state : v),
  pool: v => (v ? `${v.ok}/${v.total}` : v),
  'tiers.vanguard': v => (v && v.vanguard ? `${v.vanguard.ok}/${v.vanguard.total}/${v.vanguard.state}` : null),
  'tiers.sentinel': v => (v && v.sentinel ? `${v.sentinel.ok}/${v.sentinel.total}/${v.sentinel.state}` : null),
  'tiers.gateway': v => (v && v.gateway ? `${v.gateway.ok}/${v.gateway.total}/${v.gateway.state}` : null),
};
function recorder(S) {
  const seq = {};
  const put = (name, v) => { const a = seq[name] || (seq[name] = []); const x = JSON.stringify(v); if (a[a.length - 1] !== x) a.push(x); };
  for (const [name, f] of Object.entries(KEYS)) {
    const key = name.split('.')[0];
    put(name, f(S.get(key)));
    S.sub(key, v => put(name, f(v)));
  }
  return () => Object.fromEntries(Object.entries(seq).map(([k, a]) => [k, a.map(x => JSON.parse(x))]));
}
// T-P4-H2 item 3: the simulator reports a chosen Gateway as the carrier itself (the runtime's
// shape: FINAL-EXIT -> EXIT-SELECT -> <Gateway node>), and a fallback under it is invisible. The
// demo data chooses 东京 Gateway 01, the prototype baseline knew no such rule: the cases that
// compare with it or walk the fallback scenario turn the Gateway off first (gateway.select '').
async function gatewayOff(T) {
  const r = await call(T, 'gateway.select', { proxy_name: '' });
  assert.ok(r.ok, JSON.stringify(r.e));
  assert.equal(T.store.get('carrier'), 'vanguard');
}
async function runScript(T) {
  let at = 0;
  for (const [t, act] of SCRIPT) { await advance(t - at); at = t; act(T); await drain(); }
  await advance(END - at);
}

// Recorded from the T-P4-01 prototype simulator (design/sim.js + design/data.js as copied from
// .build/claude_ui_prototype, byte-identical) running SCRIPT with the same mocked timers;
// the generator is described in the T-P4-02b report.
const BASELINE = {
  "run": ["running","stopping","stopped","starting","running","stopping","stopped","starting","running","stopping","stopped"],
  "cancelling": [false],
  "carrier": ["vanguard","sentinel","vanguard",null,"vanguard","sentinel","vanguard",null,"vanguard",null],
  "fallback": [false,true,false,true,false],
  "restarting": [false,true,false],
  "mode": ["system","tun","system"],
  "modeBusy": [false,true,false,true,false],
  "admin": [true,false,true],
  "ksEnabled": [true,false,true],
  "ksBlocked": [null,"admin",null],
  "killswitch": ["armed","blocked","armed","disabled","armed","inactive","armed","inactive","armed","inactive"],
  "order": ["vanguard,sentinel,bastion","sentinel,vanguard,bastion","vanguard,sentinel,bastion","sentinel,vanguard,bastion","vanguard,sentinel,bastion"],
  "exit": ["ok","pending","ok","pending","ok","none","pending","ok","none","pending","ok","none"],
  "update": ["latest"],
  "pool": ["9/10","8/10","7/10","9/10","8/10","7/10","9/10","0/10","9/10"],
  "tiers.vanguard": ["2/2/ok","1/2/partial","0/2/down","2/2/ok","1/2/partial","0/2/down","2/2/ok","0/2/down","2/2/ok"],
  "tiers.sentinel": ["4/5/partial","0/5/down","4/5/partial"],
  "tiers.gateway": ["3/3/ok","0/3/down","3/3/ok"],
};

/* ================================================================== tests */

test('sim 120 s (start, collect, ready, fallback, recover, stop, with logs): every frame passes validate_ui_fixtures.py', withTimers(async () => {
  const T = await bootDesign();
  assert.equal(T.rt.state, 'live');
  assert.equal(T.rt.transport, 'fake');
  await gatewayOff(T);
  await runScript(T);
  const frames = globalThis.__designFrames.slice();
  assert.ok(frames.length > 150, `frames: ${frames.length}`);
  const types = new Set(frames.flatMap(f => (f.t === 'batch' ? f.items.map(i => i.topic) : [f.topic || f.t])));
  for (const topic of ['service.state', 'route.published', 'tiers.summary', 'service.collect', 'tier.transition', 'traffic.tick', 'traffic.history',
    'network.mode', 'killswitch.state', 'bastion.pool', 'exit.identity', 'protocols.mix', 'gateway.current', 'core.info', 'update.state', 'jobs',
    'nodes.list', 'subscriptions.list', 'config.items', 'bastion.sources', 'logs.batch', 'logs.feed', 'geo.status', 'alerts.history',
    'alerts.delivery', 'alerts.onboarding', 'hello', 'ack']) assert.ok(types.has(topic), topic);
  for (const shellOnly of ['ui.language', 'alerts.desktop', 'shutdown', 'update.prepareExit']) assert.ok(!types.has(shellOnly), shellOnly);
  const patches = frames.flatMap(f => (f.t === 'batch' ? f.items : [f])).filter(f => f.kind === 'patch');
  assert.ok(patches.length > 0, 'list topics send patches after their snapshot');
  const r = validate(frames);
  assert.equal(r.status, 0, r.out);
  assert.match(r.out, /0 problem\(s\)/);
  assertCatalogKeys(frames);
  // logs came through: the replayed tail (the logs page seed + the simulator's boot lines) and live lines
  const logRows = frames.flatMap(f => (f.t === 'batch' ? f.items : [f])).filter(f => f.topic === 'logs.batch').flatMap(f => f.data.records);
  assert.ok(logRows.length > 150, `log rows: ${logRows.length}`);
  assert.ok(T.named('sim:log').length > 10, 'live log records reach the logs page as sim:log');
  assert.ok(!T.rt.diag().some(d => ['frame_threw', 'bad_frame', 'unknown_frame', 'bad_evt', 'bad_kind', 'bad_key', 'topic_base_rev', 'topic_gap', 'page_broken', 'epoch_mismatch', 'replacement_too_old', 'edge_seq_jump', 'logs_seq_jump', 'jobs_sequence_gap'].includes(d.kind)), JSON.stringify(T.rt.diag().slice(-5)));
}));

test('store sequence: the keys both sides share match the prototype baseline', withTimers(async () => {
  const T = await bootDesign();
  await gatewayOff(T);
  const done = recorder(T.store);
  await runScript(T);
  const got = done();
  assert.deepEqual(Object.keys(got).sort(), Object.keys(BASELINE).sort());
  for (const k of Object.keys(BASELINE)) assert.deepEqual(got[k], BASELINE[k], k);
}));

test('cancel during start: starting + stop_requested (正在取消) then stopped, announced as cancelled', withTimers(async () => {
  const T = await bootDesign();
  T.sim.stop();
  await advance(1000);
  assert.equal(T.store.get('run'), 'stopped');
  T.sim.start();
  await advance(500);
  assert.equal(T.store.get('run'), 'starting');
  assert.equal(T.store.get('phase'), 0);
  T.sim.stop();
  await drain();
  assert.equal(T.store.get('cancelling'), true);
  assert.equal(T.store.get('run'), 'starting');
  await advance(500);
  assert.equal(T.store.get('run'), 'stopped');
  assert.equal(T.store.get('cancelling'), false);
  const last = T.named('rt:state').at(-1);
  assert.deepEqual([last.run, last.prev, last.cancelled], ['stopped', 'starting', true]);
  const starts = T.named('rt:job').map(e => e.job).filter(j => j.name === 'service.start');
  assert.equal(starts.at(-1).state, 'superseded', 'the start job is superseded by the stop');
}));

test('fallback / recover arrive as rt:tier fallback then recover(wasFallback), with tier.transition edges', withTimers(async () => {
  const T = await bootDesign();
  await gatewayOff(T);
  const at = { tier: T.named('rt:tier').length, tr: T.named('rt:transition').length, alias: T.named('sim:tier').length };
  T.sim.scenario('fallback');
  await advance(2000);
  T.sim.scenario('recover');
  await drain();
  const kinds = T.named('rt:tier').slice(at.tier).map(e => [e.kind, e.carrier, e.wasFallback === undefined ? null : e.wasFallback]);
  assert.deepEqual(kinds, [['degraded', 'vanguard', null], ['fallback', 'sentinel', null], ['recover', 'vanguard', true]]);
  assert.deepEqual(T.named('rt:transition').slice(at.tr).map(e => [e.from, e.to]), [['vanguard', 'sentinel'], ['sentinel', 'vanguard']]);
  // (T-P4-07: the rt:* -> sim:* alias is deleted with rt/alias.js; nothing mirrors them any more)
  assert.equal(T.named('sim:tier').length - at.alias, 0, 'no sim:* mirror');
}));

test('T-P4-H2: a chosen Gateway in the runtime shape; a fallback under it is invisible; cancelling and choosing it are tier edges; every frame passes the invariants', withTimers(async () => {
  const T = await bootDesign();
  const route = () => T.rt.topic('route.published').get();
  const gw = () => T.rt.topic('gateway.current').get();
  // the demo data chooses 东京 Gateway 01: it carries
  assert.equal(T.store.get('carrier'), 'gateway');
  assert.equal(T.store.get('order')[0], 'gateway');
  assert.equal(T.store.get('fallback'), false);
  assert.deepEqual([route().selected_chain, route().managed_tier, route().leaf], [['FINAL-EXIT', 'EXIT-SELECT', '东京 Gateway 01'], 'gateway', '东京 Gateway 01']);
  assert.deepEqual([gw().selected, gw().effective, gw().applied], ['东京 Gateway 01', '东京 Gateway 01', true]);
  // Vanguard fails under the Gateway: PROXY-FALLBACK moves to Sentinel, the runtime cannot see it
  const at = { tier: T.named('rt:tier').length, tr: T.named('rt:transition').length, frames: globalThis.__designFrames.length };
  const alertCount = () => { const h = T.rt.topic('alerts.history').get(); return h && Array.isArray(h.items) ? h.items.length : -1; };
  const alerts0 = alertCount();
  T.sim.scenario('fallback');
  await advance(2000);
  assert.equal(T.store.get('carrier'), 'gateway');
  // fix round 1: no tier line and no alert under the Gateway (the logs / alerts pages agree with the overview)
  const newLogs = () => globalThis.__designFrames.slice(at.frames).flatMap(f => (f.t === 'batch' ? f.items : [f]))
    .filter(f => f && (f.topic === 'logs.feed' || f.topic === 'logs.batch')).map(f => JSON.stringify(f.data));
  assert.ok(newLogs().some(s => s.includes('Vanguard 延迟升高')), 'the node-level health line is still written');
  assert.ok(!newLogs().some(s => s.includes('Sentinel 承载')), 'no "carried by Sentinel" line under a Gateway');
  assert.equal(alertCount(), alerts0, 'no "fell back to Sentinel" alert under a Gateway');
  assert.deepEqual(T.named('rt:tier').slice(at.tier), [], 'no notice: the tier under a Gateway is not reported');
  assert.deepEqual(T.named('rt:transition').slice(at.tr), []);
  // the Gateway is cancelled: Sentinel carries, a fallback that names no Gateway as its origin
  assert.ok((await call(T, 'gateway.select', { proxy_name: '' })).ok);
  assert.equal(T.store.get('carrier'), 'sentinel');
  assert.deepEqual([gw().selected, gw().effective], [null, null]);
  assert.deepEqual(T.named('rt:transition').slice(at.tr).map(e => [e.from, e.to]), [['gateway', 'sentinel']]);
  assert.deepEqual(T.named('rt:tier').slice(at.tier).filter(e => e.kind !== 'degraded').map(e => [e.kind, e.carrier, e.from || null]), [['fallback', 'sentinel', null]]);
  // a Gateway is chosen again: it carries; the banner goes quietly (no "recovered")
  assert.ok((await call(T, 'gateway.select', { proxy_name: '大阪 Gateway 02' })).ok);
  assert.equal(T.store.get('carrier'), 'gateway');
  assert.deepEqual(T.named('rt:transition').slice(at.tr).map(e => [e.from, e.to]), [['gateway', 'sentinel'], ['sentinel', 'gateway']]);
  const rec = T.named('rt:tier').slice(at.tier).filter(e => e.kind === 'recover');
  assert.deepEqual(rec.map(e => [e.carrier, e.wasFallback, e.quiet]), [['gateway', true, true]]);
  // stopped: the saved choice, nothing effective
  T.sim.stop();
  await advance(4000);
  assert.equal(T.store.get('run'), 'stopped');
  assert.deepEqual([gw().selected, gw().effective, gw().applied], ['大阪 Gateway 02', null, false]);
  // (whole frames: a batch is one delivery, the invariants hold per batch)
  const r = validate(globalThis.__designFrames.slice());
  assert.equal(r.status, 0, r.out);
}));

// Settled at once (no unhandled rejection while the simulator answers).
const settle = p => p.then(v => ({ ok: true, v }), e => ({ ok: false, e }));
async function call(T, cmd, args) { const p = settle(T.rt.call(cmd, args)); await drain(); return p; }
async function query(T, cmd, args = {}) { const p = settle(T.rt.query(cmd, args)); await drain(); return p; }
const jobsOf = (T, name) => T.named('rt:job').map(e => e.job).filter(j => j.name === name);
const sentFrames = () => globalThis.__designFrames.flatMap(f => (f.t === 'batch' ? f.items : [f]));
// T-P4-H (决定_第五波裁定 02b-3): every message_key the simulator sends, wherever it sits (ack /
// res / error frames, job errors and partial results, state topics such as a service.state
// failure, nested errors), is an id of the zh_CN catalog: the design target makes up no key.
const CATALOG_KEYS = new Set(readdirSync(join(UI, 'src', 'i18n', 'locales', 'zh_CN')).filter(f => f.endsWith('.json'))
  .flatMap(f => { const c = JSON.parse(readFileSync(join(UI, 'src', 'i18n', 'locales', 'zh_CN', f), 'utf8')); return Object.keys(c.messages || c); }));
function messageKeys(v, out = []) {
  if (Array.isArray(v)) for (const x of v) messageKeys(x, out);
  else if (v && typeof v === 'object') {
    if (typeof v.message_key === 'string') out.push(v.message_key);
    for (const k of Object.keys(v)) messageKeys(v[k], out);
  }
  return out;
}
function assertCatalogKeys(frames) {
  const keys = messageKeys(frames);
  for (const k of keys) assert.ok(CATALOG_KEYS.has(k), `message_key ${k} is not in the zh_CN catalog`);
  return keys.length;
}

test('commands: ack + jobs terminal shapes, mapped to the simulator; unmapped commands answer not_allowed', withTimers(async () => {
  const T = await bootDesign();
  const ok = r => { assert.ok(r.ok, JSON.stringify(r.e)); return r.v; };
  const code = r => { assert.equal(r.ok, false, JSON.stringify(r.v)); return r.e.code; };
  // exit.verify (manual): ack with op, queued -> running -> done with an IdentityResult
  const v = settle(T.rt.call('exit.verify', { trigger: 'manual' }));
  await advance(2500);
  assert.equal(ok(await v).status, 'ok');
  assert.deepEqual(jobsOf(T, 'exit.verify').map(j => j.state), ['queued', 'running', 'done']);
  const ack = sentFrames().find(f => f.t === 'ack' && f.op === jobsOf(T, 'exit.verify')[0].op);
  assert.ok(ack && ack.accepted === true && ack.req_id === jobsOf(T, 'exit.verify')[0].req_id, 'the ack carries the op and the req_id');
  assert.equal(T.store.get('exit').how, 'manual');
  // mode.set while running: busy, then done {changed, saved, restarted, applied}
  const m = settle(T.rt.call('mode.set', { mode: 'mixed' }));
  await drain();
  assert.equal(T.store.get('modeBusy'), true);
  await advance(1600);
  assert.deepEqual(ok(await m), { mode: 'mixed', changed: true, saved: true, restarted: true, applied: true, apply_error: null });
  assert.equal(T.store.get('mode'), 'mixed');
  assert.equal(T.store.get('modeBusy'), false);
  // admin_required (配置's demo link writes admin: the simulator adopts it)
  T.store.set('admin', false);
  await drain();
  assert.equal(T.store.get('killswitch'), 'blocked');
  assert.equal(code(await call(T, 'mode.set', { mode: 'tun' })), 'admin_required');
  T.store.set('admin', true);
  await drain();
  // service.restart: restarting while in flight, then the result
  const rs = settle(T.rt.call('service.restart', {}));
  await drain();
  assert.equal(T.store.get('restarting'), true);
  // a second restart is merged into the one under way: same terminal, same result (T-P4-H)
  const rs2 = settle(T.rt.call('service.restart', {}));
  await advance(1500);
  const rr = ok(await rs);
  assert.deepEqual(ok(await rs2), rr);
  assert.equal(rr.applied, true);
  assert.equal(rr.core_generation, T.rt.topic('service.state').get().core.core_generation);
  assert.equal(T.store.get('restarting'), false);
  // gateway.select: known and unknown proxy
  assert.deepEqual(ok(await call(T, 'gateway.select', { proxy_name: '大阪 Gateway 02' })),
    { selected: '大阪 Gateway 02', applied: true, apply_error: null, selection: '大阪 Gateway 02', pending: null });
  assert.equal(T.rt.topic('gateway.current').get().selected, '大阪 Gateway 02');
  assert.equal(code(await call(T, 'gateway.select', { proxy_name: 'nope' })), 'validation');
  // settings.apply: a SaveResult and config.changed; a stale base_rev is a conflict; the values stay
  const snap = ok(await query(T, 'config.snapshot'));
  const sa = ok(await call(T, 'settings.apply', { base_rev: snap.rev, changes: [{ section: 'latency', values: { primary_gateway_max_latency_ms: 600 } }] }));
  assert.deepEqual(sa, { rev: snap.rev + 1, sections: [{ section: 'latency', saved: true, applied: true, effect: 'none', error: null }], pending: null });
  assert.equal(T.store.get('configRev'), snap.rev + 1);
  assert.equal(ok(await query(T, 'config.snapshot')).values.latency.primary_gateway_max_latency_ms, 800, 'answers only; the config is not changed');
  assert.equal(code(await call(T, 'settings.apply', { base_rev: snap.rev, changes: [{ section: 'ui', values: {} }] })), 'conflict');
  assert.equal(code(await call(T, 'settings.apply', { base_rev: snap.rev + 1, changes: [] })), 'validation');
  // logs.setMihomoLevel (res)
  assert.deepEqual(ok(await call(T, 'logs.setMihomoLevel', { level: 'debug' })), { level: 'debug', applied: true });
  assert.equal(T.rt.topic('logs.feed').get().level, 'debug');
  // app.update.check, geo.update, subscriptions.refresh
  const uc = settle(T.rt.call('app.update.check', {}));
  const gu = settle(T.rt.call('geo.update', { force: true }));
  const sr = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  await drain();
  assert.equal(T.rt.topic('subscriptions.list', 'sentinel').get().items[0].refreshing, true);
  assert.equal(T.rt.topic('geo.status').get().running, true);
  await advance(1600);
  assert.equal(ok(await uc).state, 'idle');
  assert.equal(ok(await gu).completed, true);
  assert.equal(ok(await sr).stored, true);
  assert.equal(T.rt.topic('subscriptions.list', 'sentinel').get().items[0].refreshing, false);
  const nope = await call(T, 'subscriptions.refresh', { item_id: 'nope' });
  assert.equal(code(nope), 'conflict', 'bridge_node_commands: an unknown item is conflict');
  assert.equal(nope.e.reason_code, 'unknown_item');
  assert.equal(nope.e.result.stored, false, 'the idle RefreshResult comes with it');
  // exit.verify cancel: ownership only (the check goes on), the job ends cancelled
  const vc = T.rt.call('exit.verify', { trigger: 'manual' });
  const vcs = settle(vc);
  await drain();
  ok(await settle(T.rt.cancel(vc)));
  assert.equal(code(await vcs), 'cancelled');
  await advance(2500);
  // service.stop / service.start: terminal results and refusals
  const st = settle(T.rt.call('service.stop', { expected_generation: T.rt.topic('service.state').get().intent.runtime_generation }));
  await advance(700);
  assert.deepEqual(ok(await st), { startup_phase: 'stopped', shutdown_exit_code: 0, cleanup_pending: false });
  assert.deepEqual(ok(await call(T, 'service.stop', {})), { startup_phase: 'stopped', shutdown_exit_code: 0, cleanup_pending: false },
    'a stop of a stopped service is accepted and has nothing to do (DesktopRuntime.request_stop)');
  assert.equal(code(await call(T, 'service.restart', {})), 'not_running');
  const refused = sentFrames().filter(f => f.t === 'ack' && f.accepted === false).at(-1);
  assert.deepEqual([refused.skipped_reason, refused.error.code, refused.error.message_key], ['not_running', 'not_running', 'errors.bridge.not_running'],
    'a refused ack in bridge_main.pump\'s shape');
  assert.equal(code(await call(T, 'exit.verify', { trigger: 'manual' })), 'not_running');
  assert.equal(code(await call(T, 'geo.update', { force: true })), 'needs_route');
  assert.equal(code(await call(T, 'logs.setMihomoLevel', { level: 'info' })), 'not_running');
  assert.deepEqual(ok(await call(T, 'mode.set', { mode: 'system' })), { mode: 'system', changed: true, saved: true, restarted: false, applied: true, apply_error: null });
  const ss = settle(T.rt.call('service.start', {}));
  await advance(3300);
  assert.deepEqual(ok(await ss), { startup_phase: 'running', failure_code: null });
  assert.deepEqual(ok(await call(T, 'service.start', {})), { startup_phase: 'running', failure_code: null }, 'DesktopRuntime.start returns its status');
  assert.equal(code(await call(T, 'service.stop', { expected_generation: 1 })), 'conflict');
  // unmapped commands: not_allowed (the design target's honest degradation)
  for (const [cmd, args] of [['nodes.update', { item_id: 'cfg-v-hk03', updates: {} }], ['nodes.delete', { item_id: 'cfg-v-hk03' }],
    ['nodes.reveal', { item_id: 'cfg-v-hk03' }], ['import.preview', { tier: 'vanguard', text: 'x' }], ['import.apply', { tier: 'vanguard', mode: 'paste' }],
    ['alerts.test', { draft: {} }], ['alerts.onboarding.cancel', { session_id: 's' }], ['app.update.install', {}], ['settings.validate', { section: 'ui', field: 'language', value: 'x' }],
    ['nodes.diagnose', { node_id: 'v-hk03', probe_mode: 'direct', request_id: 'p1.9' }], ['nodes.cancelDiagnose', { request_id: 'p1.9' }],
    ['logs.exportBundle', { save_ref: 'r' }], ['logs.exportFiltered', { save_ref: 'r', format: 'json' }], ['host.openFolder', { kind: 'logs' }]]) {
    assert.equal(code(await call(T, cmd, args)), 'not_allowed', cmd);
  }
  assert.ok(assertCatalogKeys(sentFrames()) >= 8, 'refusals and failures carry catalog ids');
  const r = validate(globalThis.__designFrames);
  assert.equal(r.status, 0, r.out);
}));

test('queries: answers in the §11 / §16 shapes', withTimers(async () => {
  const T = await bootDesign();
  const q = async (cmd, args = {}) => { const r = await query(T, cmd, args); assert.ok(r.ok, `${cmd}: ${JSON.stringify(r.e)}`); return r.v; };
  const snap = await q('config.snapshot');
  assert.deepEqual(Object.keys(snap).sort(), ['pending', 'read_errors', 'rev', 'secret_fields_set', 'values']);
  assert.deepEqual(Object.keys(snap.values).sort(), ['alerts', 'autostart', 'dns', 'killswitch', 'latency', 'network', 'routing', 'sources', 'traffic', 'ui']);
  assert.equal(snap.values.latency.primary_gateway_max_latency_ms, 800);
  assert.equal(snap.values.network.mode, 'system');
  const info = await q('app.info');
  assert.equal(info.version, '0.1.0');
  assert.ok(info.caps.includes('service.start') && info.caps.includes('logs.tail'));
  assert.ok(info.listeners.some(l => l.kind === 'mixed' && l.port === 7890 && l.available === true));
  const cat = await q('alerts.catalog');
  assert.ok(cat.channels.length > 3 && Array.isArray(cat.event_groups) && Array.isArray(cat.scopes) && cat.acl.plaintext_storage === true);
  const specs = await q('import.specs');
  assert.ok(specs.protocols.length > 3 && specs.protocols.every(p => p.label_id && Array.isArray(p.fields)));
  const tail = await q('logs.tail', { n: 4000 });
  assert.ok(tail.records.length > 150 && tail.next_cursor === null && tail.gap === false);
  assert.equal(tail.watermark, tail.records.at(-1).seq);
  assert.ok(tail.records.some(x => x.key === 'runtime.start_requested' && x.title === '正在启动服务'), 'the logs page seed is in the tail');
  assert.ok(tail.records.some(x => x.key === 'log.raw' && x.source === 'MIHOMO'), 'raw lines: key log.raw in the history (empty in logs.batch), as bridge_log_view does');
  assert.equal((await q('logs.tail', { since_seq: tail.watermark - 3, n: 10 })).records.length, 3);
  const issues = await q('logs.query', { before_seq: tail.watermark + 1, filter: { levels: ['error', 'critical'] }, n: 50 });
  assert.ok(issues.records.length > 0 && issues.records.every(x => x.level === 'error' || x.level === 'critical'));
  const node = await q('nodes.detail', { node_id: 'v-hk03' });
  assert.deepEqual(Object.keys(node).sort(), ['check_interval_s', 'config_item', 'last_diagnostic', 'node']);
  assert.equal(node.node.tier, 'vanguard');
  assert.equal(node.config_item.id, 'cfg-v-hk03');
  assert.equal((await query(T, 'nodes.detail', { node_id: 'zz' })).e.code, 'validation');
  assert.deepEqual(await q('autostart.state'), { supported: true, enabled: false, target: 'none', mismatch: false, error: null });
  assert.equal((await q('geo.status')).running, false);
  const del = await q('alerts.deliveries', { limit: 1 });
  assert.equal(del.items.length, 1);
  assert.equal(del.next_cursor, null);
  const r = validate(globalThis.__designFrames);
  assert.equal(r.status, 0, r.out);
}));

test('T.sim.init() is idempotent; ?lang rewrites hello.lang; the replayed log tail fills T.sim.logs silently', withTimers(async () => {
  const T = await bootDesign({ search: '?lang=en_US' });
  assert.equal(T.store.get('lang'), 'en_US');
  assert.equal(T.store.get('run'), 'running', 'the store is filled during connect, before any page renders');
  const n = globalThis.__designFrames.length;
  T.sim.init(); T.sim.init();
  await drain();
  assert.equal(globalThis.__designFrames.length, n, 'init() after the boot sends nothing');
  assert.ok(T.sim.logs.length >= 5, 'the boot lines are in T.sim.logs');
  assert.equal(T.named('sim:log').length, 0, 'the replayed tail raises no sim:log (the prototype pushed its boot lines silently)');
  assert.ok(T.named('rt:state').every(e => e.instant), 'everything before rt:replayed is instant');
  assert.equal(T.named('rt:replayed').length, 1);
  const bad = await bootDesign({ search: '?lang=xx_YY' });
  assert.equal(bad.store.get('lang'), 'zh_CN');
}));

test('design/catalog.json matches bridge_catalog (alerts.catalog, import.specs)', () => {
  const r = python(['-c', [
    'import json, sys, bridge_catalog as c',
    `doc = json.load(open(${JSON.stringify(join(UI, 'src', 'design', 'catalog.json'))}, encoding="utf-8"))`,
    'ok = doc["alerts"] == c.build_alert_channels() and doc["import"] == c.build_import_specs()',
    'print("in sync" if ok else "drift")',
    'sys.exit(0 if ok else 1)'].join('\n')]);
  assert.equal(r.status, 0, (r.stdout || '') + (r.stderr || ''));
});

test('static.js: in the app bundle; T.data tier / mode metadata without the demo data', async () => {
  const app = await esbuild.build({ absWorkingDir: UI, entryPoints: [join(UI, 'src', 'entry', 'app.js')], bundle: true, write: false,
    format: 'iife', target: 'chrome120', metafile: true, logLevel: 'silent', define: defineFor('app', []),
    loader: { '.woff2': 'empty', '.woff': 'empty' }, external: ['*.woff2'] });
  const inputs = Object.keys(app.metafile.inputs).map(p => p.replace(/\\/g, '/'));
  assert.ok(inputs.some(p => p.endsWith('src/data/static.js')), 'static.js is in the app bundle');
  for (const f of ['src/design/sim.js', 'src/design/frames.js', 'src/design/demo-data.js', 'src/design/catalog.json']) assert.ok(!inputs.some(p => p.endsWith(f)), f);
  // static.js alone (the app target once T-P4-07 drops the transitional design data)
  const only = await bundle("import '../data/static.js';\n", 'app');
  const T = { };
  new Function('window', only.code)({ T });
  assert.deepEqual(T.data.tiers.map(t => [t.id, t.icon, t.order, !!t.caution, !!t.fixed]),
    [['vanguard', 'vanguard', 1, false, false], ['sentinel', 'sentinel', 2, false, false], ['bastion', 'bastion', 3, true, false], ['gateway', 'gateway', null, false, true]]);
  assert.equal(T.data.tier('bastion').name, 'Bastion');
  assert.deepEqual(Object.keys(T.data.modes), ['system', 'tun', 'mixed']);
  assert.deepEqual(Object.values(T.data.modes).map(m => !!m.admin), [false, true, true]);
  assert.equal(T.data.tiers[0].enabled, undefined, 'enabled is a runtime fact, not static');
  // the app hook: since T-P4-07 the static metadata only (the transitional design data is gone,
  // so `enabled` is not there and no demo data either; nothing from src/design/ is an input)
  const hook = await bundle("import './data-app.js';\n", 'app');
  const T2 = { util: makeWindowT().util };
  new Function('window', hook.code)({ T: T2 });
  // (T-P4-04a: the wording is in the catalog; without an i18n runtime the getters give the message id.
  // F-03: a tier has no words of its own, only its code name)
  assert.deepEqual(T2.data.tiers.map(t => [t.id, t.name, t.icon, t.order, t.enabled]),
    [['vanguard', 'Vanguard', 'vanguard', 1, undefined], ['sentinel', 'Sentinel', 'sentinel', 2, undefined], ['bastion', 'Bastion', 'bastion', 3, undefined], ['gateway', 'Gateway', 'gateway', null, undefined]]);
  assert.ok(T2.data.tiers.every(t => !('role' in t) && !('desc' in t) && !('blurb' in t)), 'F-03: no role / desc / blurb on a tier');
  assert.equal(T2.data.modes.tun.label, 'mode.tun.label');
  assert.equal(T2.data.seedHistory, undefined, 'no demo data in the app hook');
  assert.deepEqual(hook.inputs.filter(f => /src\/design\//.test(f)), [], 'the app hook imports nothing from design/');
  // with a T.t installed the getters read the catalog through it (the code name is a parameter)
  const T3 = { t: (id, p) => `<${id}|${p && p.vanguard}>` };
  new Function('window', only.code)({ T: T3 });
  assert.equal(T3.data.modes.tun.label, '<mode.tun.label|Vanguard>');
  assert.ok(!readFileSync(join(UI, 'src', 'data', 'static.js'), 'utf8').startsWith('// @i18n-pending'), 'static.js is no longer @i18n-pending');
});

/* ---- fix round 1 (review of T-P4-02b): every job reaches a terminal; config writes move the rev ---- */
const TERMINAL = new Set(['done', 'failed', 'cancelled', 'superseded']);
// ops whose last jobs frame is not terminal (PROTOCOL §6: a terminal is never swallowed)
function openJobs() {
  const last = new Map();
  for (const f of sentFrames()) if (f.t === 'evt' && f.topic === 'jobs') last.set(f.data.op, `${f.data.name}:${f.data.state}`);
  return [...last.values()].filter(s => !TERMINAL.has(s.split(':')[1]));
}

test('stop while a start is being cancelled: merged into the first stop, both end done; a start while a stop is pending is refused lifecycle_busy; nothing left open', withTimers(async () => {
  const T = await bootDesign();
  const s0 = settle(T.rt.call('service.stop', {}));
  await advance(1000);
  assert.ok((await s0).ok);
  const st = settle(T.rt.call('service.start', {}));
  await advance(500);
  assert.equal(T.store.get('run'), 'starting');
  const first = settle(T.rt.call('service.stop', {}));
  await drain();
  assert.equal(T.store.get('cancelling'), true);
  // the second stop shares the stop intent (DesktopRuntime.request_stop): accepted, ends with the first
  const second = settle(T.rt.call('service.stop', {}));
  await drain();
  // a start while the stop is pending: the executor refuses it (lifecycle_busy, errors.bridge.lifecycle_busy)
  const busy = await call(T, 'service.start', {});
  assert.equal(busy.ok, false);
  assert.equal(busy.e.code, 'lifecycle_busy');
  const refused = sentFrames().filter(f => f.t === 'ack' && f.accepted === false).at(-1);
  assert.deepEqual([refused.skipped_reason, refused.error.message_key], ['lifecycle_busy', 'errors.bridge.lifecycle_busy']);
  // the page's cancel segment stays clickable while cancelling (overview renderRun): toggle again
  T.sim.toggle();
  await drain();
  T.sim.toggle();
  await advance(600);
  const stopped = { startup_phase: 'stopped', shutdown_exit_code: 0, cleanup_pending: false };
  assert.deepEqual((await first).v, stopped);
  assert.deepEqual((await second).v, stopped, 'the merged stop ends with the same result');
  assert.equal((await st).e.code, 'superseded');
  assert.equal(T.store.get('run'), 'stopped');
  assert.equal(T.store.get('cancelling'), false);
  assert.deepEqual(jobsOf(T, 'service.stop').filter(j => TERMINAL.has(j.state)).map(j => j.state), ['done', 'done', 'done', 'done', 'done']);
  assert.deepEqual(openJobs(), []);
  // a stop from running, then another stop while stopping: merged, both end done
  const s2 = settle(T.rt.call('service.start', {}));
  await advance(3300);
  assert.ok((await s2).ok);
  const a = settle(T.rt.call('service.stop', {}));
  await drain();
  assert.equal(T.store.get('run'), 'stopping');
  const b = settle(T.rt.call('service.stop', {}));
  await advance(700);
  assert.deepEqual((await a).v, stopped);
  assert.deepEqual((await b).v, stopped);
  assert.deepEqual(openJobs(), []);
  // a start while starting is merged into it; both end done (or both superseded by a stop)
  const c1 = settle(T.rt.call('service.start', {}));
  await advance(300);
  const c2 = settle(T.rt.call('service.start', {}));
  await advance(3300);
  assert.deepEqual((await c1).v, { startup_phase: 'running', failure_code: null });
  assert.deepEqual((await c2).v, { startup_phase: 'running', failure_code: null });
  T.sim.stop();
  await advance(700);
  const d1 = settle(T.rt.call('service.start', {}));
  await advance(300);
  const d2 = settle(T.rt.call('service.start', {}));
  await drain();
  T.sim.stop();
  await advance(700);
  assert.equal((await d1).e.code, 'superseded');
  assert.equal((await d2).e.code, 'superseded');
  // a restart merged into one under way is superseded with it by a stop
  T.sim.start();
  await advance(3500);
  const r1 = settle(T.rt.call('service.restart', {}));
  const r2 = settle(T.rt.call('service.restart', {}));
  await drain();
  T.sim.stop();
  await advance(700);
  assert.equal((await r1).e.code, 'superseded');
  assert.equal((await r2).e.code, 'superseded');
  assert.deepEqual(openJobs(), []);
  assertCatalogKeys(sentFrames());
  const r = validate(globalThis.__designFrames);
  assert.equal(r.status, 0, r.out);
}));

test('repeated subscription refresh / Geo / update check jobs: queued, one after another, shared (the sidecar\'s queued semantics); a queued job can be cancelled', withTimers(async () => {
  const T = await bootDesign();
  const refreshing = () => T.rt.topic('subscriptions.list', 'sentinel').get().items[0].refreshing;
  const opOf = name => jobsOf(T, name).map(j => j.op);
  // subscriptions.refresh x3 (the serial mutation domain): the 2nd and 3rd wait queued
  const pa = T.rt.call('subscriptions.refresh', { item_id: 'nebula' });
  const a = settle(pa);
  const b = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  const pc = T.rt.call('subscriptions.refresh', { item_id: 'nebula' });
  const c = settle(pc);
  await drain();
  const [opA, opB, opC] = [...new Set(opOf('subscriptions.refresh'))];
  assert.equal(jobsOf(T, 'subscriptions.refresh').filter(j => j.op === opB).at(-1).state, 'queued');
  assert.equal(jobsOf(T, 'subscriptions.refresh').filter(j => j.op === opA).at(-1).state, 'running');
  assert.equal(refreshing(), true);
  // the third one is cancelled while queued (bridge_jobs.cancel: queued and not started -> cancelled)
  const cc = await settle(T.rt.cancel(pc));
  assert.ok(cc.ok, JSON.stringify(cc.e));
  assert.equal((await c).e.code, 'cancelled');
  // a running one is not cancellable
  const nc = await settle(T.rt.cancel(pa));
  assert.equal(nc.ok, false);
  assert.equal(nc.e.code, 'not_cancellable');
  await advance(1600);
  assert.equal((await a).v.stored, true);
  assert.equal(jobsOf(T, 'subscriptions.refresh').filter(j => j.op === opB).at(-1).state, 'running', 'the next one runs when the first ended');
  assert.equal(refreshing(), true);
  await advance(1600);
  assert.equal((await b).v.stored, true);
  assert.equal(refreshing(), false);
  assert.deepEqual(jobsOf(T, 'subscriptions.refresh').filter(j => j.op === opA).map(j => j.state), ['queued', 'running', 'done']);
  assert.deepEqual(jobsOf(T, 'subscriptions.refresh').filter(j => j.op === opB).map(j => j.state), ['queued', 'running', 'done']);
  assert.deepEqual(jobsOf(T, 'subscriptions.refresh').filter(j => j.op === opC).map(j => j.state), ['queued', 'cancelled']);
  // cancel of an op that ended: not_running (bridge_jobs.cancel)
  const ended = await settle(T.rt.cancel(opA));
  assert.equal(ended.ok, false);
  // geo.update x2: both running, the downloads one after another (the updater's action lock)
  const g1 = settle(T.rt.call('geo.update', { force: true }));
  const g2 = settle(T.rt.call('geo.update', { force: true }));
  await advance(1300);
  assert.equal((await g1).v.completed, true);
  assert.equal((await g1).v.status.running, false, 'its own download completed');
  assert.equal(T.rt.topic('geo.status').get().running, true, 'the second download is under way');
  assert.equal(await Promise.race([g2.then(() => 'settled'), Promise.resolve('pending')]), 'pending');
  await advance(1300);
  assert.equal((await g2).v.completed, true);
  assert.equal(T.rt.topic('geo.status').get().running, false);
  // app.update.check x2: the second shares the check in flight and ends with its result
  const u1 = settle(T.rt.call('app.update.check', {}));
  await advance(400);
  const u2 = settle(T.rt.call('app.update.check', {}));
  await advance(600);
  assert.deepEqual((await u2).v, (await u1).v, 'one check, one result (UpdateOwner.check single flight)');
  assert.equal(T.rt.topic('update.state').get().state, 'idle');
  assert.deepEqual(openJobs(), []);
  assertCatalogKeys(sentFrames());
  const r = validate(globalThis.__designFrames);
  assert.equal(r.status, 0, r.out);
}));

test('geo.update / subscriptions.refresh / app.update.check in flight across stop, start and setState: each ends, the topics go back to idle', withTimers(async () => {
  const T = await bootDesign();
  const geo = () => T.rt.topic('geo.status').get();
  const refreshing = () => T.rt.topic('subscriptions.list', 'sentinel').get().items[0].refreshing;
  const upd = () => T.rt.topic('update.state').get().state;
  // running -> all three, then stop at once
  let gu = settle(T.rt.call('geo.update', { force: true }));
  let sr = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  let uc = settle(T.rt.call('app.update.check', {}));
  await drain();
  assert.equal(geo().running, true);
  assert.equal(refreshing(), true);
  assert.equal(upd(), 'checking');
  T.sim.stop();
  await advance(2000);
  assert.equal(T.store.get('run'), 'stopped');
  assert.equal((await gu).e.code, 'needs_route', 'the route went away under the download');
  assert.equal(geo().running, false);
  assert.equal((await sr).v.stored, true);
  assert.equal(refreshing(), false);
  assert.equal((await uc).v.state, 'idle');
  assert.equal(upd(), 'idle');
  // stopped -> update check (and a refresh), then start: start / finishStart clear only the lifecycle timers
  uc = settle(T.rt.call('app.update.check', {}));
  sr = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  await drain();
  T.sim.start();
  await advance(3500);
  assert.equal(T.store.get('run'), 'running');
  assert.equal((await uc).v.state, 'idle');
  assert.equal(upd(), 'idle');
  assert.equal((await sr).v.stored, true);
  assert.equal(refreshing(), false);
  // running -> geo.update across setState('running') (the demo reset): it completes
  gu = settle(T.rt.call('geo.update', { force: true }));
  uc = settle(T.rt.call('app.update.check', {}));
  await drain();
  T.sim.setState('running');
  await advance(1500);
  assert.equal((await gu).v.completed, true);
  assert.equal(geo().running, false);
  assert.equal((await uc).v.state, 'idle');
  // ... and across setState('stopped'): failed needs_route, running back to false
  gu = settle(T.rt.call('geo.update', { force: true }));
  sr = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  await drain();
  T.sim.setState('stopped');
  await advance(2000);
  assert.equal((await gu).e.code, 'needs_route');
  assert.equal(geo().running, false);
  assert.equal((await sr).v.stored, true);
  assert.equal(refreshing(), false);
  // ... and during a start (finishStart runs while they are in flight)
  T.sim.start();
  await advance(2000);
  uc = settle(T.rt.call('app.update.check', {}));
  sr = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  await advance(2000);
  assert.equal(T.store.get('run'), 'running');
  assert.equal((await uc).v.state, 'idle');
  assert.equal((await sr).v.stored, true);
  assert.deepEqual(openJobs(), []);
  const r = validate(globalThis.__designFrames);
  assert.equal(r.status, 0, r.out);
}));

test('mode.set / gateway.select / subscriptions.refresh move the config rev and send config.changed (PROTOCOL §15); settings.apply checks the section', withTimers(async () => {
  const T = await bootDesign();
  const rev0 = (await query(T, 'config.snapshot')).v.rev;
  const changed = () => sentFrames().filter(f => f.topic === 'config.changed').map(f => f.data);
  // no change, a refusal, a failure: the rev stays
  assert.equal((await call(T, 'mode.set', { mode: 'system' })).v.changed, false);
  T.store.set('admin', false);
  await drain();
  assert.equal((await call(T, 'mode.set', { mode: 'tun' })).e.code, 'admin_required');
  T.store.set('admin', true);
  await drain();
  assert.equal((await call(T, 'gateway.select', { proxy_name: 'nope' })).e.code, 'validation');
  assert.deepEqual(changed(), []);
  assert.equal((await query(T, 'config.snapshot')).v.rev, rev0);
  // a running mode switch: restart effect
  const m = settle(T.rt.call('mode.set', { mode: 'mixed' }));
  await advance(1600);
  assert.ok((await m).ok);
  assert.equal(T.store.get('configRev'), rev0 + 1);
  assert.deepEqual(changed().at(-1), { rev: rev0 + 1, files: ['local'], sections: [{ section: 'network', saved: true, applied: true, effect: 'restart', error: null }],
    pending: null, external_detection: 'bridge_writes_only' });
  assert.ok((await call(T, 'gateway.select', { proxy_name: '大阪 Gateway 02' })).ok);
  assert.equal(T.store.get('configRev'), rev0 + 2);
  assert.deepEqual(changed().at(-1).sections, [{ section: 'gateway', saved: true, applied: true, effect: 'reload', error: null }]);
  const sr = settle(T.rt.call('subscriptions.refresh', { item_id: 'nebula' }));
  await advance(1600);
  assert.ok((await sr).ok);
  assert.equal(T.store.get('configRev'), rev0 + 3);
  assert.deepEqual(changed().at(-1).files, []);
  // a switch kept by a stop (applies on the next start) is a write too
  const m2 = settle(T.rt.call('mode.set', { mode: 'system' }));
  await drain();
  T.sim.stop();
  await advance(700);
  assert.equal((await m2).v.restarted, false);
  assert.equal(T.store.get('configRev'), rev0 + 4);
  assert.equal((await query(T, 'config.snapshot')).v.rev, rev0 + 4, 'config.snapshot reports the same rev');
  // settings.apply: a section outside the ten ConfigValues sections is refused, like the sidecar
  const bad = await call(T, 'settings.apply', { base_rev: rev0 + 4, changes: [{ section: 'bogus', values: {} }] });
  assert.equal(bad.e.code, 'validation');
  assert.equal(bad.e.field, 'section');
  assert.equal(T.store.get('configRev'), rev0 + 4);
  // the design target makes up no message_key: errors.<code>, or a key the catalog has
  assert.ok(assertCatalogKeys(sentFrames()) >= 3);
  const r = validate(globalThis.__designFrames);
  assert.equal(r.status, 0, r.out);
}));

test('FakeTransport sim mode: no backend throws; a backend gets the replay, the invoke id, a diag port; invocations are capped', async () => {
  const { FakeTransport } = await import('../../src/rt/transport-fake.js');
  assert.throws(() => new FakeTransport({ mode: 'sim' }), /requires a sim backend/);
  let pushFn = null, diagFn = null, invoked = null;
  const be = {
    replay: () => [{ t: 'rt:reset', epoch: 'e1', reason: 'reconnect' }],
    attach(p, d) { pushFn = p; diagFn = d; },
    invoke(p) { invoked = p; pushFn({ t: 'ack', epoch: 'e1', id: p.id, req_id: p.req_id, accepted: true }); },
  };
  const ft = new FakeTransport({ mode: 'sim', sim: be });
  const got = [], diags = [];
  await ft.connect(d => got.push(d.frame), { onDiag: d => diags.push(d) });
  assert.deepEqual(got.map(f => f.t), ['rt:reset']);
  const res = await ft.invoke('rt', { req_id: 'p.1', cmd: 'service.start', args: {} });
  assert.equal(invoked.id, res.id);
  assert.equal(got.at(-1).t, 'ack');
  assert.equal(got.at(-1).id, res.id, 'the backend echoes the id invoke() resolved with');
  diagFn({ kind: 'sim_commit_overflow', rounds: 8 });
  assert.deepEqual(diags, [{ kind: 'sim_commit_overflow', rounds: 8 }]);
  for (let i = 0; i < 300; i++) await ft.invoke('rt', { req_id: `p.${i + 2}`, cmd: 'logs.tail', args: {} });
  assert.equal(ft.invocations.length, 200, 'sim mode keeps the latest 200 invocations only');
  assert.equal(ft.invocations.at(-1).req_id, 'p.301');
});

test('the 120 s run records no sim_commit_overflow diagnostic', withTimers(async () => {
  const T = await bootDesign();
  await runScript(T);
  assert.ok(!T.rt.diag().some(d => d.kind === 'sim_commit_overflow'));
}));

// Pure projection cases in the runtime's own shapes (review S1): tiers.summary.order puts the
// carrier first and lists gateway (desktop_runtime tiers_summary_status), fallback is carrier in
// {sentinel, bastion}. With tier.transition in caps, the fallback / recover kinds come from the
// DTO's from / to only (project.js, ratified by the 4th-wave ruling item 1). Since T-P4-H
// (决定_第五波裁定 02b-1) a backup -> gateway transition is a recovery too, and tiers.summary's
// level is reconciled with the transitions (one notice per change, see rt_project.test.mjs).
test('rt:tier from tier.transition in the runtime shapes (order carrier-first with gateway)', async () => {
  const { live, evt, tierSummary } = await import('./rt_helpers.mjs');
  const topics = ['service.state', 'route.published', 'tiers.summary', 'traffic.tick', 'traffic.history', 'logs.batch', 'core.info', 'jobs', 'tier.transition'];
  const shape = carrier => tierSummary({ carrier, fallback: carrier === 'sentinel' || carrier === 'bastion', degraded: carrier !== 'vanguard',
    order: [carrier, ...['vanguard', 'sentinel', 'bastion', 'gateway'].filter(t => t !== carrier)] });
  const { T, fake } = await live({ hello: { topics } }, [evt('tiers.summary', 1, 'snapshot', shape('vanguard'))]);
  let rev = 1, seq = 0;
  const hop = (from, to) => {
    seq += 1;
    fake.deliver(evt('tier.transition', seq, 'edge', { from, to, observed_at_ms: seq, authority: 'observed' }, { seq }));
    rev += 1;
    fake.deliver(evt('tiers.summary', rev, 'snapshot', shape(to)));
  };
  hop('vanguard', 'vanguard');                 // first frame of the topic: instant, no notice
  const rows = [
    ['vanguard', 'sentinel', [['fallback', 'sentinel', null]]],
    ['sentinel', 'bastion', [['fallback', 'bastion', null]]],
    ['bastion', 'sentinel', [['fallback', 'sentinel', null]]],   // still fallback, with from: the page rewords in place
    ['sentinel', 'vanguard', [['recover', 'vanguard', true]]],
    ['vanguard', 'bastion', [['fallback', 'bastion', null]]],
    ['bastion', 'vanguard', [['recover', 'vanguard', true]]],
    ['vanguard', 'vanguard', []],                                  // from === to
    ['vanguard', 'sentinel', [['fallback', 'sentinel', null]]],
    ['sentinel', 'gateway', [['recover', 'gateway', true]]],      // backup -> gateway: recover (T-P4-H)
    // gateway -> vanguard is no level change (no fallback notice); this shape marks the gateway
    // carrier degraded and Vanguard not, but that is a switch of carrier, not a latency recover of
    // one carrier: nothing (T-P4-H2 fix round 1, M1; was recover(wasFallback false) before)
    ['gateway', 'vanguard', []],
  ];
  for (const [from, to, want] of rows) {
    const before = T.named('rt:tier').length;
    hop(from, to);
    const got = T.named('rt:tier').slice(before).map(e => [e.kind, e.carrier, e.wasFallback === undefined ? null : e.wasFallback]);
    assert.deepEqual(got, want, `${from} -> ${to}`);
    assert.equal(T.store.get('carrier'), to);
    assert.equal(T.store.get('order')[0], to, 'the runtime order puts the carrier first');
  }
});

test('static.js is in the app build (buildTarget, the npm run build pipeline: its main script carries the static wording)', async () => {
  const { buildTarget } = await import('../../build.mjs');
  const { files, manifest } = await buildTarget({ target: 'app' });
  const main = manifest.files.find(f => /^app\.[A-Z0-9]+\.js$/.test(f.path));
  assert.ok(main, 'build-manifest.json lists the app script');
  const js = new TextDecoder().decode(files.get(main.path));
  // (T-P4-04a: the static wording is message ids in the main script, its zh_CN text in the language
  // bundle; static.js builds the ids, `mode.${id}` + the field, so the main script carries the
  // builder and the field names, the language bundle the full ids. F-03: tiers have no wording.)
  const unesc = s => s.replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
  assert.ok(js.includes('`mode.${'), 'the id builder of static.js');
  assert.ok(js.includes('"desc"'), '"desc"');
  assert.ok(!js.includes('"blurb"'), 'F-03: no tier blurb field');
  for (const s of ['通过虚拟网卡（TUN）接管全部流量']) assert.ok(!unesc(js).includes(s), `no ${s} in the main script`);
  const zh = manifest.files.filter(f => /^locale-zh_CN\.[A-Z0-9]+\.js$/.test(f.path)).map(f => unesc(new TextDecoder().decode(files.get(f.path)))).join('\n');
  assert.ok(zh, 'build-manifest.json lists the zh_CN language bundle');
  for (const s of ['通过虚拟网卡（TUN）接管全部流量']) assert.ok(zh.includes(s), `${s} in the zh_CN bundle`);
  for (const id of ['mode.tun.desc']) assert.ok(zh.includes(`"${id}"`), `${id} in the zh_CN bundle`);
  assert.ok(!zh.includes('"tier.gateway.blurb"'), 'F-03: the tier blurbs are gone');
  assert.equal(manifest.target, 'app');
});
