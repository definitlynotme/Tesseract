// Shared helpers for the rt_*.test.mjs files (T-P4-02a). Not a test file itself
// (test/unit/index.js only loads *.test.mjs).
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRt } from '../../src/rt/client.js';
import { createFakeClock } from '../../src/rt/clock.js';
import { createIds } from '../../src/rt/ids.js';
import { FakeTransport } from '../../src/rt/transport-fake.js';

export const UI = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
export const REPO = resolve(UI, '..');
export const SCENARIOS = join(UI, 'test', 'fixtures', 'scenarios');
// Same resolution as tools/check.mjs: TESSERACT_UI_PYTHON, then the Windows venv, then the
// POSIX venv.
export const PYTHON = process.env.TESSERACT_UI_PYTHON
  || (existsSync(join(REPO, '.venv', 'Scripts', 'python.exe')) ? join(REPO, '.venv', 'Scripts', 'python.exe') : join(REPO, '.venv', 'bin', 'python'));
export const E1 = 'e1';
export const E2 = 'e2';

// The two store / bus primitives of core.js (same semantics, no DOM).
export function makeT() {
  const bus = new Map(), state = Object.create(null), subs = new Map();
  const T = { events: [] };
  T.on = (e, fn) => { let s = bus.get(e); if (!s) bus.set(e, s = new Set()); s.add(fn); return () => s.delete(fn); };
  T.emit = (e, p) => {
    T.events.push([e, p]);
    const s = bus.get(e); if (!s) return;
    for (const fn of [...s]) fn(p);
  };
  T.store = {
    get: k => state[k],
    set(k, v) { const prev = state[k]; if (Object.is(prev, v)) return; state[k] = v; const s = subs.get(k); if (s) for (const fn of [...s]) fn(v, prev); },
    patch(o) { for (const k in o) T.store.set(k, o[k]); },
    update(k, fn) { T.store.set(k, fn(state[k])); },
    sub(k, fn) { let s = subs.get(k); if (!s) subs.set(k, s = new Set()); s.add(fn); return () => s.delete(fn); },
    snapshot: () => Object.assign({}, state),
  };
  T.named = name => T.events.filter(([e]) => e === name).map(([, p]) => p);
  return T;
}

export const flush = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

export const P1_TOPICS = ['service.state', 'route.published', 'tiers.summary', 'traffic.tick', 'traffic.history', 'logs.batch', 'core.info', 'jobs'];
export const P1_COMMANDS = ['service.start', 'service.stop', 'service.restart', 'app.quit'];

export function hello({ epoch = E1, commands = P1_COMMANDS, topics = P1_TOPICS, features = [], lang = 'zh_CN' } = {}) {
  return { t: 'hello', epoch, produced_at_ms: 1, sent_at_ms: 2, proto: 1, app_version: '0.1.0', runtime_pid: 4321, lang,
    caps: { commands, topics, features }, scope: { kind: 'custom', id: 'test' },
    network_recovery: { proxy: { state: 'not_needed', error_code: null, remaining_count: 0 }, firewall: { state: 'not_needed', error_code: null, remaining_count: 0 } } };
}

export function evt(topic, rev, kind, data, extra = {}) {
  return { t: 'evt', epoch: E1, produced_at_ms: 1, sent_at_ms: 2, topic, rev, kind, data, ...extra };
}

export function svc({ phase = 'running', stop = false, validation = false, gen = 4, uptime = 120, core = null, fresh = false } = {}) {
  const coreRunning = core == null ? phase === 'running' : core;
  return {
    intent: { desired_running: phase !== 'stopped', stop_requested: stop, lifecycle_revision: 1, runtime_generation: gen },
    runtime: { runtime_running: phase !== 'stopped', startup_phase: phase, shutdown_cleanup_pending: false, failure_code: null },
    core: { running: coreRunning, pid: coreRunning ? 7 : null, process_generation: 1, core_generation: 3, cleanup_pending: false },
    readiness: { state: 'ready', validation_active: validation, reason: '', remaining_ms: null, publication_revision: 1, lifecycle_revision: 1, route_epoch: 1, network_context_epoch: 1 },
    freshness: { sampled_at_ms: 1, snapshot_revision: 1, stale: fresh, uptime_s: phase === 'running' ? uptime : null },
  };
}

export function tierSummary({ carrier = 'vanguard', fallback = false, degraded = false, order = ['vanguard', 'sentinel', 'bastion', 'gateway'], tiers } = {}) {
  return { carrier, offline_reason: null, order, degraded, fallback,
    tiers: tiers || { vanguard: { enabled: true, total: 3, usable: 2 }, sentinel: { enabled: true, total: 2, usable: 2 }, bastion: { enabled: true, total: 40, usable: 0 }, gateway: { enabled: false, total: 0, usable: 0 } },
    pool: { usable: 4, total: 5 } };
}

export function job(op, name, state, extra = {}) {
  return { op, name, state, started_at_ms: null, progress: null, result: null, error: null, ...extra };
}

// T.rt on a FakeTransport with a fake clock. `transport` lets a test drive frames by hand:
// rt.deliver(frame) pushes one frame in order.
export function boot({ scenario = { steps: [] }, enabledFeatures = [], T = makeT(), ids = createIds({ conn: 'abc' }) } = {}) {
  const clock = createFakeClock(1000);
  T.clock = clock;
  const rt = createRt({ T, clock, ids, enabledFeatures });
  T.rt = rt;
  const fake = new FakeTransport({ scenario, clock });
  rt.connect(fake);
  return { T, rt, fake, clock };
}

// Connected and live: rt:reset(reconnect) + hello (+ optional frames).
export async function live(opts = {}, frames = []) {
  const b = boot(opts);
  await flush();
  b.fake.deliver({ t: 'rt:reset', epoch: opts.epoch || E1, reason: 'reconnect' });
  b.fake.deliver(hello({ epoch: opts.epoch || E1, ...(opts.hello || {}) }));
  for (const f of frames) b.fake.deliver(f);
  return b;
}

export function loadScenario(name) { return JSON.parse(readFileSync(join(SCENARIOS, name + '.json'), 'utf8')); }

export function python(args, opts = {}) {
  const r = spawnSync(PYTHON, ['-B', ...args], { cwd: REPO, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, ...opts });
  if (r.error) throw r.error;
  return r;
}
