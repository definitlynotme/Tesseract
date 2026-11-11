// T-P4-05a: the overview's derivations (src/rt/project_overview.js; mapping_overview sections 3
// and 4, PROTOCOL sections 5, 11, 15, 16; rulings 4th wave 02a-3 / 02b-8, 5th wave 02b-4..02b-8).
//
// Pure checks, no DOM: the deck (three fallback tiers, gateway never a card, the runtime order's
// carrier first), the hero (readiness seven states and validation_active, the kill switch's
// promise, v1 degraded is not "latency rising" (R2), B-01 only when the page passes it), the run
// pill (failed / cleanup pending keeps 停止 / stop failed / cancelling), deck cards and pills
// (Bastion's denominator is capacity), the Gateway row, the route line (no node name while
// stopped), the network-mode hint (drift, G08) and mode.set outcomes, the pool (pending only when
// the field is present), the exit card (seven IdentityResult states, partial, fresh=false,
// previous always "last", conflict / rotating hide the address, one family only, trigger vs own
// request ids, refresh disabled while a manual request runs, G09 auto lookup off), the protocol
// mix (D6 source, "other" tail, G33) and the chart helpers (empty / one-point series, gaps,
// 24 h buckets).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as P from '../../src/rt/project_overview.js';

const TIERS = {
  vanguard: { enabled: true, ok: 2, total: 3 },
  sentinel: { enabled: true, ok: 1, total: 2 },
  bastion: { enabled: true, ok: 25, total: 40 },
  gateway: { enabled: true, ok: 2, total: 2 },
};

/* ------------------------------------------------------------------ deck order */

test('deckOrder: the runtime order (four ids, carrier first) keeps three fallback tiers; gateway never enters the deck', () => {
  assert.deepEqual(P.deckOrder(['vanguard', 'sentinel', 'bastion', 'gateway']), ['vanguard', 'sentinel', 'bastion']);
  assert.deepEqual(P.deckOrder(['sentinel', 'vanguard', 'bastion', 'gateway']), ['sentinel', 'vanguard', 'bastion']);
  assert.deepEqual(P.deckOrder(['gateway', 'bastion', 'vanguard', 'sentinel']), ['bastion', 'vanguard', 'sentinel']);
  // missing ids are appended, unknown ids and duplicates dropped, a non-array is the default
  assert.deepEqual(P.deckOrder(['bastion', 'bastion', 'x']), ['bastion', 'vanguard', 'sentinel']);
  assert.deepEqual(P.deckOrder(null), ['vanguard', 'sentinel', 'bastion']);
  assert.ok(!P.DECK.includes('gateway'));
});

test('chainOf / firstTier / leftBehind follow the enabled fallback tiers', () => {
  assert.deepEqual(P.chainOf(TIERS), ['vanguard', 'sentinel', 'bastion']);
  assert.deepEqual(P.chainOf({ ...TIERS, vanguard: { enabled: false } }), ['sentinel', 'bastion']);
  assert.deepEqual(P.chainOf({}), ['vanguard']);
  assert.equal(P.firstTier({ ...TIERS, vanguard: { enabled: false } }), 'sentinel');
  assert.equal(P.leftBehind('sentinel', TIERS), 'vanguard');
  assert.equal(P.leftBehind('bastion', { ...TIERS, vanguard: { enabled: false } }), 'sentinel');
});

test('dotsOf: one dot per node up to DOT_MAX, a meter beyond (R12); counts clamp', () => {
  assert.deepEqual(P.dotsOf(3, 2), { n: 3, ok: 2, style: '' });
  assert.deepEqual(P.dotsOf(3, 9, 'off'), { n: 3, ok: 3, style: 'off' });
  assert.deepEqual(P.dotsOf(-1, 'x'), { n: 0, ok: 0, style: '' });
  const m = P.dotsOf(P.DOT_MAX + 16, 10);
  assert.equal(m.meter, true); assert.equal(m.v, 10 / (P.DOT_MAX + 16));
});

/* ------------------------------------------------------------------ hero / run */

test('readinessKind: the seven readiness states and validation_active (G06)', () => {
  const k = (state, validation_active = false) => P.readinessKind({ state, validation_active });
  assert.equal(k('serving'), 'ready');
  assert.equal(k('ready'), 'ready');
  assert.equal(k('ready', true), 'validating');
  assert.equal(k('serving', true), 'validating');
  assert.equal(k('activation_hold'), 'hold');
  assert.equal(k('controller_hold'), 'hold');
  assert.equal(k('facility_hold'), 'hold');
  assert.equal(k('data_plane_failed'), 'data_plane_failed');
  assert.equal(k('fail_closed', true), 'fail_closed');
  assert.equal(k('something_new'), null);
  assert.equal(P.readinessKind(null), null);
});

const hero = over => P.heroOf({ run: 'running', carrier: 'vanguard', tiers: TIERS, killswitch: 'armed', readiness: { state: 'serving', validation_active: false }, ...over });

test('heroOf: running and ready; v1 degraded (some carrier nodes unusable) is not a warning (R2)', () => {
  const h = hero();
  assert.deepEqual(h.name, { kind: 'tier', tier: 'vanguard' });
  assert.equal(h.dot, 'ok');
  assert.deepEqual(h.hint, { kind: 'nodes', count: 'partial', ok: 2, total: 3, ks: 'none' });
  // all usable
  assert.equal(hero({ tiers: { ...TIERS, vanguard: { enabled: true, ok: 3, total: 3 } } }).hint.count, 'all');
  // the latency-rising words only come through the B-01 branch's slow input
  assert.equal(hero({ slow: true }).hint.kind, 'slow');
  assert.equal(hero({ slow: true }).dot, 'warn');
});

test('heroOf: readiness states, the kill switch promise and fallback', () => {
  assert.equal(hero({ readiness: { state: 'ready', validation_active: true } }).hint.kind, 'validating');
  assert.equal(hero({ readiness: { state: 'ready', validation_active: true } }).dot, 'busy');
  for (const s of ['activation_hold', 'controller_hold', 'facility_hold']) assert.deepEqual([hero({ readiness: { state: s } }).hint.kind, hero({ readiness: { state: s } }).dot], ['hold', 'warn']);
  assert.deepEqual([hero({ readiness: { state: 'data_plane_failed' } }).hint.kind, hero({ readiness: { state: 'data_plane_failed' } }).dot], ['data_plane_failed', 'err']);
  const fc = hero({ readiness: { state: 'fail_closed' } });
  assert.deepEqual([fc.name, fc.hint.kind, fc.dot], [{ kind: 'state', state: 'blocked' }, 'blocked', 'err']);
  // the kill switch fired: the hero says outbound is blocked (triggered, T-P4-C1 item 5)
  const tr = hero({ killswitch: 'triggered' });
  assert.deepEqual([tr.name.state, tr.hint.kind, tr.dot], ['blocked', 'blocked', 'err']);
  // tiers.summary.offline_reason fail_closed reads the same
  assert.equal(hero({ offline: 'fail_closed' }).hint.kind, 'blocked');
  // off / not in effect / error: a suffix and an amber dot
  for (const [ks, suffix] of [['disabled', 'off'], ['blocked', 'blocked'], ['error', 'error']]) {
    const h = hero({ killswitch: ks });
    assert.equal(h.hint.ks, suffix); assert.equal(h.dot, 'warn');
  }
  assert.equal(hero({ killswitch: 'inactive' }).dot, 'ok');
  // a fallback names the tier it left behind
  const fb = hero({ carrier: 'sentinel', fallback: true });
  assert.deepEqual(fb.hint, { kind: 'fallback', tier: 'vanguard', ks: 'none' });
  assert.equal(fb.dot, 'warn');
  // no carrier yet: unconfirmed
  assert.deepEqual(hero({ carrier: null }).name, { kind: 'state', state: 'unconfirmed' });
  assert.equal(hero({ carrier: null }).hint.kind, 'unconfirmed');
  // stale service.state: grey dot, 数据未更新, last carrier kept
  assert.deepEqual([hero({ stale: true }).hint.kind, hero({ stale: true }).dot, hero({ stale: true }).name.tier], ['stale', 'off', 'vanguard']);
});

test('heroOf: not running (connecting, starting phases, cancelling, stopping, stopped, failed, superseded)', () => {
  const h = over => P.heroOf({ tiers: TIERS, ...over });
  assert.deepEqual(h({ run: 'unknown' }).hint, { kind: 'connecting', again: false });
  assert.deepEqual(h({ run: undefined, again: true }).hint, { kind: 'connecting', again: true });
  // phase is Math.max(0, phase) (4th wave 02a-3)
  assert.deepEqual(h({ run: 'starting', phase: -1 }).hint, { kind: 'phase', phase: 0 });
  assert.deepEqual(h({ run: 'starting', phase: 2 }).hint, { kind: 'phase', phase: 2 });
  // cancelling reads as cancelling (5th wave 02b-5)
  const c = h({ run: 'starting', cancelling: true, phase: 1 });
  assert.deepEqual([c.name.state, c.hint, c.dot], ['cancelling', { kind: 'stopping', cancelling: true }, 'busy']);
  assert.deepEqual(h({ run: 'stopping' }).hint, { kind: 'stopping', cancelling: false });
  assert.deepEqual(h({ run: 'stopped' }).hint, { kind: 'stopped', tier: 'vanguard' });
  assert.equal(h({ run: 'stopped', tiers: { ...TIERS, vanguard: { enabled: false } } }).hint.tier, 'sentinel');
  const failure = { code: 'runtime_listener_conflict', message_key: 'errors.start.listener_conflict' };
  const f = h({ run: 'failed', failure });
  assert.deepEqual([f.name.state, f.dot, f.hint], ['failed', 'err', { kind: 'failed', failure }]);
  // a stop that superseded a start shows as stopped (S-R3)
  assert.equal(h({ run: 'failed', failure: { code: 'superseded' } }).name.state, 'stopped');
  // a refused command's note
  assert.deepEqual(h({ run: 'stopped', note: { text: 'x' } }).hint, { kind: 'note', note: { text: 'x' } });
});

test('heroOf: a failed stop (G05) wins in every run state it can leave behind', () => {
  const err = { code: 'timeout' };
  for (const run of ['running', 'stopping', 'failed']) {
    const h = P.heroOf({ run, carrier: 'vanguard', tiers: TIERS, stopFailed: true, stopError: err });
    assert.deepEqual([h.hint.kind, h.hint.error, h.dot], ['stop_failed', err, 'err'], run);
  }
  assert.equal(P.heroOf({ run: 'running', carrier: 'vanguard', tiers: TIERS, restarting: true }).hint.kind, 'restarting');
  assert.equal(P.healthOf({ run: 'stopped' }), 'off');
  assert.equal(P.healthOf({ run: 'starting' }), 'busy');
  assert.equal(P.healthOf({ run: 'running', carrier: 'vanguard', tiers: TIERS, killswitch: 'armed', readiness: { state: 'serving' } }), 'ok');
});

test('pillMode: connecting, start, starting / cancelling, failed, cleanup pending keeps 停止, stop failed', () => {
  assert.equal(P.pillMode({ run: 'unknown' }), 'connecting');
  assert.equal(P.pillMode({ run: 'stopped' }), 'start');
  assert.equal(P.pillMode({ run: 'starting' }), 'starting');
  assert.equal(P.pillMode({ run: 'starting', cancelling: true }), 'cancelling');
  assert.equal(P.pillMode({ run: 'running' }), 'running');
  assert.equal(P.pillMode({ run: 'stopping' }), 'stopping');
  assert.equal(P.pillMode({ run: 'failed', core: { running: false, cleanup_pending: false } }), 'failed');
  assert.equal(P.pillMode({ run: 'failed', core: { running: false, cleanup_pending: true } }), 'failed_stop');
  assert.equal(P.pillMode({ run: 'failed', core: { running: true } }), 'failed_stop');
  assert.equal(P.pillMode({ run: 'failed', failure: { reason_code: 'superseded' }, core: {} }), 'start');
  assert.equal(P.pillMode({ run: 'failed', failure: { code: 'superseded' }, core: { cleanup_pending: true } }), 'failed_stop');
  for (const run of ['running', 'stopping', 'failed']) assert.equal(P.pillMode({ run, stopFailed: true }), 'stop_failed', run);
  assert.equal(P.pillMode({ run: 'stopped', stopFailed: true }), 'start');
});

test('ksKind maps every projected kill switch word; anything else is unknown', () => {
  assert.deepEqual(['disabled', 'blocked', 'error', 'triggered', 'armed', 'inactive', null, 'x'].map(P.ksKind),
    ['off', 'blocked', 'error', 'triggered', 'armed', 'inactive', 'unknown', 'unknown']);
});

/* ------------------------------------------------------------------ deck */

const items = [
  { name: 'HK-01', protocol: 'vless', latency_ms: 42, status: 'healthy' },
  { name: 'HK-02', protocol: 'trojan', latency_ms: null, status: 'unhealthy', reason_code: 'timeout' },
];

test('currentNode: the route chain member found in that tier\'s nodes.list; leaf only for the managed tier', () => {
  const route = { selected_chain: ['EXIT-SELECT', 'HK-01'], leaf: 'HK-01', managed_tier: 'vanguard', valid: true };
  assert.equal(P.currentNode(route, items, 'vanguard').name, 'HK-01');
  assert.equal(P.currentNode({ ...route, selected_chain: ['X'], leaf: 'L' }, items, 'vanguard').name, 'L');
  assert.equal(P.currentNode({ ...route, selected_chain: ['X'], leaf: 'L' }, items, 'sentinel'), null);
  assert.equal(P.currentNode(null, items, 'vanguard'), null);
  assert.equal(P.currentNode({ leaf: 'HK-01' }, items, 'vanguard'), null);
});

test('downReason: the most common reason of the unusable rows, mixed when no majority (E15)', () => {
  assert.deepEqual(P.downReason(items), { code: 'timeout', mixed: false });
  assert.deepEqual(P.downReason([{ status: 'unhealthy', reason_code: 'a' }, { status: 'unhealthy', reason_code: 'b' }]), { code: 'a', mixed: true });
  assert.deepEqual(P.downReason([{ status: 'healthy' }, { status: 'disabled', reason_code: 'x' }]), { code: null, mixed: false });
  assert.deepEqual(P.downReason(null), { code: null, mixed: false });
});

test('deckStat: running, starting, stopped, unknown and off tiers', () => {
  const st = (tier, over = {}) => P.deckStat({ tier, run: 'running', carrier: 'vanguard', tiers: TIERS, collected: {}, bastion: { ok: 30, capacity: 60, min: 20 }, ...over });
  assert.deepEqual(st('vanguard').stat, { kind: 'usable', ok: 2, total: 3 });
  assert.deepEqual(st('sentinel').stat, { kind: 'standby', ok: 1, total: 2 });
  assert.deepEqual(st('vanguard', { tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 3 } } }).stat, { kind: 'down', total: 3 });
  assert.equal(st('vanguard', { tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 3 } } }).tone, 'err');
  assert.deepEqual(st('vanguard', { tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 0 } } }).stat, { kind: 'unconfigured' });
  assert.deepEqual(st('sentinel', { tiers: { ...TIERS, sentinel: { enabled: false, ok: 0, total: 2 } } }).stat, { kind: 'off' });
  assert.deepEqual(st('vanguard', { run: 'unknown' }).stat, { kind: 'unknown' });
  assert.deepEqual(st('vanguard', { summaryKnown: false }).stat, { kind: 'unknown' });
  assert.deepEqual(st('vanguard', { run: 'stopped' }).stat, { kind: 'nodes', total: 3 });
  // starting: collecting until the tier is collected; the deck's front reads as usable
  assert.deepEqual(st('vanguard', { run: 'starting', carrier: null }).stat, { kind: 'collecting' });
  assert.deepEqual(st('vanguard', { run: 'starting', carrier: null, front: 'vanguard', collected: { vanguard: true } }).stat, { kind: 'usable', ok: 2, total: 3 });
  assert.deepEqual(st('sentinel', { run: 'starting', carrier: null, front: 'vanguard', collected: { sentinel: true } }).stat, { kind: 'standby', ok: 1, total: 2 });
});

test('deckStat: Bastion\'s denominator is capacity (D9; 5th wave 02b-6), never total', () => {
  const st = over => P.deckStat({ tier: 'bastion', run: 'running', carrier: 'vanguard', tiers: TIERS, collected: {}, bastion: { ok: 30, capacity: 60, min: 20 }, ...over });
  const s = st();
  assert.deepEqual(s.stat, { kind: 'bastion_standby', n: 30, capacity: 60, min: 20 });
  assert.equal(s.dots.meter, true); assert.equal(s.dots.v, 0.5);
  assert.deepEqual(st({ bastion: { ok: 10, capacity: 60, min: 20 } }).stat.kind, 'bastion_low');
  assert.equal(st({ bastion: { ok: 10, capacity: 60, min: 20 } }).tone, 'warn');
  assert.equal(st({ bastion: { ok: 10, capacity: 60, min: 20, collecting: true } }).stat.kind, 'bastion_filling');
  assert.equal(st({ carrier: 'bastion', bastion: { ok: 30, capacity: 60, min: 20, collecting: true } }).dots.style, 'warn');
  assert.deepEqual(st({ tiers: { ...TIERS, bastion: { enabled: false } } }).stat, { kind: 'bastion_off' });
  assert.deepEqual(st({ bastionKnown: false }).stat, { kind: 'bastion_unknown' });
  assert.deepEqual(st({ bastion: { ok: 0, capacity: 0, min: 0 } }).stat, { kind: 'bastion_unknown' });
  // stopped: no baseline (bastion.pool has none while stopped) -> unknown; a known capacity is shown
  assert.deepEqual(st({ run: 'stopped', bastionKnown: false }).stat, { kind: 'bastion_unknown' });
  assert.deepEqual(st({ run: 'stopped' }).stat, { kind: 'bastion_capacity', capacity: 60 });
  assert.deepEqual(st({ run: 'starting' }).stat, { kind: 'collecting' });
  assert.deepEqual(st({ run: 'starting', collected: { bastion: true } }).stat, { kind: 'bastion_after_ready' });
});

test('deckPill: carrying / fallback / down / standby; cancelling and stopping while not running; slow only from B-01', () => {
  const pill = (tier, over = {}) => P.deckPill({ tier, run: 'running', carrier: 'vanguard', tiers: TIERS, ...over });
  assert.equal(pill('vanguard'), 'carrying');
  assert.equal(pill('sentinel'), 'standby');
  assert.equal(pill('sentinel', { carrier: 'sentinel' }), 'fallback');
  assert.equal(pill('bastion', { carrier: 'bastion' }), 'fallback');
  assert.equal(pill('vanguard', { carrier: 'sentinel', tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 3 } } }), 'down');
  assert.equal(pill('vanguard', { slow: true }), 'slow');
  assert.equal(pill('sentinel', { slow: true }), 'standby');
  assert.equal(pill('vanguard', { run: 'starting' }), 'starting');
  assert.equal(pill('vanguard', { run: 'starting', cancelling: true }), 'cancelling');
  assert.equal(pill('vanguard', { run: 'stopping' }), 'stopping');
  assert.equal(pill('vanguard', { run: 'failed' }), 'stopped');
  assert.equal(pill('vanguard', { run: 'unknown' }), 'unknown');
});

test('gatewayRow: off, waiting / verifying while starting, missing, pending, current, stopped', () => {
  const gw = { selected: 'GW-Tokyo', effective: 'GW-Tokyo', applied: true, missing: false };
  const row = over => P.gatewayRow({ run: 'running', tiers: TIERS, collected: {}, phase: 2, gateway: gw, ...over });
  assert.deepEqual([row().kind, row().name], ['current', 'GW-Tokyo']);
  assert.equal(row({ run: 'unknown' }).kind, 'unknown');
  assert.equal(row({ tiers: { ...TIERS, gateway: { enabled: false } } }).kind, 'off');
  assert.equal(row({ gateway: { selected: null, effective: null } }).kind, 'off');
  assert.equal(row({ gateway: { ...gw, missing: true } }).kind, 'missing');
  assert.equal(row({ gateway: { ...gw, missing: true } }).tone, 'warn');
  assert.equal(row({ gateway: { ...gw, applied: false, pending: { selected: 'GW-2' } } }).kind, 'pending');
  assert.equal(row({ run: 'starting', phase: 0 }).kind, 'waiting');
  assert.equal(row({ run: 'starting', phase: 1 }).kind, 'verifying');
  assert.equal(row({ run: 'starting', phase: -3 }).kind, 'waiting');
  assert.deepEqual([row({ run: 'stopped' }).kind, row({ run: 'stopped' }).name], ['stopped', 'GW-Tokyo']);
  assert.equal(row({ run: 'stopped', gateway: null }).kind, 'unknown');
  assert.equal(row({ gateway: { selected: 'GW-Tokyo', effective: null, applied: true } }).name, 'GW-Tokyo');
});

test('routeLine: via / direct / unconfirmed while running; no node name while stopped (D17)', () => {
  const route = { selected_chain: ['HK-01'], leaf: 'HK-01', managed_tier: 'vanguard', valid: true };
  const gw = { selected: 'GW-Tokyo', effective: 'GW-Tokyo' };
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'vanguard', route, items, gateway: gw, tiers: TIERS }), { kind: 'via', via: 'HK-01', viaTier: 'vanguard', exit: 'GW-Tokyo' });
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'vanguard', route, items, gateway: { selected: null, effective: null }, tiers: TIERS }), { kind: 'direct', via: 'HK-01', viaTier: 'vanguard' });
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'vanguard', route: { ...route, valid: false }, items, gateway: gw, tiers: TIERS }), { kind: 'unconfirmed' });
  assert.deepEqual(P.routeLine({ run: 'running', carrier: null, route, items, gateway: gw, tiers: TIERS }), { kind: 'unconfirmed' });
  const stopped = P.routeLine({ run: 'stopped', carrier: null, route, items, gateway: gw, tiers: TIERS });
  assert.deepEqual(stopped, { kind: 'plan', tier: 'vanguard', gateway: true });
  assert.ok(!JSON.stringify(stopped).includes('HK-01') && !JSON.stringify(stopped).includes('GW-Tokyo'));
  assert.equal(P.routeLine({ run: 'stopped', tiers: { ...TIERS, gateway: { enabled: false } } }).gateway, false);
  assert.equal(P.routeLine({ run: 'stopped', gateway: { selected: null }, tiers: TIERS }).gateway, false);
});

/* ------------------------------------------------------------------ strip */

test('mixedListener: app.info listeners[kind=mixed] as host:port; IPv6 hosts bracketed; none -> null', () => {
  assert.equal(P.mixedListener([{ kind: 'controller', host: '127.0.0.1', port: 9090 }, { kind: 'mixed', host: '127.0.0.1', port: 7890 }]), '127.0.0.1:7890');
  assert.equal(P.mixedListener([{ kind: 'mixed', host: '::1', port: 7890 }]), '[::1]:7890');
  assert.equal(P.mixedListener([{ kind: 'mixed', host: '127.0.0.1', port: null }]), null);
  assert.equal(P.mixedListener(undefined), null);
});

test('modeHint: drift (G08), proxy unknown, gated, per-mode states', () => {
  const h = over => P.modeHint({ run: 'running', mode: 'system', modeKnown: true, admin: true, gatedModes: ['tun', 'mixed'], systemProxy: { proxy_state: 'owned', drift: false }, ...over });
  assert.deepEqual(h(), { kind: 'system', state: 'running' });
  assert.deepEqual(h({ systemProxy: { proxy_state: 'owned', drift: true } }), { kind: 'drift' });
  assert.deepEqual(h({ systemProxy: { proxy_state: 'other' } }), { kind: 'drift' });
  assert.deepEqual(h({ mode: 'mixed', systemProxy: { proxy_state: 'other' } }), { kind: 'drift' });
  // TUN does not use the system proxy: no drift there
  assert.deepEqual(h({ mode: 'tun', systemProxy: { drift: true } }), { kind: 'tun', state: 'running' });
  // stopped: a drift is not reported (Tesseract does not own the proxy then)
  assert.deepEqual(h({ run: 'stopped', systemProxy: { drift: true } }), { kind: 'system', state: 'other' });
  assert.deepEqual(h({ systemProxy: { proxy_state: 'unknown' } }), { kind: 'proxy_unknown' });
  assert.deepEqual(h({ systemProxy: { proxy_state: 'pending' } }), { kind: 'system', state: 'starting' });
  assert.deepEqual(h({ mode: 'tun', admin: false }), { kind: 'gated' });
  assert.deepEqual(h({ modeKnown: false }), { kind: 'unknown' });
  assert.deepEqual(h({ run: 'starting', mode: 'mixed' }), { kind: 'mixed', state: 'starting' });
});

test('modeOutcome: next start, admin_required (also while stopped), cleanup_pending, saved but not applied, quiet endings', () => {
  assert.deepEqual(P.modeOutcome({ ok: true, result: { saved: true, applied: true }, run: 'stopped' }), { kind: 'next_start' });
  assert.equal(P.modeOutcome({ ok: true, result: { saved: true, applied: true }, run: 'running' }), null);
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'admin_required' }, run: 'stopped' }), { kind: 'admin_required' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'not_allowed', reason_code: 'admin_required' } }), { kind: 'admin_required' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'lifecycle_busy', reason_code: 'cleanup_pending' } }), { kind: 'cleanup_pending' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'io' }, cleanupPending: true }), { kind: 'cleanup_pending' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'io', message_key: 'errors.mode.cleanup_pending' } }), { kind: 'cleanup_pending' });
  const apply = { code: 'io', message_key: 'errors.mode.apply_failed' };
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'io', result: { saved: true, applied: false, apply_error: apply } } }), { kind: 'not_applied', error: apply });
  const bare = { code: 'io', result: { saved: true, applied: false } };
  assert.deepEqual(P.modeOutcome({ ok: false, error: bare }), { kind: 'not_applied', error: bare });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'conflict' } }), { kind: 'conflict' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'timeout' } }), { kind: 'failed', error: { code: 'timeout' } });
  for (const code of ['cancelled', 'superseded', 'runtime_gone', 'owner_revoked']) assert.equal(P.modeOutcome({ ok: false, error: { code } }), null, code);
});

test('poolCell: known only while running with a summary; pending only when a tier carries the field (C2 item 7)', () => {
  const summary = { tiers: { vanguard: { pending: 2 }, sentinel: { pending: 1 }, gateway: {}, bastion: { pending: 9 } } };
  assert.deepEqual(P.poolCell({ run: 'running', pool: { ok: 5, total: 7 }, summary, stale: false }), { known: true, ok: 5, total: 7, rate: 5 / 7, pending: 3 });
  assert.deepEqual(P.poolCell({ run: 'running', pool: { ok: 5, total: 7 }, summary: { tiers: { vanguard: {} } }, stale: false }).pending, 0);
  assert.equal(P.poolCell({ run: 'running', pool: { ok: 0, total: 0 }, summary, stale: false }).rate, null);
  assert.deepEqual(P.poolCell({ run: 'running', pool: { ok: 5, total: 7 }, summary: null }), { known: false, state: 'other' });
  assert.deepEqual(P.poolCell({ run: 'running', pool: { ok: 5, total: 7 }, summary, stale: true }), { known: false, state: 'other' });
  assert.deepEqual(P.poolCell({ run: 'starting', pool: null, summary }), { known: false, state: 'starting' });
  assert.deepEqual(P.poolCell({ run: 'stopped', pool: null, summary }), { known: false, state: 'other' });
});

/* ------------------------------------------------------------------ exit card */

const OBS = 1790654280000;
const leg = (address, country = 'JP', extra = {}) => ({ status: 'ok', address, country, reason_code: null, observed_at_ms: OBS, ...extra });
const fail = reason => ({ status: 'failed', address: null, country: null, reason_code: reason, observed_at_ms: null });
const result = (status, v4, v6, over = {}) => ({
  request_id: 'r-1', generation: 3, binding_id: 'b-1', status, reason_code: null, v4, v6, fresh: true, observed_at_ms: OBS,
  native_fallback_verified: false, native_fallback_verified_commit_rejected: false, native_fallback_unverified_budget: false, manual_execution_binding_stale: false,
  ...over,
});
const OK = result('ok', leg('203.0.113.24'), leg('2001:db8::24'), { trigger: 'auto' });
const card = (current, previous = null, over = {}) => P.exitCard({ run: 'running', phase: 2, identity: { current, previous }, auto: true, canVerify: true, ...over });

test('exitCard: ok (已确认 by trigger auto, 已验证 by trigger manual), the IPv6 location', () => {
  const c = card(OK);
  assert.equal(c.show, 'addr');
  assert.equal(c.legs.v4.address, '203.0.113.24'); assert.equal(c.legs.v6.address, '2001:db8::24');
  assert.equal(c.last, false);
  assert.deepEqual(c.badge, { kind: 'ok', how: 'auto', at: OBS });
  assert.equal(c.same, true);
  assert.deepEqual(c.refresh, { shown: true, enabled: true });
  assert.equal(card({ ...OK, trigger: 'manual' }).badge.how, 'manual');
  // a different IPv6 country: its own location line
  assert.equal(card(result('ok', leg('203.0.113.24'), leg('2001:db8::24', 'US'))).same, false);
  // C2 fields pass through when present, null when absent (never inferred)
  const c2 = card(result('ok', leg('203.0.113.24', 'JP', { city: 'Tokyo', timezone: 'Asia/Tokyo', asn: '64500', org: 'Example', type: 'hosting', single_source: true }), fail('identity_unavailable'), { status: 'partial' }));
  assert.deepEqual([c2.legs.v4.city, c2.legs.v4.timezone, c2.legs.v4.asn, c2.legs.v4.org, c2.legs.v4.type, c2.legs.v4.single], ['Tokyo', 'Asia/Tokyo', '64500', 'Example', 'hosting', true]);
  assert.deepEqual([c.legs.v4.city, c.legs.v4.asn, c.legs.v4.single], [null, null, false]);
});

test('exitCard: trigger absent (frozen sidecar): this window\'s request ids decide (R3)', () => {
  const noTrigger = { ...OK }; delete noTrigger.trigger;
  assert.equal(card(noTrigger).badge.how, 'auto');
  assert.equal(card(noTrigger, null, { ownIds: new Set(['r-1']) }).badge.how, 'manual');
  assert.equal(card(noTrigger, null, { curHow: 'manual' }).badge.how, 'manual');
  // trigger present wins over a guess
  assert.equal(card({ ...OK, trigger: 'auto' }, null, { ownIds: new Set(['r-1']), curHow: 'manual' }).badge.how, 'auto');
  assert.equal(P.exitHow({ trigger: 'recheck' }), 'auto');
  assert.equal(P.exitHow(null), 'auto');
});

test('exitCard: partial and a single address family; the failed leg keeps its reason, no fake address', () => {
  const p = card(result('partial', leg('203.0.113.24'), fail('identity_timeout'), { trigger: 'auto' }));
  assert.equal(p.show, 'addr');
  assert.deepEqual([p.legs.v6.status, p.legs.v6.address, p.legs.v6.reason], ['failed', null, 'identity_timeout']);
  assert.equal(p.badge.kind, 'ok');
  const v6 = card(result('partial', fail('identity_unavailable'), leg('2001:db8::24', 'JP'), { trigger: 'auto' }));
  assert.equal(v6.show, 'addr');
  assert.equal(v6.legs.v4.address, null); assert.equal(v6.legs.v4.reason, 'identity_unavailable');
  assert.equal(v6.same, true, 'IPv4 has no address: IPv6 is not "same as above"');
  // ok but no address at all: nothing to show
  assert.equal(card(result('ok', fail('x'), fail('y'))).show, 'msg');
});

test('exitCard: running (manual) disables refresh; the previous result stays, dimmed, as 上次结果', () => {
  const run = result('running', { status: 'running' }, { status: 'running' }, { request_id: 'm-2', reason_code: 'querying', trigger: 'manual' });
  const c = card(run, OK);
  assert.deepEqual(c.badge, { kind: 'running', how: 'manual', phase: 'querying' });
  assert.deepEqual([c.show, c.last, c.dim], ['addr', true, true]);
  assert.equal(c.refresh.enabled, false, 'S-E16: a second manual request is disabled while one runs');
  // no previous: the running message with its phase
  for (const phase of ['upgrading', 'retrying', 'rate_limited', 'waiting_connection', 'querying']) {
    const m = card({ ...run, reason_code: phase });
    assert.deepEqual([m.show, m.msg.kind, m.msg.phase, m.badge.phase], ['msg', 'running', phase, phase], phase);
  }
  assert.equal(card({ ...run, reason_code: 'unknown_phase' }).badge.phase, 'querying');
  // deferred while starting: 就绪后查询 (S-E4), the previous result is not shown over it
  const d = card({ ...run, reason_code: 'deferred' }, OK, { run: 'starting', phase: 1 });
  assert.deepEqual([d.show, d.msg.kind, d.msg.deferred, d.refresh.enabled], ['msg', 'starting', true, false]);
});

test('exitCard: failed shows the previous result as 上次结果, or the reason; route_changed keeps the last result', () => {
  const f = card(result('failed', fail('identity_timeout'), fail('identity_timeout'), { reason_code: 'identity_timeout' }), OK);
  assert.deepEqual(f.badge, { kind: 'failed', reason: 'identity_timeout' });
  assert.deepEqual([f.show, f.last], ['addr', true]);
  const n = card(result('failed', fail('exit_unreachable'), fail('exit_unreachable'), { reason_code: 'exit_unreachable' }));
  assert.deepEqual([n.show, n.msg], ['msg', { kind: 'failed', reason: 'exit_unreachable' }]);
  const ch = card(result('failed', fail('x'), fail('x'), { reason_code: 'route_changed' }), OK);
  assert.deepEqual([ch.badge.reason, ch.last], ['route_changed', true]);
  // failed without a reason: a Fallback flag, else unavailable
  assert.equal(card(result('failed', fail('x'), fail('x'), { manual_execution_binding_stale: true })).badge.reason, 'manual_execution_binding_stale');
  assert.equal(card(result('failed', fail('x'), fail('x'))).badge.reason, 'unavailable');
});

test('exitCard: conflict and rotating hide the address, even with a previous result (S-E10, D21)', () => {
  for (const reason of P.EXIT_HIDE) {
    const okHidden = card(result('ok', leg('203.0.113.24'), leg('2001:db8::24'), { reason_code: reason }));
    assert.deepEqual([okHidden.show, okHidden.legs, okHidden.msg], ['msg', null, { kind: 'hidden', reason }], reason);
    const failHidden = card(result('failed', fail(reason), fail(reason), { reason_code: reason }), OK);
    assert.deepEqual([failHidden.show, failHidden.legs, failHidden.msg.kind], ['msg', null, 'hidden'], reason);
  }
});

test('exitCard: expired (fresh=false / identity_stale), warnings keep the address, Fallback flags', () => {
  assert.equal(card({ ...OK, fresh: false }).badge.kind, 'expired');
  assert.equal(card({ ...OK, fresh: false }).show, 'addr');
  assert.equal(card({ ...OK, reason_code: 'identity_stale' }).badge.kind, 'expired');
  for (const reason of P.EXIT_WARN) {
    const w = card({ ...OK, reason_code: reason });
    assert.deepEqual([w.badge.kind, w.badge.reason, w.show], ['warn', reason, 'addr'], reason);
  }
  assert.deepEqual([card({ ...OK, native_fallback_verified: true }).badge.kind, card({ ...OK, native_fallback_verified: true }).badge.reason], ['info', 'native_fallback_verified']);
  for (const flag of P.EXIT_FLAGS.slice(1)) assert.equal(card({ ...OK, [flag]: true }).badge.kind, 'warn', flag);
  assert.deepEqual([card({ ...OK, reason_code: 'route_contract_failed' }).badge.kind, card({ ...OK, reason_code: 'route_contract_failed' }).show], ['warn', 'addr']);
});

test('exitCard: cancelled / superseded go quietly back to the previous result (S-E15); idle keeps it as expired', () => {
  for (const status of ['cancelled', 'superseded']) {
    const c = card(result(status, fail('x'), fail('x')), OK);
    assert.deepEqual([c.show, c.last, c.badge.kind], ['addr', true, 'ok'], status);
    const none = card(result(status, fail('x'), fail('x')), null);
    assert.deepEqual([none.show, none.msg.kind], ['msg', 'wait'], status);
    assert.equal(card(result(status, fail('x'), fail('x')), null, { auto: false }).msg.kind, 'auto_off');
  }
  const idle = card(result('idle', { status: 'unknown' }, { status: 'unknown' }), OK);
  assert.deepEqual([idle.show, idle.last, idle.badge.kind], ['addr', true, 'expired']);
});

test('exitCard: no result yet: wait (auto on, no endless spinner), auto_off (G09), starting; stopped, stopping, connecting', () => {
  assert.deepEqual(card(null).msg, { kind: 'wait' });
  assert.deepEqual(card(null).badge, { kind: 'idle', state: 'wait' });
  assert.deepEqual(card(null, null, { auto: false }).msg, { kind: 'auto_off' });
  assert.deepEqual(card(null, null, { auto: null }).msg, { kind: 'wait' });
  assert.deepEqual(card(null, null, { run: 'starting', phase: 1 }).msg, { kind: 'starting', phase: 1 });
  assert.deepEqual(card(null, null, { run: 'starting', phase: -1 }).msg, { kind: 'starting', phase: 0 });
  assert.equal(card(null, null, { run: 'starting' }).refresh.enabled, true, 'a click while starting is deferred by the sidecar (S-E4)');
  assert.deepEqual(card(OK, null, { run: 'stopped' }).msg, { kind: 'stopped' });
  assert.equal(card(OK, null, { run: 'stopped' }).show, 'msg', 'no address while stopped');
  assert.deepEqual(card(OK, null, { run: 'stopped' }).refresh, { shown: false, enabled: false });
  assert.deepEqual(card(OK, null, { run: 'stopping' }).msg, { kind: 'stopping' });
  assert.deepEqual(card(OK, null, { run: 'unknown' }).msg, { kind: 'connecting' });
  assert.equal(card(OK, null, { canVerify: false }).refresh.enabled, false, 'exit.verify not in hello.caps');
  // stale: 数据未更新, the last value kept
  const s = card(OK, null, { stale: true });
  assert.deepEqual([s.badge, s.show], [{ kind: 'stale' }, 'addr']);
});

/* ------------------------------------------------------------------ protocols */

test('protoMix: source (D6), entries = Σcounts (R5), sort by count then the fixed order, the tail beyond 6 is 其他 (G33)', () => {
  const m = P.protoMix({ source: 'loaded', counts: { vless: 4, trojan: 4, ss: 2, bad: -1, str: '3', zero: 0 } });
  assert.equal(m.source, 'loaded');
  assert.equal(m.entries, 10);
  assert.deepEqual(m.list.map(r => r.name), ['trojan', 'vless', 'ss']);
  assert.equal(P.protoMix({ source: 'configured', counts: {} }).source, 'configured');
  assert.equal(P.protoMix({ source: 'other', counts: {} }).source, null);
  assert.deepEqual(P.protoMix(null), { source: null, entries: 0, list: [] });
  const many = P.protoMix({ source: 'loaded', counts: { a: 9, b: 8, c: 7, d: 6, e: 5, f: 4, g: 3, h: 2 } });
  assert.equal(many.list.length, P.PROTO_MAX);
  assert.deepEqual(many.list.slice(0, 5).map(r => r.name), ['a', 'b', 'c', 'd', 'e']);
  assert.deepEqual(many.list[5], { name: null, other: true, count: 9, names: ['f', 'g', 'h'] });
  assert.equal(many.entries, 44);
});

/* ------------------------------------------------------------------ chart */

test('chart helpers: 0 or 1 point is not drawable (the empty state, no NaN); gaps break the line; ticks fold into 5-minute buckets', () => {
  assert.equal(P.drawable([]), false);
  assert.equal(P.drawable([{ t: 1 }]), false);
  assert.equal(P.drawable([{ t: 1 }, { t: NaN }]), false);
  assert.equal(P.drawable(null), false);
  assert.equal(P.drawable([{ t: 1 }, { t: 2 }]), true);
  assert.deepEqual(P.breaksOf([{ t: 0 }, { t: 1000 }, { t: 5000 }, { t: 6000 }], 1000), [2]);
  assert.deepEqual(P.breaksOf([], 1000), []);
  const h = [];
  P.foldTick(h, { t: 0, d: 10, u: 2 });
  P.foldTick(h, { t: 1000, d: 20, u: 4 });
  assert.equal(h.length, 1); assert.deepEqual([h[0].d, h[0].u, h[0].n], [15, 3, 2]);
  P.foldTick(h, { t: 700000, d: 1, u: 1 });
  assert.equal(h.length, 2); assert.equal(h[1].t, 600000, 'aligned to the bucket grid');
  P.foldTick(h, { t: 100, d: 99, u: 99 });
  assert.equal(h.length, 2, 'a sample older than the last bucket is ignored');
  P.foldTick(h, { t: NaN });
  assert.equal(h.length, 2);
  const cap = [];
  for (let i = 0; i < 5; i++) P.foldTick(cap, { t: i * 300000, d: i, u: i }, 300000, 3);
  assert.deepEqual(cap.map(b => b.t), [600000, 900000, 1200000]);
});

/* ------------------------------------------------- review 05a, fix round 1 */

// The runtime shape while Gateway is the exit (desktop_runtime tiers_summary_status /
// published_route_status / bridge_gateway_status): carrier 'gateway', order gateway first,
// fallback false, the route FINAL-EXIT -> EXIT-SELECT -> the Gateway node, effective = leaf.
const GW_ROUTE = { selected_chain: ['FINAL-EXIT', 'EXIT-SELECT', 'GW-Tokyo'], leaf: 'GW-Tokyo', managed_tier: 'gateway', valid: true };
const GW_CUR = { selected: 'GW-Tokyo', effective: 'GW-Tokyo', missing: false, applied: true, pending: null };

test('M1 heroOf: Gateway carries -> Gateway with its own hint (tier under it unconfirmed); never fallback, never Gateway\'s node count', () => {
  const h = hero({ carrier: 'gateway', fallback: false });
  assert.deepEqual(h.name, { kind: 'tier', tier: 'gateway' });
  assert.deepEqual(h.hint, { kind: 'gateway', ks: 'none' });
  assert.equal(h.dot, 'ok');
  assert.equal(hero({ carrier: 'gateway', killswitch: 'disabled' }).hint.ks, 'off');
  assert.equal(hero({ carrier: 'gateway', killswitch: 'disabled' }).dot, 'warn');
  // readiness and blocking still come first
  assert.equal(hero({ carrier: 'gateway', readiness: { state: 'data_plane_failed' } }).hint.kind, 'data_plane_failed');
  assert.equal(hero({ carrier: 'gateway', readiness: { state: 'serving', validation_active: true } }).hint.kind, 'validating');
  // the B-01 slow input does not turn Gateway into a carrier with rising latency
  assert.equal(hero({ carrier: 'gateway', slow: true }).hint.kind, 'gateway');
});

test('M1 deckStat / deckPill: Gateway carries -> the front card is unconfirmed (not 待命, not 承载流量); a down front stays down', () => {
  const st = (tier, over = {}) => P.deckStat({ tier, run: 'running', carrier: 'gateway', front: 'vanguard', tiers: TIERS, collected: {}, bastion: { ok: 30, capacity: 60, min: 20 }, ...over });
  assert.deepEqual(st('vanguard').stat, { kind: 'unconfirmed', ok: 2, total: 3 });
  assert.deepEqual(st('sentinel').stat, { kind: 'standby', ok: 1, total: 2 });
  assert.deepEqual(st('vanguard', { tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 3 } } }).stat, { kind: 'down', total: 3 });
  // the deck order of the runtime shape: Gateway first is dropped, Vanguard is the front
  assert.deepEqual(P.deckOrder(['gateway', 'vanguard', 'sentinel', 'bastion']), ['vanguard', 'sentinel', 'bastion']);
  const pill = (tier, over = {}) => P.deckPill({ tier, run: 'running', carrier: 'gateway', tiers: TIERS, ...over });
  assert.equal(pill('vanguard'), 'unconfirmed');
  assert.equal(pill('vanguard', { tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 3 } } }), 'down');
  assert.equal(pill('vanguard', { slow: true }), 'unconfirmed');
  assert.equal(pill('vanguard', { run: 'stopped' }), 'stopped');
});

test('M1 routeLine: Gateway carries -> {gateway, exit}, never 直接出口 with the Gateway node as the via', () => {
  const gwItems = [{ name: 'GW-Tokyo', protocol: 'vless', latency_ms: 80 }];
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'gateway', route: GW_ROUTE, items: gwItems, gateway: GW_CUR, tiers: TIERS }), { kind: 'gateway', exit: 'GW-Tokyo' });
  // gateway.current not known (or stale away): the route's leaf of the managed Gateway tier
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'gateway', route: GW_ROUTE, items: [], gateway: null, tiers: TIERS }), { kind: 'gateway', exit: 'GW-Tokyo' });
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'gateway', route: { ...GW_ROUTE, leaf: null, managed_tier: null }, items: [], gateway: null, tiers: TIERS }), { kind: 'unconfirmed' });
  assert.deepEqual(P.routeLine({ run: 'running', carrier: 'gateway', route: { ...GW_ROUTE, valid: false }, items: [], gateway: GW_CUR, tiers: TIERS }), { kind: 'unconfirmed' });
});

test('S8 heroOf: a carrier without a node count is neutral (nodes_unknown), never 全部节点可用', () => {
  const h = hero({ tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 0 } } });
  assert.deepEqual(h.hint, { kind: 'nodes_unknown', ks: 'none' });
  assert.equal(h.dot, 'ok');
  assert.equal(hero({ tiers: { ...TIERS, vanguard: { enabled: true, ok: 0, total: 0 } }, killswitch: 'blocked' }).hint.ks, 'blocked');
});

test('S2 modeOutcome: an explicit admin_required / conflict wins over a global cleanup_pending; the global flag explains only not-applied or io', () => {
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'admin_required' }, cleanupPending: true }), { kind: 'admin_required' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'conflict' }, cleanupPending: true }), { kind: 'conflict' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'timeout' }, cleanupPending: true }), { kind: 'failed', error: { code: 'timeout' } });
  const na = { code: 'lifecycle_busy', result: { saved: true, applied: false } };
  assert.deepEqual(P.modeOutcome({ ok: false, error: na, cleanupPending: true }), { kind: 'cleanup_pending' });
  assert.deepEqual(P.modeOutcome({ ok: false, error: na }), { kind: 'not_applied', error: na });
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'io' }, cleanupPending: true }), { kind: 'cleanup_pending' });
  // the error itself saying cleanup_pending still reads as such without the global flag
  assert.deepEqual(P.modeOutcome({ ok: false, error: { code: 'lifecycle_busy', reason_code: 'cleanup_pending' } }), { kind: 'cleanup_pending' });
});

test('S5 / S7 exitCard: cancelling disables 刷新; a running result without trigger is manual', () => {
  assert.equal(card(OK, null, { run: 'starting', cancelling: true }).refresh.enabled, false);
  assert.equal(card(OK, null, { run: 'starting', cancelling: false }).refresh.enabled, true);
  const run = result('running', { status: 'running' }, { status: 'running' }, { request_id: 'm-9', reason_code: 'querying' });
  assert.equal(card(run).badge.how, 'manual');
  assert.equal(card(run, OK).badge.how, 'manual');
  // a finished result without trigger still needs this window's request id (R3)
  const done = { ...OK }; delete done.trigger;
  assert.equal(card(done).badge.how, 'auto');
});
