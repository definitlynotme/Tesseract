// T-P4-06: the shell layer in the real design target (local Chrome through playwright-core,
// file://, fixture replay of the shell_* scenarios, no network):
//   - connect-time declarations and host_ready on a real page load (order, once, page name);
//   - host:confirmQuit answered with the save bar's count plus the registered drafts;
//   - host:navigate never closes an open drawer or drops a registered draft;
//   - a malicious host:notify stays text (title / detail), is receipted once on screen;
//   - the tier banner (core/app.js), driven by rt:tier alone: a tier.transition that arrives
//     before a late tiers.summary keeps its banner (also when the late summary only ends a
//     degraded spell); an instant recover takes the banner down without a toast; the banner goes
//     when the service stops running (7th-wave ruling T-P4-H #3; T-P4-H report section 7 #5);
//   - command palette service commands: disabled rules (run, cancelling, caps, exit identity,
//     network.mode.admin);
//   - T.ui.busy: after 10 s the control says common.state.still_working, the promise unchanged;
//   - T.motion.reduced = window.__TESSERACT_MOTION__.reduced OR prefers-reduced-motion (live).
// Skipped when Chrome is not installed (TESSERACT_UI_CHROME overrides the path).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { CHROME, UI, chromeAvailable } from '../fixtures/i18n/helpers.mjs';
import { buildTarget, writeOutput } from '../../build.mjs';

const skip = !chromeAvailable() && 'local Chrome not found';
const scenario = name => JSON.parse(readFileSync(join(UI, 'test', 'fixtures', 'scenarios', `${name}.json`), 'utf8'));
const E = 'rt-fixture-e1';
const T0 = 1790000000000;

let dir, browser, designUrl;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-shellc-app-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  designUrl = pathToFileURL(join(dir, 'design', 'index.html')).href;
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

async function open(sc, { hash = '#logs', reducedMotion = 'reduce', motion = null } = {}) {
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 800 }, reducedMotion });
  await context.route(/^(?!file:)/, r => r.abort());
  await context.addInitScript(([json, m]) => {
    window.__TESSERACT_FIXTURE__ = json;
    if (m) window.__TESSERACT_MOTION__ = m;
  }, [JSON.stringify(sc), motion]);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(designUrl + hash);
  await page.waitForFunction(() => window.T && T.i18n && T.store.get('lang') === 'zh_CN' && T.router.current);
  await page.waitForTimeout(150);
  return { context, page, errors };
}
const hosts = page => page.evaluate(() => T.rt.__fake.invocations.filter(x => x.kind === 'host').map(x => [x.name, x.args]));
const failures = page => page.evaluate(() => T.rt.__fake.failures);
const evt = (topic, data, rev, extra = {}) => ({ t: 'evt', epoch: E, produced_at_ms: T0 + rev, sent_at_ms: T0 + rev + 1, topic, rev, kind: 'snapshot', data, ...extra });
const deliver = (page, f) => page.evaluate(x => window.__TESSERACT_DESIGN__.deliver(x), f);

// a running page with no fallback, whose first tier.transition (the shell's cached last edge,
// instant) has been seen, so the next one is live
function runningBase() {
  const sc = scenario('shell_tier_fallback');
  sc.steps = sc.steps.slice(0, 10);
  sc.steps.push({ deliver: evt('tier.transition', { from: 'vanguard', to: 'vanguard', observed_at_ms: T0, authority: 'observed' }, 50, { kind: 'edge', seq: 1 }) });
  return sc;
}
const summary = (carrier, fallback, degraded = false) => ({
  carrier, offline_reason: null, order: [carrier, ...['vanguard', 'sentinel', 'bastion', 'gateway'].filter(x => x !== carrier)], degraded, fallback,
  tiers: { vanguard: { enabled: true, total: 3, usable: fallback ? 0 : 2 }, sentinel: { enabled: true, total: 2, usable: 1 },
    bastion: { enabled: true, total: 40, usable: 25 }, gateway: { enabled: false, total: 0, usable: 0 } },
  pool: { usable: 26, total: 45 },
});
const transition = (from, to, rev, seq) => evt('tier.transition', { from, to, observed_at_ms: T0 + rev, authority: 'observed' }, rev, { kind: 'edge', seq });
const tierState = page => page.evaluate(() => ({
  banner: (document.querySelector('#banners .banner[data-id="fallback"] .banner-title > span') || {}).textContent || null,
  toasts: [...document.querySelectorAll('#toast-slot .toast .toast-msg')].map(x => x.textContent),
}));

test('real page load: host_notify_ready, then host_set_page (profiles -> nodes), then host_ready once', { skip }, async () => {
  const { context, page, errors } = await open(scenario('shell_update_checking'), { hash: '#profiles' });
  await page.waitForTimeout(300);
  const h = (await hosts(page)).filter(([n]) => ['host_notify_ready', 'host_set_page', 'host_ready'].includes(n));
  assert.deepEqual(h, [['host_notify_ready', undefined], ['host_set_page', { page: 'nodes' }], ['host_ready', undefined]]);
  // a page change names the page; an rt:reset{reconnect} is the start of a page connection (the
  // shell sends it only at the start of a connect()'s replay, transport.rs): it declares again,
  // also while the client has not left its state (T-P4-H2 item 2, review cont. 1 SHOULD-3); a
  // same-epoch reconnect (a new rt_connect) declares again; host_ready stays once
  await page.evaluate(() => T.go('settings', { focus: 'alerts' }));
  await page.waitForTimeout(100);
  await deliver(page, { t: 'rt:reset', epoch: E, reason: 'reconnect' });
  await page.waitForTimeout(100);
  const shown = () => hosts(page).then(x => x.filter(([n]) => ['host_notify_ready', 'host_set_page', 'host_ready'].includes(n)).slice(3));
  const again = [['host_notify_ready', undefined], ['host_set_page', { page: 'alerts' }]];
  assert.deepEqual(await shown(), [['host_set_page', { page: 'alerts' }], ...again]);
  await page.evaluate(() => T.rt.__fake.reconnect('t06'));
  await deliver(page, { t: 'rt:reset', epoch: E, reason: 'reconnect' });
  await page.waitForTimeout(100);
  assert.deepEqual(await shown(), [['host_set_page', { page: 'alerts' }], ...again, ...again]);
  assert.deepEqual(await failures(page), []);
  assert.deepEqual(errors, []);
  await context.close();
});

test('host_ready waits for the first service.state snapshot (connecting: not called)', { skip }, async () => {
  const { context, page, errors } = await open(scenario('shell_connecting'));
  await page.waitForTimeout(300);
  const names = (await hosts(page)).map(([n]) => n);
  assert.ok(names.includes('host_notify_ready') && !names.includes('host_ready'), names.join());
  assert.equal(await page.evaluate(() => document.querySelector('#app').dataset.run), 'unknown');
  assert.deepEqual(errors, []);
  await context.close();
});

test('host:confirmQuit: unsaved = save bar count + registered drafts, answered at once, no dialog of its own', { skip }, async () => {
  const sc = scenario('shell_savebar_quit');
  sc.steps = sc.steps.filter(s => !s.on_invoke);
  sc.allow_host = [...sc.allow_host, 'host_confirm_quit'];
  const { context, page, errors } = await open(sc);
  const r = await page.evaluate(async () => {
    T.ui.saveBar({ count: 3, effect: 'instant', onSave: () => Promise.resolve(), onUndo: () => {} });
    const off = T.shellc.registerDraft(() => 2);
    T.shellc.registerDraft(() => true);
    window.__TESSERACT_DESIGN__.deliver({ t: 'host:confirmQuit', token: 11, epoch: 'rt-fixture-e1' });
    const first = T.rt.__fake.invocations.filter(x => x.name === 'host_confirm_quit').map(x => x.args);
    off();
    window.__TESSERACT_DESIGN__.deliver({ t: 'host:confirmQuit', token: 12, epoch: 'rt-fixture-e1' });
    await new Promise(res => setTimeout(res, 50));
    return { first, all: T.rt.__fake.invocations.filter(x => x.name === 'host_confirm_quit').map(x => x.args), dialogs: document.querySelectorAll('#layer .dlg, #layer [role="dialog"]').length };
  });
  // synchronous answer (well inside the shell's 3 s)
  assert.deepEqual(r.first, [{ token: 11, unsaved: 6 }]);
  assert.deepEqual(r.all, [{ token: 11, unsaved: 6 }, { token: 12, unsaved: 4 }]);
  assert.equal(r.dialogs, 0);
  assert.deepEqual(errors, []);
  await context.close();
});

test('host:navigate with a drawer open and a draft registered: the drawer stays, the draft is kept; afterwards it goes', { skip }, async () => {
  const { context, page, errors } = await open(scenario('shell_confirm_quit'), { hash: '#settings' });
  const r = await page.evaluate(async () => {
    let asked = 0;
    const d = T.drawer({ title: 'draft', content: '<p>x</p>', beforeClose: () => { asked++; return false; } });
    const off = T.shellc.registerDraft(() => 1);
    await new Promise(res => setTimeout(res, 50));
    const before = { overlay: T.overlayOpen(), unsaved: T.shellc.unsaved(), page: T.router.current };
    window.__TESSERACT_DESIGN__.deliver({ t: 'host:navigate', page: 'alerts', epoch: 'rt-fixture-e1' });
    window.__TESSERACT_DESIGN__.deliver({ t: 'host:navigate', page: 'overview', epoch: 'rt-fixture-e1' });
    await new Promise(res => setTimeout(res, 100));
    const held = { overlay: T.overlayOpen(), unsaved: T.shellc.unsaved(), page: T.router.current, drawerInDom: d.el.isConnected, asked,
      notes: T.shellc.diag().filter(x => x.kind === 'navigate_held').map(x => x.page) };
    off();
    d.close();
    await new Promise(res => setTimeout(res, 400));
    window.__TESSERACT_DESIGN__.deliver({ t: 'host:navigate', page: 'overview', epoch: 'rt-fixture-e1' });
    await new Promise(res => setTimeout(res, 100));
    return { before, held, after: T.router.current };
  });
  assert.deepEqual(r.before, { overlay: true, unsaved: 1, page: 'settings' });
  assert.deepEqual(r.held, { overlay: true, unsaved: 1, page: 'settings', drawerInDom: true, asked: 0, notes: ['alerts', 'overview'] });
  assert.equal(r.after, 'overview', 'nothing open, no draft: host:navigate goes');
  assert.deepEqual(errors, []);
  await context.close();
});

test('malicious host:notify: title and detail are text, nothing injected; each notice receipted once', { skip }, async () => {
  const { context, page, errors } = await open(scenario('shell_notify_malicious'));
  await page.waitForTimeout(300);
  const r = await page.evaluate(() => ({
    pwned: window.__pwned === undefined ? null : window.__pwned,
    injected: document.querySelectorAll('#banners img, #banners script, #toast-slot img, #toast-slot script, [onerror]').length,
    bannerTitle: (document.querySelector('#banners .banner[data-id="host-notify:m-1"] .banner-title > span') || {}).textContent,
    bannerMsg: (document.querySelector('#banners .banner[data-id="host-notify:m-1"] .banner-msg') || {}).textContent,
    toast: (document.querySelector('#toast-slot .toast .toast-msg') || {}).textContent,
    view: [...document.querySelectorAll('#banners .banner[data-id="host-notify:m-1"] .banner-actions button')].map(b => b.textContent),
  }));
  const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>';
  assert.equal(r.pwned, null);
  assert.equal(r.injected, 0);
  assert.equal(r.bannerTitle, evil);
  assert.equal(r.bannerMsg, `${evil} **md** [x](javascript:alert(1))`);
  assert.equal(r.toast, evil + evil);
  assert.deepEqual(r.view, ['查看']);
  const receipts = (await hosts(page)).filter(([n]) => n === 'host_notify_rendered').map(([, a]) => a.delivery_id);
  assert.deepEqual(receipts.sort(), ['m-1', 'm-2']);
  assert.deepEqual(errors, []);
  await context.close();
});

test('tier banner: tier.transition first, tiers.summary more than 1 s later -> the banner stays', { skip }, async () => {
  const { context, page, errors } = await open(runningBase());
  await deliver(page, transition('vanguard', 'sentinel', 60, 2));
  await page.waitForTimeout(150);
  const first = await tierState(page);
  assert.ok(first.banner && first.banner.includes('Sentinel'), JSON.stringify(first));
  await page.waitForTimeout(1200);
  await deliver(page, evt('tiers.summary', summary('sentinel', true), 70));
  await page.waitForTimeout(300);
  const late = await tierState(page);
  assert.equal(late.banner, first.banner);
  assert.deepEqual(late.toasts, []);
  assert.deepEqual(errors, []);
  await context.close();
});

test('tier banner: a late summary that only ends a degraded spell (latency recovery) leaves the banner up', { skip }, async () => {
  const { context, page, errors } = await open(runningBase());
  await deliver(page, evt('tiers.summary', summary('vanguard', false, true), 55));      // degraded (no banner)
  await page.waitForTimeout(100);
  await deliver(page, transition('vanguard', 'sentinel', 60, 2));                         // fallback
  await page.waitForTimeout(1200);
  await deliver(page, evt('tiers.summary', summary('vanguard', false, false), 70));     // sampled before the switch
  await page.waitForTimeout(300);
  const s = await tierState(page);
  assert.ok(s.banner && s.banner.includes('Sentinel'), JSON.stringify(s));
  assert.equal(s.toasts.length, 1, 'the latency recovery has its own toast');
  assert.deepEqual(errors, []);
  await context.close();
});

test('tier banner: an instant recover takes it down without a "recovered" toast; a live one toasts', { skip }, async () => {
  const { context, page, errors } = await open(runningBase());
  const fire = e => page.evaluate(x => T.emit('rt:tier', x), e);
  await fire({ kind: 'fallback', carrier: 'sentinel', from: 'vanguard', observed: true, source: 'transition', instant: false });
  await page.waitForTimeout(150);
  assert.ok((await tierState(page)).banner);
  await fire({ kind: 'recover', carrier: 'vanguard', from: 'sentinel', wasFallback: true, observed: true, source: 'summary', instant: true });
  await page.waitForTimeout(800);
  assert.deepEqual(await tierState(page), { banner: null, toasts: [] });
  await fire({ kind: 'fallback', carrier: 'sentinel', from: 'vanguard', observed: true, source: 'transition', instant: false });
  await page.waitForTimeout(150);
  await fire({ kind: 'recover', carrier: 'vanguard', from: 'sentinel', wasFallback: true, observed: true, source: 'summary', instant: false });
  await page.waitForTimeout(300);
  const live = await tierState(page);
  assert.equal(live.banner, null);
  assert.equal(live.toasts.length, 1);
  assert.deepEqual(errors, []);
  await context.close();
});

test('T-P4-H2: entering Gateway takes the banner down without "recovered"; leaving it for a backup tier names Vanguard, never Gateway', { skip }, async () => {
  const { context, page, errors } = await open(runningBase());
  const body = () => page.evaluate(() => { const b = document.querySelector('#banners .banner[data-id="fallback"]'); return b ? b.textContent : null; });
  await deliver(page, transition('vanguard', 'sentinel', 60, 2));
  await page.waitForTimeout(150);
  assert.ok((await tierState(page)).banner);
  // the user chose a Gateway (transition first, then the summary)
  await deliver(page, transition('sentinel', 'gateway', 61, 3));
  await deliver(page, evt('tiers.summary', summary('gateway', false), 71));
  await page.waitForTimeout(800);
  assert.deepEqual(await tierState(page), { banner: null, toasts: [] }, 'the banner goes; nothing says "recovered"');
  // the Gateway is cancelled and PROXY-FALLBACK carries on Bastion (summary first this time)
  await deliver(page, evt('tiers.summary', summary('bastion', true), 72));
  await deliver(page, transition('gateway', 'bastion', 62, 4));
  await page.waitForTimeout(150);
  const s = await tierState(page);
  assert.ok(s.banner && s.banner.includes('Bastion'), JSON.stringify(s));
  const text = await body();
  assert.ok(text.includes('Vanguard') && !text.includes('Gateway'), text);
  assert.deepEqual(errors, []);
  await context.close();
});

test('tier banner: goes when the service stops running (failed), also from a replayed frame', { skip }, async () => {
  const { context, page, errors } = await open(runningBase());
  await page.evaluate(() => T.emit('rt:tier', { kind: 'fallback', carrier: 'sentinel', from: 'vanguard', observed: true, source: 'transition', instant: false }));
  await page.waitForTimeout(150);
  assert.ok((await tierState(page)).banner);
  await page.evaluate(() => T.emit('rt:state', { run: 'failed', prev: 'running', cancelled: false, instant: true }));
  await page.waitForTimeout(400);
  assert.equal((await tierState(page)).banner, null);
  assert.deepEqual(errors, []);
  await context.close();
});

// the palette's service and mode commands: [label, disabled, hint]
async function paletteState(page) {
  return page.evaluate(async () => {
    T.cmd.open();
    await new Promise(r => setTimeout(r, 30));
    const want = ['svc_toggle', 'svc_restart', 'svc_exit', 'svc_copy', 'mode_system', 'mode_tun', 'mode_mixed'];
    const out = {};
    for (const b of document.querySelectorAll('.cmdk-list .cmdk-item')) {
      const label = b.getAttribute('aria-label');
      const key = want.find(k => label === T.t(`shell.palette.cmd.${k}`, k === 'svc_toggle' ? { action: 'start' } : undefined)
        || (k === 'svc_toggle' && ['stop', 'cancel'].some(a => label === T.t('shell.palette.cmd.svc_toggle', { action: a }))));
      if (key) out[key] = [b.getAttribute('aria-disabled') === 'true', ((b.querySelector('.hint') || {}).textContent || '')];
    }
    T.cmd.close();
    return out;
  });
}

test('palette service commands: run state, cancelling, caps, exit identity, admin', { skip }, async () => {
  const cases = [
    ['shell_update_checking', s => {
      assert.deepEqual(s.svc_toggle, [false, '']);
      assert.deepEqual(s.svc_restart, [false, '']);
      assert.deepEqual(s.svc_exit, [false, '']);
      assert.deepEqual(s.svc_copy, [false, '203.0.113.24']);
      assert.deepEqual(s.mode_system, [true, '当前']);
      assert.deepEqual(s.mode_tun, [false, '']);
    }],
    ['shell_connecting', s => { for (const k of ['svc_toggle', 'svc_restart', 'svc_exit', 'svc_copy']) assert.equal(s[k][0], true, k); }],
    ['shell_cancelling', s => { assert.equal(s.svc_toggle[0], true); assert.equal(s.svc_restart[0], true); }],
    ['shell_caps_absent', s => {
      assert.equal(s.svc_toggle[0], false);
      for (const k of ['svc_exit', 'svc_copy', 'mode_system', 'mode_tun', 'mode_mixed']) assert.equal(s[k][0], true, k);
    }],
    ['shell_copy_ip_unavailable', s => assert.deepEqual(s.svc_copy, [true, ''])],
    ['shell_mode_failed', s => {
      assert.deepEqual(s.mode_tun, [true, '需要管理员权限']);
      assert.deepEqual(s.mode_mixed, [true, '需要管理员权限']);
      assert.deepEqual(s.svc_toggle, [false, '']);
      assert.equal(s.svc_restart[0], true);
    }],
  ];
  for (const [name, check] of cases) {
    const { context, page, errors } = await open(scenario(name));
    const s = await paletteState(page);
    try { check(s); } catch (e) { e.message = `${name}: ${e.message} ${JSON.stringify(s)}`; throw e; }
    assert.deepEqual(errors, [], name);
    await context.close();
  }
});

test('T.ui.busy: after 10 s the control says "still working"; the promise and its result are unchanged', { skip }, async () => {
  const { context, page, errors } = await open(runningBase());
  const r = await page.evaluate(async () => {
    const real = window.setTimeout;
    const timers = [];
    window.setTimeout = (fn, ms, ...a) => { if (ms === 10000) { timers.push(fn); return 0; } return real(fn, ms, ...a); };
    const btn = T.h('button', { class: 'btn' }, T.h('span', { class: 'lab' }, 'Go'));
    document.body.append(btn);
    let resolve;
    const job = new Promise(res => { resolve = res; });
    const p = T.ui.busy(btn, () => job);
    window.setTimeout = real;
    await new Promise(res => real(res, 30));
    const before = btn.querySelector('.lab').textContent;
    timers.forEach(fn => fn());
    await new Promise(res => real(res, 30));
    const slow = btn.querySelector('.lab').textContent;
    resolve(42);
    const v = await p;
    return { timers: timers.length, before, slow, want: T.t('common.state.still_working'), v, busy: T.t('core.busy.working') };
  });
  assert.equal(r.timers, 1);
  assert.equal(r.before, r.busy);
  assert.equal(r.slow, r.want);
  assert.ok(r.want && r.want !== 'common.state.still_working');
  assert.equal(r.v, 42);
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.motion.reduced: the shell flag OR prefers-reduced-motion, the media query followed live', { skip }, async () => {
  const read = page => page.evaluate(() => [T.motion.reduced, document.documentElement.classList.contains('reduced-motion')]);
  let o = await open(runningBase(), { reducedMotion: 'no-preference', motion: { reduced: true } });
  assert.deepEqual(await read(o.page), [true, true]);
  await o.context.close();
  o = await open(runningBase(), { reducedMotion: 'reduce' });
  assert.deepEqual(await read(o.page), [true, true]);
  await o.context.close();
  o = await open(runningBase(), { reducedMotion: 'no-preference' });
  assert.deepEqual(await read(o.page), [false, false]);
  await o.page.emulateMedia({ reducedMotion: 'reduce' });
  // the media query's change event arrives on its own time: wait for it (a fixed 50 ms failed
  // under load in a full run; T-P4-06 review cont. 1 SHOULD-2, 8th-wave ruling T-P4-H2 item 2)
  await o.page.waitForFunction(() => T.motion.reduced === true && document.documentElement.classList.contains('reduced-motion'), null, { timeout: 5000 });
  assert.deepEqual(await read(o.page), [true, true]);
  assert.deepEqual(o.errors, []);
  await o.context.close();
});
