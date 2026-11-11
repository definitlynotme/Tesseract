// T-P4-05b: the tiers page's derivations (src/rt/project_tiers.js) and its handling of a revealed
// secret (mapping_tiers sections 1, 4, 6-8, 10, 12; PROTOCOL sections 12, 15, 16).
//
// Pure checks: row state, current-node marker (route.published for the fallback tiers;
// gateway.current.selected_node_id for Gateway, never a guess by name: 7th-wave ruling), drafts and
// their ConfigItem ids (K-11), subscription rows and their degraded form (section 4.3), membership
// and search by subscription name (K-4, G40), the new rows of an import by difference (items are
// batch tokens), the newer of nodes.detail and the list row, write / refresh outcomes (G35, 12.3),
// the pool (D-sem, D9) and the import form rules of import.specs (G19, no prefill from an implicit
// default), preview groups (G38).
// Browser check (local Chrome, the design target built in memory, fixture tiers_writes; skipped
// without Chrome): nodes.reveal's answer is shown as text in the side panel only — never in an
// attribute or the store — and is gone from the whole page once the panel closes.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { buildTarget, writeOutput } from '../../build.mjs';
import * as P from '../../src/rt/project_tiers.js';

const UI = new URL('../../', import.meta.url);
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const skip = !existsSync(CHROME) && 'local Chrome not found';
const SPECS = JSON.parse(readFileSync(new URL('src/design/catalog.json', UI), 'utf8')).import;

const node = (id, o = {}) => ({ id, name: o.name || id, tier: o.tier || 'vanguard', kind: o.kind || 'manual', enabled: o.status !== 'disabled', status: o.status || 'healthy',
  reason_code: o.reason || null, protocol: o.protocol || 'trojan', latency_ms: o.lat == null ? null : o.lat, ...(o.extra || {}) });
const route = (leaf, tier) => ({ valid: true, leaf, managed_tier: tier });

test('row state: the runtime status only (no recount, no latency re-judging)', () => {
  assert.equal(P.nodeState(node('a')), 'ok');
  assert.equal(P.nodeState(node('a', { extra: { slow: true } })), 'slow', 'K-5 slow when the field is there');
  assert.equal(P.nodeState(node('a', { status: 'failed', reason: 'tls_failed' })), 'down');
  assert.equal(P.nodeState(node('a', { status: 'pending', reason: 'subscription_recheck_pending' })), 'recheck');
  assert.equal(P.nodeState(node('a', { status: 'pending' })), 'pending');
  assert.equal(P.nodeState(node('a', { status: 'unknown' })), 'pending');
  assert.equal(P.nodeState(node('a', { status: 'disabled' })), 'draft');
  assert.equal(P.nodeState(node('a', { lat: 5000 })), 'ok', 'a high latency is coloured, never re-judged');
  assert.equal(P.nodeReason(node('a', { status: 'failed', reason: 'tls_failed' })), 'tls_failed');
  assert.equal(P.nodeReason(node('a', { status: 'failed', reason: 'made_up' })), 'node_unavailable', 'an unknown code reads as node_unavailable');
  assert.equal(P.nodeReason(node('a', { status: 'failed' })), 'node_unavailable');
  assert.equal(P.nodeReason(node('a')), null);
  assert.equal(P.lastKnown('ok', 'stopped'), true);
  assert.equal(P.lastKnown('ok', 'starting'), false, 'starting counts as running');
  assert.equal(P.lastKnown('pending', 'stopped'), false);
});

test('current marker: route.published for the fallback tiers, selected_node_id for Gateway (no guess by name)', () => {
  const hk = node('v-hk', { name: 'hk' }), jp = node('v-jp', { name: 'jp' });
  const ctx = { route: route('hk', 'vanguard'), gateway: null, run: 'running' };
  assert.equal(P.isCurrent(hk, 'vanguard', ctx), true);
  assert.equal(P.isCurrent(jp, 'vanguard', ctx), false);
  assert.equal(P.isCurrent(hk, 'vanguard', { ...ctx, run: 'stopped' }), false, 'stopped: nothing carries');
  assert.equal(P.isCurrent(hk, 'vanguard', { ...ctx, route: { ...ctx.route, valid: false } }), false, 'an invalid route marks nothing');
  assert.equal(P.isCurrent(hk, 'sentinel', { ...ctx, route: route('hk', 'vanguard') }), false, 'only the carrying tier marks (B-01)');
  assert.equal(P.isCurrent(node('d', { name: 'hk', status: 'disabled' }), 'vanguard', ctx), false, 'a draft is never current');
  const gw = node('g-osa', { name: 'osa', tier: 'gateway' }), gw2 = node('g-osa-2', { name: 'osa', tier: 'gateway' });
  const g = { selected: 'osa', effective: 'osa', missing: false, applied: true, pending: null };
  assert.equal(P.isCurrent(gw, 'gateway', { route: null, gateway: g, run: 'running' }), false, 'no selected_node_id: no row is marked, even with the same name');
  assert.equal(P.gatewayView(g, 'running').name, 'osa', '… the current exit is still named (as text)');
  const gid = { ...g, selected_node_id: 'g-osa' };
  assert.equal(P.isCurrent(gw, 'gateway', { route: null, gateway: gid, run: 'running' }), true, 'T-P2-H8: the row by its id');
  assert.equal(P.isCurrent(gw2, 'gateway', { route: null, gateway: gid, run: 'running' }), false, 'a namesake is not marked');
  assert.equal(P.isCurrent(gw, 'gateway', { route: null, gateway: { ...gid, effective: 'other' }, run: 'running' }), false, 'running on another exit: the selection is not the current one');
  assert.equal(P.isCurrent(gw, 'gateway', { route: null, gateway: { ...gid, effective: null }, run: 'stopped' }), true, 'stopped: the saved selection');
  const v = P.gatewayView({ selected: 'gone', effective: null, missing: true, applied: false, pending: null }, 'running');
  assert.equal(v.missing, true, 'G37'); assert.equal(v.waiting, false);
  assert.equal(P.gatewayView({ selected: 'osa', effective: null, missing: false, applied: false, pending: null }, 'running').waiting, true);
  assert.equal(P.gatewayView(null, 'running').known, false, 'gateway.current not published: unknown');
});

test('drafts: nodes.list disabled rows and config.items, and the ConfigItem id that re-enables them (K-11)', () => {
  const nodes = [node('v-a', { name: 'a' }), node('v-old', { name: 'old', status: 'disabled' })];
  const items = [
    { id: 'cfg-a', name: 'a', kind: 'node', section: 'vanguard', enabled: true, secret_set: true },
    { id: 'cfg-old', name: 'old', kind: 'node', section: 'vanguard', enabled: false, secret_set: true },
    { id: 'cfg-two', name: 'two', kind: 'node', section: 'vanguard', enabled: false, secret_set: true },
    { id: 'cfg-gw', name: 'x', kind: 'node', section: 'gateway', enabled: false, secret_set: true },
    { id: 'sub-1', name: 's', kind: 'subscription', section: 'vanguard', enabled: false, secret_set: true },
  ];
  const d = P.draftRows('vanguard', items, nodes);
  assert.deepEqual(d.map(x => x.id), ['cfg-two'], 'a config item already listed (by name) is not repeated; other tiers and kinds are not drafts');
  assert.equal(d[0].configItemId, 'cfg-two');
  assert.equal(P.configItemIdOf(nodes[1], items, null), 'cfg-old', 'by name within the tier');
  assert.equal(P.configItemIdOf(nodes[1], [], { config_item: { id: 'cfg-detail' } }), 'cfg-detail', 'nodes.detail.config_item first');
  assert.equal(P.configItemIdOf(nodes[1], [...items, { ...items[1], id: 'cfg-dup' }], null), null, 'two candidates: unknown, the switch only goes off');
});

test('subscriptions: rows, the config.items fallback (section 4.3), health words (G17, G35)', () => {
  const list = [{ id: 's1', name: 'n', tier: 'sentinel', enabled: true, display: 'n', total: 3, healthy: 2, visibility: 'previous', refreshing: false }];
  assert.equal(P.subscriptionRows('sentinel', { list, published: true, items: [] })[0].degraded, false);
  assert.equal(P.subscriptionRows('sentinel', { list: null, published: true, items: [] }), null, 'published, no snapshot yet');
  const items = [{ id: 'sub-x', name: 'x', kind: 'subscription', section: 'sentinel', enabled: false, secret_set: true }, { id: 'n1', name: 'y', kind: 'node', section: 'sentinel', enabled: true, secret_set: true }];
  const deg = P.subscriptionRows('sentinel', { list: null, published: false, items });
  assert.deepEqual(deg.map(s => [s.id, s.enabled, s.degraded, s.total]), [['sub-x', false, true, null]]);
  assert.equal(P.subscriptionHealth(list[0]), 'previous');
  assert.equal(P.subscriptionHealth({ ...list[0], enabled: false }), 'disabled');
  assert.equal(P.subscriptionHealth(deg[0]), 'unknown', 'degraded: health unknown');
  for (const v of ['current', 'pending', 'unknown']) assert.equal(P.subscriptionHealth({ ...list[0], visibility: v }), v);
});

test('K-4 / G40: membership and search by subscription name only when Node.subscription_id is there', () => {
  const withId = [node('a', { name: 'fra', kind: 'subscription', extra: { subscription_id: 's1' } }), node('b', { name: 'sel', kind: 'subscription', extra: { subscription_id: 's2' } })];
  const without = withId.map(({ subscription_id, ...n }) => n);
  assert.equal(P.hasMembership(withId), true);
  assert.equal(P.hasMembership(without), false, 'frozen sidecar: no drill-in');
  assert.equal(P.hasMembership([node('m', { extra: { subscription_id: null } })]), true, 'the key present, even null');
  assert.deepEqual(P.membersOf('s1', withId).map(n => n.id), ['a']);
  assert.equal(P.matches(withId[0], 'nebula', 'nebula'), true, 'G40: the subscription name finds its node');
  assert.equal(P.matches(without[0], 'nebula', null), false);
  assert.equal(P.matches(withId[0], 'TROJAN', null), true, 'protocol, any case');
  assert.equal(P.matches(withId[0], '  ', null), true);
});

test('write-after-read: the new rows of an import by difference; the newer of detail and list row', () => {
  const before = new Set(['v-a', 'v-b', 'g:s1']);
  assert.deepEqual(P.diffNew(before, [{ id: 'v-a' }, { id: 'v-c' }, { id: 'g:s1' }, { id: 'g:s2' }]), ['v-c', 'g:s2']);
  assert.deepEqual(P.diffNew(['v-a'], [{ id: 'v-a' }]), []);
  const row = node('a', { lat: 40 }), det = node('a', { lat: 47 });
  assert.equal(P.pickNewer({ listRow: row, revAtRequest: 3, revNow: 3, detailNode: det }), det, 'the list did not move: nodes.detail is newer');
  assert.equal(P.pickNewer({ listRow: row, revAtRequest: 3, revNow: 4, detailNode: det }), row, 'a later patch wins');
  assert.equal(P.pickNewer({ listRow: null, revAtRequest: 0, revNow: 0, detailNode: det }), det);
  const lat = [node('d', { status: 'disabled' }), node('f', { status: 'failed' }), node('p', { status: 'pending' }), node('o', { lat: 90 }), node('q', { lat: 30 })];
  assert.deepEqual(lat.sort((a, b) => P.latKey(a) - P.latKey(b)).map(n => n.id), ['q', 'o', 'p', 'f', 'd']);
});

test('outcomes: Applied, failed writes (rolled back, unconfirmed rollback, saved not applied), refresh (G35)', () => {
  assert.equal(P.appliedOutcome({ saved: true, applied: true, pending: null }, 'running'), 'applied');
  assert.equal(P.appliedOutcome({ saved: true, applied: true, pending: null }, 'stopped'), 'next_start', 'Core not running: applied means the next start');
  assert.equal(P.appliedOutcome({ saved: true, applied: false, pending: { state: 'pending' } }, 'running'), 'pending');
  assert.equal(P.appliedOutcome({ saved: true, applied: false, pending: null }, 'running'), 'unapplied');
  const failed = (result, reason) => ({ code: 'io', reason_code: reason, result });
  assert.equal(P.writeFailure(failed({ saved: false, applied: false, apply_error: { code: 'io', reason_code: 'rolled_back' } }, 'rolled_back')), 'rolled_back');
  assert.equal(P.writeFailure(failed({ saved: true, applied: false, apply_error: { code: 'io', reason_code: 'rollback_unconfirmed' } }, 'rollback_unconfirmed')), 'rollback_unconfirmed', 'not "saved"');
  assert.equal(P.writeFailure(failed({ saved: true, applied: false, apply_error: { code: 'io', reason_code: 'runtime_reload_failed' } }, 'runtime_reload_failed')), 'unapplied');
  assert.equal(P.writeFailure({ code: 'lifecycle_busy' }), 'busy');
  assert.equal(P.writeFailure({ code: 'conflict' }), 'error');
  const RR = o => ({ total: 3, healthy: 2, stored: true, applied: true, retained_previous: true, apply_error: null, error: null, ...o });
  assert.equal(P.refreshOutcome({ result: RR(), run: 'running' }), 'applied');
  assert.equal(P.refreshOutcome({ result: RR(), run: 'stopped' }), 'next_start');
  assert.equal(P.refreshOutcome({ error: { code: 'io', result: RR({ applied: false }) } }), 'stored_unapplied');
  assert.equal(P.refreshOutcome({ error: { code: 'io', result: RR({ stored: false, applied: false }) } }), 'kept_previous');
  assert.equal(P.refreshOutcome({ error: { code: 'io', result: RR({ stored: false, applied: false, retained_previous: false }) } }), 'failed', 'a new subscription has no previous cache');
  assert.equal(P.refreshOutcome({ error: { code: 'lifecycle_busy' } }), 'busy_stopping');
  assert.equal(P.refreshOutcome({ error: { code: 'conflict', reason_code: 'refresh_in_progress' } }), 'in_progress');
  assert.equal(P.refreshOutcome({ error: { code: 'conflict', reason_code: 'recovery_pending' } }), 'recovery');
  assert.equal(P.planRetry({ code: 'conflict', reason_code: 'plan_expired' }), true);
  assert.equal(P.planRetry({ code: 'conflict', result: { apply_error: { code: 'conflict', reason_code: 'import_plan_changed' } } }), true);
  assert.equal(P.planRetry({ code: 'conflict', reason_code: 'duplicate_node' }), false);
});

test('Bastion pool: only collecting and the numbers (D-sem 2, D9)', () => {
  const pool = { runtime_alive: 2, user_alive: 1, capacity: 60, min_alive_nodes: 3, target_alive: 24, collecting: false, observed_at_ms: 1 };
  assert.equal(P.poolView(pool, { enabled: false, run: 'running' }).phase, 'off');
  assert.equal(P.poolView(null, { enabled: true, run: 'running' }).phase, 'unknown');
  assert.equal(P.poolView(pool, { enabled: true, run: 'stopped' }).phase, 'idle');
  assert.equal(P.poolView({ ...pool, collecting: true }, { enabled: true, run: 'running' }).phase, 'filling');
  assert.equal(P.poolView(pool, { enabled: true, run: 'running', carrier: 'bastion' }).phase, 'carrying');
  assert.equal(P.poolView(pool, { enabled: true, run: 'running' }).phase, 'low', 'below the minimum and not collecting');
  const r = P.poolView({ ...pool, runtime_alive: 9 }, { enabled: true, run: 'running' });
  assert.deepEqual([r.phase, r.capacity, r.target], ['ready', 60, 24], 'the capacity is the meter (D9, never 60 written in)');
  // §8u: the totals when sent (available/total on the page), null from an older sidecar
  assert.deepEqual([r.total, r.userTotal], [null, null]);
  const t = P.poolView({ ...pool, runtime_total: 14, user_total: 2 }, { enabled: true, run: 'running' });
  assert.deepEqual([t.ok, t.total, t.user, t.userTotal], [2, 14, 1, 2]);
});

test('§8u: a multi-protocol Vanguard row is current when the route runs any of its variants', () => {
  const ctx = { route: { valid: true, managed_tier: 'vanguard', leaf: 'edge :: VLESS Reality' }, run: 'running' };
  const row = { id: 'a', name: 'edge', tier: 'vanguard', enabled: true, status: 'healthy' };
  assert.equal(P.isCurrent(row, 'vanguard', ctx), true);
  assert.equal(P.isCurrent({ ...row, name: 'edg' }, 'vanguard', ctx), false);
  assert.equal(P.isCurrent(row, 'sentinel', { ...ctx, route: { ...ctx.route, managed_tier: 'sentinel' } }), false, 'only Vanguard has variants');
  assert.equal(P.isVariantOf('a :: b :: SOCKS5', 'a'), false, 'the label is the last part only');
});

test('import.specs (G19): conditions, required first, advanced apart, no prefill from an implicit default', () => {
  const argo = SPECS.protocols.find(p => p.id === 'argo_xhttp');
  const vals = {};
  const { main, advanced } = P.formFields(argo, vals);
  assert.ok(!main.concat(advanced).some(f => f.id === 'tier'), 'the tier field is the sheet\'s own');
  const req = main.filter(f => P.fieldRequired(f, argo.fields, vals)).length;
  assert.ok(main.slice(0, req).every(f => P.fieldRequired(f, argo.fields, vals)), 'required fields first');
  assert.ok(main.some(f => f.id === 'sni'), 'sni is visible: security\'s implicit default is tls (conditions only)');
  assert.ok(advanced.some(f => f.id === 'fingerprint') && !main.some(f => f.id === 'fingerprint'), 'advanced fields apart');
  assert.ok(!main.some(f => f.id === 'sni') || P.fieldRequired(argo.fields.find(f => f.id === 'sni'), argo.fields, vals), 'sni required while tls');
  assert.ok(!P.formFields(argo, { security: 'none' }).main.some(f => f.id === 'sni'), 'security none hides sni');
  const fields = P.applyFields(argo, { name: 'n', server: 'h', port: '8443', uuid: 'u', host: 'x', path: '/p', sni: '' });
  assert.deepEqual(fields, { name: 'n', server: 'h', port: 8443, uuid: 'u', host: 'x', path: '/p' }, 'only what was entered; no security from its implicit default');
  const probs = P.localProblems(argo, { port: '70000' });
  assert.equal(probs.port, 'range'); assert.equal(probs.name, 'required');
  assert.ok(P.protocolsForTier(SPECS, 'vanguard').some(p => p.id === 'argo_xhttp'));
  assert.ok(!P.protocolsForTier(SPECS, 'sentinel').some(p => p.id === 'argo_xhttp'), 'Argo only in Vanguard');
  assert.ok(!P.protocolsForTier(SPECS, 'vanguard', { chain: true }).some(p => p.id === 'argo_xhttp'), 'Argo not in a chain');
  assert.equal(P.formFields(argo, {}, { shared: true }).main.some(f => f.id === 'name' || f.id === 'server'), false, 'a chain row shares name and server');
  assert.deepEqual(P.fieldNotices({ id: 'm', conditions: { notices: [{ code: 'ss_none_unencrypted', visible_when: [{ field: 'm', values: ['none'] }] }] } }, [], { m: 'none' }), ['ss_none_unencrypted']);
});

test('import.preview: groups (G38) only when they cover every accepted node once; rejected reasons counted', () => {
  const ok = { accepted: 3, groups: [{ key: 'g0', label: 'hk', row_indexes: [0, 1] }, { key: 'g1', row_indexes: [2] }] };
  assert.equal(P.previewGroups(ok), ok.groups);
  assert.equal(P.previewGroups({ accepted: 3 }), null, 'absent: the per-row preview');
  assert.equal(P.previewGroups({ accepted: 3, groups: [{ key: 'g0', row_indexes: [0, 1] }] }), null, 'a node missing');
  assert.equal(P.previewGroups({ accepted: 2, groups: [{ key: 'g0', row_indexes: [0, 0, 1] }] }), null, 'a node twice');
  assert.equal(P.previewGroups({ accepted: 0, groups: [] }), null);
  const e = (k, s) => ({ code: 'validation', message_key: k, params: s ? { scheme: s } : {} });
  const r = P.rejectedSummary([e('errors.import.subscription_url_not_node'), e('errors.import.subscription_url_not_node'), e('errors.import.unsupported_uri_scheme', 'xyz'), e('errors.import.unsupported_uri_scheme', 'abc')]);
  assert.deepEqual(r.map(x => x.n), [2, 1, 1]);
  assert.equal(P.firstUrlLine('vless://x\n  https://sub.example.com/a  \nhttp://b'), 'https://sub.example.com/a');
  assert.equal(P.firstUrlLine('vless://x'), null);
});

/* ------------------------------------------------------------------- browser: nodes.reveal */
const SECRET = 'FIXTURE-SECRET-7f3a';
let dir, browser;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-ti05b-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(dir, files);
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu', '--host-resolver-rules=MAP * ~NOTFOUND'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

test('nodes.reveal (private): text in the side panel only, never an attribute or the store; gone when the panel closes', { skip, timeout: 120000 }, async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce', locale: 'zh-CN' });
  try {
    await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
    await context.addInitScript({ path: fileURLToPath(new URL('test/render/fixture_clock.js', UI)) });
    await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, readFileSync(new URL('test/fixtures/scenarios/tiers_writes.json', UI), 'utf8'));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e.message)));
    await page.goto(pathToFileURL(join(dir, 'index.html')).href + '#tiers', { waitUntil: 'load' });
    const adv = async ms => { await page.evaluate(m => window.__fixtureClock.advance(m), ms); await page.waitForTimeout(60); await page.evaluate(() => window.__fixtureClock.advance(0)); };
    await adv(3000);
    const has = () => page.evaluate(s => ({
      html: document.documentElement.outerHTML.includes(s),
      text: [...document.querySelectorAll('.tiers-secv')].some(e => e.textContent.includes(s)),
      attrs: [...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)).some(v => v.includes(s)),
      /* every store key (T.store.snapshot) and the keyed lists of all four tiers (review A S4 / B S1) */
      store: Object.entries(T.store.snapshot()).some(([, v]) => { try { return JSON.stringify(v === undefined ? null : v).includes(s); } catch (e) { return true; } })
        || ['vanguard', 'sentinel', 'bastion', 'gateway'].some(t => ['nodes.list', 'subscriptions.list'].some(n => JSON.stringify(T.rt.topic(n, t).get() || null).includes(s))),
      diag: JSON.stringify(T.rt.diag()).includes(s),
    }), SECRET);
    await page.click('.tiers-row[data-key="v-hk03"] .tiers-name'); await adv(700);
    await page.click('.tiers-panel .tiers-fold'); await adv(300);
    assert.equal((await has()).html, false, 'nothing is fetched before the eye is clicked');
    await page.click('.tiers-panel .tiers-params .icon-btn'); await adv(500);
    const shown = await has();
    assert.equal(shown.text, true, 'shown as text');
    assert.equal(shown.attrs, false, 'no attribute holds it');
    assert.equal(shown.store, false, 'not in the store or a topic');
    assert.equal(shown.diag, false, 'not in the diagnostics ring');
    await page.click('.tiers-panel .tiers-params .icon-btn'); await adv(300);
    assert.equal((await has()).html, false, 'hidden again: the mask is back');
    await page.click('.tiers-panel .tiers-params .icon-btn'); await adv(300);
    assert.equal((await has()).text, true, 'shown again from memory');
    await page.click('.tiers-panel .ovl-x'); await adv(800);
    assert.equal(await page.evaluate(() => !!document.querySelector('.tiers-panel')), false);
    assert.equal((await has()).html, false, 'the panel closed: the value is gone from the whole page (outerHTML)');
    const reveals = await page.evaluate(() => T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.reveal').length);
    assert.equal(reveals, 1, 'fetched once (on the click), kept in the panel memory meanwhile');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

// After an import, until nodes.list catches up, configured nodes it lacks are listed from config.items.
test('import wait: unloaded rows list enabled configured nodes nodes.list lacks', () => {
  const items = [
    { id: 'c1', kind: 'node', section: 'sentinel', name: 'a', enabled: true },
    { id: 'c2', kind: 'node', section: 'sentinel', name: 'b', enabled: true },
    { id: 'c3', kind: 'node', section: 'sentinel', name: 'b', enabled: true },
    { id: 'c4', kind: 'node', section: 'sentinel', name: 'off', enabled: false },
    { id: 'g1', kind: 'node', section: 'gateway', name: 'g', enabled: true },
  ];
  assert.deepEqual(P.unloadedRows('sentinel', items, []).map(r => r.id), ['c1', 'c2', 'c3'], 'enabled nodes of the tier only; disabled are drafts');
  assert.deepEqual(P.unloadedRows('sentinel', items, [{ id: 'n1', name: 'a' }]).map(r => r.id), ['c2', 'c3']);
});
