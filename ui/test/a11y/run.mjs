// T-P4-09 accessibility, the automatically testable part (P4 architecture §11; ticket requirement 8).
//
//   node ui/test/a11y/run.mjs [--dist <design build>] [--only a,b] [--shell] [--build] [--keep]
//
// The design target in fixture mode (window.__TESSERACT_FIXTURE__), local Chrome (playwright-core,
// offline: file: only), zh_CN, 1280x800; the deterministic fixture clock where a block needs no real
// motion. The build is ui/dist/design as `npm --prefix ui run build -- --pseudo --i18n-trace` left
// it (a private copy, checked against its manifest), or --dist. Blocks:
//   names       CDP Accessibility.getFullAXTree on the four pages × their main states (and the
//               palette, a toast and a banner, the tier panel, the log reader, the raw log grid, a
//               settings draft, the channel drawer): every interactive node (a widget role, or
//               focusable) that is not ignored has a non-empty accessible name
//   lang        a language switch (host:language → pseudo, then back to zh_CN) on 总览 and on 配置:
//               <html lang> follows; every aria-label / title / placeholder / data-tip that
//               data-i18n-attr binds is rewritten in the new language, and no such attribute outside
//               translate="no" keeps a CJK character in pseudo
//   live        the run pill, the toast host and the banner host are aria-live regions; a toast and a
//               banner leave the focus where it was
//   focus       the channel drawer (modal) and a confirm dialog keep Tab and Shift+Tab inside and give
//               the focus back to the opener on Esc; the palette (Ctrl K) takes the focus into its
//               field, keeps Tab there and gives it back on Esc; Esc on a toast dismisses it and
//               leaves the focus; Ctrl 1–4 from the keyboard land inside the page (DESIGN §4.5:
//               层级 on the tier's tab, the others on the page), never on <body>; with the focus
//               on a sidebar item it stays there
//   motion      prefers-reduced-motion: reduce, and the shell's injected window.__TESSERACT_MOTION__
//               = {reduced: true} (T-P4-C3 item 7) with the media at no-preference: T.motion.reduced,
//               html.reduced-motion, CSS transitions collapsed, every animation of a page switch
//               ≤ 1 ms; and the control (no-preference, nothing injected) keeps its motion
//   selftest    the checks find what they look for: a planted unnamed button (axNames), a planted
//               CJK aria-label and a planted bound attribute left unrewritten in pseudo (the lang
//               block's own langSnap / staleBound)
//   shell       (--shell) the same motion checks in the debug shell started with
//               TESSERACT_REDUCED_MOTION=1 (custom scope, stand-in sidecar; ui/test/perf/shell.mjs's
//               launcher; --build rebuilds the shell first)
// Evidence: .build/<work orders>/evidence/T-P4-09/a11y-<stamp>/result.json. Exit 0 when every check
// passed. Screen readers (NVDA, Narrator) are P7.
import { chromium } from 'playwright-core';
import { readFileSync, rmSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const UI = resolve(HERE, '..', '..');
const REPO = resolve(UI, '..');
const R = await import(pathToFileURL(join(UI, 'test', 'render', 'run.mjs')).href);
const { EVIDENCE: PERF_EVIDENCE, writeJson, stamp, acquireLock } = await import(pathToFileURL(join(UI, 'test', 'perf', 'common.mjs')).href);
const CLOCK = join(UI, 'test', 'render', 'fixture_clock.js');
const SCEN = join(UI, 'test', 'fixtures', 'scenarios');
const scenario = name => JSON.parse(readFileSync(join(SCEN, `${name}.json`), 'utf8'));
const epochOf = sc => { const s = sc.steps.find(x => x.deliver && x.deliver.epoch); return s ? s.deliver.epoch : 'rt-fixture-e1'; };

function parseArgs(argv) {
  const o = { dist: null, only: null, shell: false, build: false, keep: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dist') o.dist = argv[++i];
    else if (a === '--only') o.only = argv[++i].split(',');
    else if (a === '--shell') o.shell = true;
    else if (a === '--build') o.build = true;
    else if (a === '--keep') o.keep = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return o;
}

// ------------------------------------------------------------------------------------ the page
// (no ?lang: a host:language frame reaches the page as in the product; FakeTransport would
// rewrite it to ?lang's language)
async function open(browser, dist, sc, page, { clock = true, reducedMotion = 'reduce', inject = null } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, reducedMotion, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light' });
  await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
  if (inject) await context.addInitScript(m => { Object.defineProperty(window, '__TESSERACT_MOTION__', { value: Object.freeze(m), configurable: false, writable: false }); }, inject);
  if (clock) await context.addInitScript({ path: CLOCK });
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, JSON.stringify(typeof sc === 'string' ? scenario(sc) : sc));
  const p = await context.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(String(e && e.message || e).slice(0, 300)));
  await p.goto(pathToFileURL(join(dist, 'index.html')).href + '#' + page, { waitUntil: 'load' });
  await p.waitForFunction(() => window.T && window.T.rt && window.T.rt.state === 'live', null, { timeout: 15000 });
  await p.evaluate(() => document.fonts.ready);
  await tick(p, clock ? 3000 : 0);
  return { context, page: p, errors, clock };
}
// virtual time (fixture clock) and a little real time (reduced-motion WAAPI animations are 1 ms)
async function tick(p, ms = 400) {
  await p.evaluate(m => (window.__fixtureClock ? window.__fixtureClock.advance(m) : new Promise(r => setTimeout(r, Math.min(m, 600)))), ms);
  await p.waitForTimeout(120);
  await p.evaluate(() => (window.__fixtureClock ? window.__fixtureClock.advance(0) : null));
}
const deliver = (p, frame) => p.evaluate(f => window.__TESSERACT_DESIGN__.deliver(f), frame);
const active = p => p.evaluate(() => {
  const a = document.activeElement;
  if (!a || a === document.body) return { body: true };
  return { tag: a.tagName.toLowerCase(), id: a.id || null, cls: typeof a.className === 'string' ? a.className.slice(0, 60) : null, page: a.closest('.page') ? a.closest('.page').dataset.page : null,
    region: a.closest('.side') ? 'side' : a.closest('.topbar') ? 'topbar' : a.closest('.ovl') ? 'overlay' : a.closest('.cmdk') ? 'palette' : a.closest('.page') ? 'page' : 'other', name: (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 40) };
});

// ------------------------------------------------------------------------------------ results
const results = [];
let muted = false;            // (the self-test's planted failures: recorded inverted, not printed)
function check(block, name, ok, detail) {
  results.push({ block, name, ok: !!ok, detail: detail === undefined ? null : detail });
  if (!muted) console.log(`${ok ? 'ok  ' : 'FAIL'} [${block}] ${name}${!ok && detail ? ' ' + JSON.stringify(detail).slice(0, 400) : ''}`);
}

// ------------------------------------------------------------------------------------ names
// widget roles (WAI-ARIA 1.2) and focusable nodes: each needs a name
const WIDGET = new Set(['button', 'checkbox', 'combobox', 'link', 'listbox', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'searchbox',
  'slider', 'spinbutton', 'switch', 'tab', 'textbox', 'treeitem', 'gridcell', 'row', 'menu', 'menubar', 'radiogroup', 'tablist', 'tree', 'grid', 'dialog', 'alertdialog', 'scrollbar']);
// containers whose name is optional when their items are named (ARIA: name recommended, not required)
const CONTAINER_OK = new Set(['listbox', 'menu', 'menubar', 'radiogroup', 'tablist', 'tree', 'grid', 'row', 'gridcell', 'scrollbar']);
async function axNames(page, label) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Accessibility.enable');
  const { nodes } = await cdp.send('Accessibility.getFullAXTree');
  const bad = [];
  let counted = 0;
  for (const n of nodes) {
    if (n.ignored) continue;
    const role = n.role && n.role.value;
    const props = Object.fromEntries((n.properties || []).map(p => [p.name, p.value && p.value.value]));
    const focusable = props.focusable === true;
    if (!WIDGET.has(role) && !focusable) continue;
    if (role === 'RootWebArea' || role === 'WebArea') continue;
    counted++;
    const name = n.name && typeof n.name.value === 'string' ? n.name.value.trim() : '';
    if (name) continue;
    if (CONTAINER_OK.has(role) && !focusable) continue;
    let where = null;
    if (n.backendDOMNodeId) {
      try {
        const { object } = await cdp.send('DOM.resolveNode', { backendNodeId: n.backendDOMNodeId });
        const r = await cdp.send('Runtime.callFunctionOn', { objectId: object.objectId, returnByValue: true,
          functionDeclaration: 'function () { const e = this.nodeType === 1 ? this : this.parentElement; if (!e) return null; const p = e.closest(".page"); return (p ? p.dataset.page + " " : "") + e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (typeof e.className === "string" && e.className ? "." + e.className.trim().split(/\\s+/).slice(0, 3).join(".") : "") + " " + (e.outerHTML || "").slice(0, 160); }' });
        where = r.result && r.result.value;
      } catch (e) { where = `backend ${n.backendDOMNodeId}`; }
    }
    bad.push({ role, focusable, where });
  }
  await cdp.detach();
  check('names', label, bad.length === 0, bad.length ? { unnamed: bad.slice(0, 12), count: bad.length, interactive: counted } : { interactive: counted });
  return bad;
}
const NAME_STATES = [
  { label: '总览 running', sc: 'overview_running', page: 'overview' },
  { label: '总览 stopped', sc: 'overview_stopped', page: 'overview' },
  { label: '总览 palette open', sc: 'overview_running', page: 'overview', act: async p => { await p.keyboard.press('Control+K'); await tick(p); } },
  { label: '总览 toast and banner', sc: 'overview_running', page: 'overview', act: async p => { await p.evaluate(() => { window.T.notify({ kind: 'info', title: 'A', message: 'b', actions: [{ label: 'OK', run: () => {} }] }); window.T.notify({ kind: 'warning', id: 'a11y-b', title: 'A', message: 'b' }); }); await tick(p); } },
  { label: '层级 running', sc: 'tiers_running', page: 'profiles' },
  { label: '层级 node panel', sc: 'tiers_running', page: 'profiles', act: async p => { await p.click('.tiers-row[data-key]'); await tick(p, 800); } },
  { label: '运行日志 key events', sc: 'logs_i18n_running', page: 'logs' },
  { label: '运行日志 raw grid', sc: 'logs_i18n_running', page: 'logs', act: async p => { await p.click('#logs-view [data-v="raw"]'); await tick(p); } },
  { label: '运行日志 reader', sc: 'logs_i18n_running', page: 'logs', act: async p => { await p.click('#logs-events .logs-row[data-key]'); await tick(p, 800); } },
  { label: '配置 running', sc: 'settings_i18n_running', page: 'settings' },
  { label: '配置 draft (save bar)', sc: 'settings_i18n_running', page: 'settings', act: async p => { await p.click('.switch[data-k="lan"]'); await tick(p, 800); } },
  { label: '配置 channel drawer', sc: 'settings_i18n_running', page: 'settings', act: async p => { await p.evaluate(() => window.T.go('settings', { focus: 'alerts' })); await tick(p, 1500); await p.click('#set-ch-gal .set-ch'); await tick(p, 800); } },
];

// ------------------------------------------------------------------------------------ lang
async function langBlock(browser, dist) {
  for (const [label, sc, page] of [['总览', 'overview_running', 'overview'], ['配置', 'settings_i18n_running', 'settings']]) {
    const { context, page: p, errors } = await open(browser, dist, sc, page);
    try {
      const E = epochOf(scenario(sc));
      const snap = () => p.evaluate(langSnap);
      const before = await snap();
      check('lang', `${label}: <html lang> zh-CN at start`, before.lang === 'zh-CN', before.lang);
      await deliver(p, { t: 'host:language', epoch: E, language: 'pseudo' });
      await tick(p, 800);
      const ps = await snap();
      check('lang', `${label}: <html lang> follows the switch to pseudo`, ps.i18n === 'pseudo' && ps.lang && ps.lang !== 'zh-CN', { html: ps.lang, i18n: ps.i18n });
      const stale = staleBound(ps);
      check('lang', `${label}: every data-i18n-attr attribute is rewritten (${ps.bound.length})`, stale.length === 0, stale.slice(0, 10));
      check('lang', `${label}: no aria-label / title / placeholder / data-tip keeps a CJK word in pseudo`, ps.loose.length === 0, ps.loose.slice(0, 10));
      await deliver(p, { t: 'host:language', epoch: E, language: 'zh_CN' });
      await tick(p, 800);
      const back = await snap();
      const left = back.bound.filter(b => b.value.includes('\u27e6'));
      check('lang', `${label}: back to zh_CN: <html lang> zh-CN and no pseudo word left in the bound attributes`, back.lang === 'zh-CN' && left.length === 0, { lang: back.lang, left: left.slice(0, 6) });
      check('lang', `${label}: no page error`, !errors.length, errors);
    } finally { await context.close(); }
  }
}

// in-page: the attributes data-i18n-attr binds ({attr, value, where}) and every aria-label / title /
// placeholder / data-tip outside translate="no" and [hidden] holding a CJK character (loose): the
// lang block and the self-test run this same scan (review S6)
function langSnap() {
  const CJK = /[\u3400-\u9fff\uf900-\ufaff]/;
  const bound = [], loose = [];
  for (const el of document.querySelectorAll('[data-i18n-attr]')) {
    for (const pair of el.getAttribute('data-i18n-attr').split(';')) {
      const at = pair.indexOf(':'); if (at < 0) continue;
      const attr = pair.slice(0, at).trim();
      bound.push({ attr, value: el.getAttribute(attr) || '', where: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/)[0] : '') });
    }
  }
  for (const el of document.querySelectorAll('[aria-label], [title], [placeholder], [data-tip]')) {
    if (el.closest('[translate="no"]') || el.closest('[hidden]')) continue;
    for (const attr of ['aria-label', 'title', 'placeholder', 'data-tip']) { const v = el.getAttribute(attr); if (v && CJK.test(v)) loose.push({ attr, value: v.slice(0, 40), where: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '') }); }
  }
  return { lang: document.documentElement.lang, i18n: window.T.i18n.lang, bound, loose };
}
// the bound attributes not rewritten into pseudo (no \u27e6 bracket)
const staleBound = s => s.bound.filter(b => b.value && !b.value.includes('\u27e6'));

// ------------------------------------------------------------------------------------ live
async function liveBlock(browser, dist) {
  const { context, page: p, errors } = await open(browser, dist, 'overview_running', 'overview');
  try {
    const hosts = await p.evaluate(() => {
      const live = el => { const r = el && el.closest('[aria-live], [role="status"], [role="alert"], [role="log"]'); return r ? (r.getAttribute('aria-live') || r.getAttribute('role')) : null; };
      return { toast: live(document.getElementById('toast-slot')), banners: live(document.getElementById('banners')), pill: live(document.querySelector('#ov-run .run-read, .run .run-read, [data-run-pill] .run-read') || document.querySelector('.run-row')) };
    });
    check('live', 'the toast host is an aria-live region', !!hosts.toast, hosts);
    check('live', 'the banner host is an aria-live region', !!hosts.banners, hosts);
    check('live', 'the run pill\'s state is inside an aria-live region', !!hosts.pill, hosts);
    // a notice never takes the focus
    await p.focus('.nav-item[data-nav="logs"]');
    const f0 = await active(p);
    await p.evaluate(() => { window.T.notify({ kind: 'info', title: 'A', message: 'toast', actions: [{ label: 'OK', run: () => {} }] }); window.T.notify({ kind: 'error', id: 'a11y-live', title: 'A', message: 'banner' }); });
    await tick(p);
    const f1 = await active(p);
    const shown = await p.evaluate(() => ({ toast: !!document.querySelector('#toast-slot .toast'), banner: !!document.querySelector('#banners .banner') }));
    check('live', 'a toast and a banner leave the focus where it was', shown.toast && shown.banner && JSON.stringify(f0) === JSON.stringify(f1), { before: f0, after: f1, shown });
    const inLive = await p.evaluate(() => ({ toast: !!(document.querySelector('#toast-slot .toast') || {}).closest?.('[aria-live]'), banner: !!(document.querySelector('#banners .banner') || {}).closest?.('[aria-live]') }));
    check('live', 'the toast and the banner shown are inside their live regions', inLive.toast && inLive.banner, inLive);
    check('live', 'no page error', !errors.length, errors);
  } finally { await context.close(); }
}

// ------------------------------------------------------------------------------------ focus
async function trapCheck(p, label, panelSel, presses = 24) {
  const inside = sel => p.evaluate(s => { const pn = document.querySelector(s); return !!(pn && document.activeElement && pn.contains(document.activeElement)); }, sel);
  let out = 0;
  for (let i = 0; i < presses; i++) { await p.keyboard.press('Tab'); if (!(await inside(panelSel))) out++; }
  for (let i = 0; i < presses; i++) { await p.keyboard.press('Shift+Tab'); if (!(await inside(panelSel))) out++; }
  check('focus', `${label}: Tab and Shift+Tab stay inside (${presses * 2} presses)`, out === 0, { outside: out, at: await active(p) });
}
async function focusBlock(browser, dist) {
  // the channel drawer (modal)
  {
    const { context, page: p } = await open(browser, dist, 'settings_i18n_running', 'settings');
    try {
      await p.evaluate(() => window.T.go('settings', { focus: 'alerts' }));
      await tick(p, 1500);
      await p.focus('#set-ch-gal .set-ch');
      const opener = await active(p);
      await p.keyboard.press('Enter');
      await tick(p, 800);
      const at = await active(p);
      check('focus', 'channel drawer: the focus moves into it', at.region === 'overlay', at);
      await trapCheck(p, 'channel drawer', '.ovl .drawer');
      await p.keyboard.press('Escape');
      await tick(p, 800);
      const back = await active(p);
      check('focus', 'channel drawer: Esc closes it and the focus goes back to its opener', JSON.stringify(back) === JSON.stringify(opener) && !(await p.evaluate(() => !!document.querySelector('.ovl .drawer'))), { opener, back });
    } finally { await context.close(); }
  }
  // a confirm dialog (T.confirm), opened from a focused control
  {
    const { context, page: p } = await open(browser, dist, 'overview_running', 'overview');
    try {
      await p.focus('#cmd-open');
      const opener = await active(p);
      await p.evaluate(() => { window.__conf = window.T.confirm({ title: 'A', message: 'b', confirmLabel: 'C' }); });
      await tick(p, 600);
      const at = await active(p);
      check('focus', 'confirm dialog: the focus moves into it', at.region === 'overlay', at);
      await trapCheck(p, 'confirm dialog', '.ovl .dlg');
      await p.keyboard.press('Escape');
      await tick(p, 800);
      const back = await active(p);
      check('focus', 'confirm dialog: Esc closes it and the focus goes back to its opener', JSON.stringify(back) === JSON.stringify(opener), { opener, back });
    } finally { await context.close(); }
  }
  // the palette, Esc on a toast, Ctrl 1–4
  {
    const { context, page: p } = await open(browser, dist, 'tiers_running', 'overview');
    try {
      await p.focus('.nav-item[data-nav="overview"]');
      const opener = await active(p);
      await p.keyboard.press('Control+K');
      await tick(p);
      const inPal = await p.evaluate(() => { const a = document.activeElement; return !!(a && a.closest('.cmdk') && a.tagName === 'INPUT'); });
      check('focus', 'Ctrl K: the focus lands in the palette\'s field', inPal, await active(p));
      await p.keyboard.press('Tab'); await p.keyboard.press('Tab');
      check('focus', 'palette: Tab keeps the focus in its field', await p.evaluate(() => !!(document.activeElement && document.activeElement.closest('.cmdk'))), await active(p));
      await p.keyboard.press('Escape');
      await tick(p, 600);
      const back = await active(p);
      check('focus', 'palette: Esc closes it and the focus goes back where it was', JSON.stringify(back) === JSON.stringify(opener), { opener, back });
      // Esc on a toast
      await p.evaluate(() => window.T.notify({ kind: 'info', title: 'A', message: 'toast' }));
      await tick(p);
      const f0 = await active(p);
      await p.keyboard.press('Escape');
      await tick(p, 600);
      const gone = await p.evaluate(() => !window.T.notices.toast);
      check('focus', 'Esc dismisses the toast and leaves the focus', gone && JSON.stringify(await active(p)) === JSON.stringify(f0), { gone, before: f0, after: await active(p) });
      // Ctrl 1–4 from the keyboard, the focus on <body> (a click on an empty spot first)
      for (const [k, id] of [['2', 'profiles'], ['3', 'logs'], ['4', 'settings'], ['1', 'overview']]) {
        await p.evaluate(() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); });
        await p.keyboard.press(`Control+${k}`);
        await tick(p, 1200);
        const a = await active(p);
        const cur = await p.evaluate(() => window.T.router.current);
        const ok = cur === id && !a.body && a.page === id;
        const tierTab = id !== 'profiles' || (await p.evaluate(() => { const a = document.activeElement; return !!(a && (a.getAttribute('role') === 'tab' || a.closest('[role="tablist"]'))); }));
        check('focus', `Ctrl ${k}: ${id} shown and the focus lands inside it${id === 'profiles' ? ' (on the tier\'s tab)' : ''}`, ok && tierTab, { current: cur, active: a });
      }
      // (review S5) DESIGN §4.5: the focus already in the sidebar stays where it is
      await p.focus('.nav-item[data-nav="profiles"]');
      const onNav = await active(p);
      await p.keyboard.press('Control+3');
      await tick(p, 1200);
      const a3 = await active(p), cur3 = await p.evaluate(() => window.T.router.current);
      check('focus', 'Ctrl 3 with the focus on a sidebar item: logs shown and the focus stays on that item', onNav.region === 'side' && cur3 === 'logs' && JSON.stringify(a3) === JSON.stringify(onNav), { current: cur3, before: onNav, after: a3 });
    } finally { await context.close(); }
  }
}

// ------------------------------------------------------------------------------------ motion
// in-page: the motion state, a transition duration and the animations of a page switch
async function motionFacts(p) {
  return p.evaluate(async () => {
    const T = window.T;
    const btn = document.querySelector('.btn') || document.querySelector('button');
    const cs = btn ? getComputedStyle(btn) : null;
    const dur = s => Math.max(0, ...String(s || '0s').split(',').map(x => (x.trim().endsWith('ms') ? parseFloat(x) : parseFloat(x) * 1000)));
    const before = new Set(document.getAnimations());
    T.go('settings');
    await new Promise(r => requestAnimationFrame(() => r()));
    const anims = document.getAnimations().filter(a => !before.has(a)).map(a => { const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {}; return { d: Math.round(Number(t.duration) || 0), delay: Math.round(Number(t.delay) || 0), target: a.effect && a.effect.target ? a.effect.target.className && typeof a.effect.target.className === 'string' ? a.effect.target.className.split(' ')[0] : a.effect.target.tagName : null }; });
    return { reduced: T.motion.reduced, htmlClass: document.documentElement.classList.contains('reduced-motion'), mediaReduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
      transition: cs ? dur(cs.transitionDuration) : null, anims, longest: Math.max(0, ...anims.map(a => a.d + a.delay)) };
  });
}
async function motionBlock(browser, dist) {
  const cases = [
    { label: 'control (no-preference, nothing injected)', reducedMotion: 'no-preference', inject: null, want: false },
    { label: 'prefers-reduced-motion: reduce', reducedMotion: 'reduce', inject: null, want: true },
    { label: 'the shell\'s __TESSERACT_MOTION__ {reduced:true} (media no-preference)', reducedMotion: 'no-preference', inject: { reduced: true }, want: true },
  ];
  for (const c of cases) {
    const { context, page: p, errors } = await open(browser, dist, 'settings_i18n_running', 'overview', { clock: false, reducedMotion: c.reducedMotion, inject: c.inject });
    try {
      const m = await motionFacts(p);
      if (c.want) {
        check('motion', `${c.label}: T.motion.reduced and html.reduced-motion`, m.reduced === true && m.htmlClass, m);
        check('motion', `${c.label}: CSS transitions collapse (≤ 0.01 ms)`, m.transition !== null && m.transition <= 0.01, { transition_ms: m.transition });
        check('motion', `${c.label}: a page switch animates nothing longer than 1 ms`, m.anims.every(a => a.d <= 1 && a.delay === 0), { count: m.anims.length, sample: m.anims.slice(0, 6) });
      } else {
        check('motion', `${c.label}: motion stays (T.motion.reduced false, transitions and the entrance stagger run)`, m.reduced === false && !m.htmlClass && m.transition > 0 && m.longest > 100, { reduced: m.reduced, transition_ms: m.transition, longest: m.longest, count: m.anims.length });
      }
      check('motion', `${c.label}: no page error`, !errors.length, errors);
    } finally { await context.close(); }
  }
}
async function shellMotion(o, evidence) {
  const S = await import(pathToFileURL(join(UI, 'test', 'perf', 'shell.mjs')).href);
  const facts = {};
  if (o.build) S.buildShell(facts);
  const run = await S.launchScoped({ env: { TESSERACT_REDUCED_MOTION: '1' }, evidence, log: console.log });
  try {
    const p = run.sess.page;
    const logged = await run.sess.logWait(/^reduced_motion reduced=true$/, 'reduced_motion reduced=true', 10000).then(() => true, () => false);
    check('shell', 'TESSERACT_REDUCED_MOTION=1: the shell logs reduced_motion reduced=true', logged);
    const inj = await p.evaluate(() => ({ value: window.__TESSERACT_MOTION__, frozen: Object.isFrozen(window.__TESSERACT_MOTION__) }));
    check('shell', 'the frozen window.__TESSERACT_MOTION__ = {reduced:true} is injected', inj.value && inj.value.reduced === true && inj.frozen, inj);
    const m = await motionFacts(p);
    check('shell', 'WebView2: T.motion.reduced and html.reduced-motion', m.reduced === true && m.htmlClass, { reduced: m.reduced, htmlClass: m.htmlClass, media: m.mediaReduce });
    check('shell', 'WebView2: CSS transitions collapse (≤ 0.01 ms)', m.transition !== null && m.transition <= 0.01, { transition_ms: m.transition });
    check('shell', 'WebView2: a page switch animates nothing longer than 1 ms', m.anims.every(a => a.d <= 1 && a.delay === 0), { count: m.anims.length, sample: m.anims.slice(0, 6) });
  } finally {
    const c = await run.close(o.keep);
    check('shell', 'the shell quits from the tray, nothing of this scope left', c.ok, c);
  }
}

// ------------------------------------------------------------------------------------ self-test
// the checks are not vacuous: a planted unnamed button, a planted CJK aria-label and a planted
// stale bound attribute are each found by the blocks' own scans (axNames; langSnap and staleBound
// in pseudo); the planted button's failing result is recorded under 'selftest', inverted
async function selfTest(browser, dist) {
  const { context, page: p } = await open(browser, dist, 'overview_running', 'overview');
  try {
    await p.evaluate(() => { const b = document.createElement('button'); b.className = 'a11y-plant'; document.querySelector('.page[data-page="overview"]').append(b); });
    const before = results.length;
    muted = true;
    const bad = await axNames(p, 'self-test: planted unnamed button (expected to fail)').finally(() => { muted = false; });
    results.splice(before);
    check('selftest', 'names: a planted unnamed <button> is reported', bad.length === 1 && /a11y-plant/.test(bad[0].where || ''), bad.slice(0, 2));
    // (review S6) the lang block's own scan (langSnap, staleBound), in pseudo: a planted CJK
    // aria-label, and a planted bound attribute left in zh_CN (not rewritten by the switch)
    await deliver(p, { t: 'host:language', epoch: epochOf(scenario('overview_running')), language: 'pseudo' });
    await tick(p, 800);
    const clean = await p.evaluate(langSnap);
    await p.evaluate(() => {
      const pg = document.querySelector('.page[data-page="overview"]');
      const a = document.createElement('span'); a.setAttribute('aria-label', '启动'); a.id = 'a11y-plant-cjk'; pg.append(a);
      const s = document.createElement('span'); s.setAttribute('data-i18n-attr', 'title:shell.page.overview.title'); s.setAttribute('title', '总览'); s.id = 'a11y-plant-stale'; pg.append(s);
    });
    const dirty = await p.evaluate(langSnap);
    const loose = dirty.loose.filter(x => /a11y-plant/.test(x.where)).map(x => x.where), stale = staleBound(dirty).filter(x => /a11y-plant/.test(x.where)).map(x => x.where);
    check('selftest', 'lang: the scan is clean in pseudo before the plants (the lang block checks would pass here)', clean.i18n === 'pseudo' && !clean.loose.length && !staleBound(clean).length, { i18n: clean.i18n, loose: clean.loose.slice(0, 4), stale: staleBound(clean).slice(0, 4) });
    check('selftest', 'lang: langSnap reports a planted CJK aria-label outside translate="no"', loose.includes('span#a11y-plant-cjk'), loose);
    check('selftest', 'lang: staleBound reports a planted bound attribute left unrewritten', stale.includes('span#a11y-plant-stale') && loose.includes('span#a11y-plant-stale'), { stale, loose });
  } finally { await context.close(); }
}

// ------------------------------------------------------------------------------------ main
async function main() {
  const o = parseArgs(process.argv.slice(2));
  // (review B S1) not while a perf run measures (and no perf run while this one, --shell above all,
  // runs): the measuring lock of ui/test/perf/common.mjs
  const held = acquireLock(`a11y${o.shell ? ' --shell' : ''}`);
  const evidence = join(PERF_EVIDENCE, `a11y-${stamp()}`);
  const src = o.dist ? resolve(o.dist) : join(UI, 'dist', 'design');
  R.readManifest(src);
  const snap = R.snapshotDist(src);
  const browser = await chromium.launch({ executablePath: R.CHROME, headless: true, args: ['--host-resolver-rules=MAP * ~NOTFOUND'] });
  const want = b => !o.only || o.only.includes(b);
  let error = null;
  try {
    if (want('names')) for (const st of NAME_STATES) {
      const { context, page, errors } = await open(browser, snap.dir, st.sc, st.page);
      try { if (st.act) await st.act(page); await axNames(page, st.label); if (errors.length) check('names', `${st.label}: no page error`, false, errors); } finally { await context.close(); }
    }
    if (want('lang')) await langBlock(browser, snap.dir);
    if (want('live')) await liveBlock(browser, snap.dir);
    if (want('focus')) await focusBlock(browser, snap.dir);
    if (want('motion')) await motionBlock(browser, snap.dir);
    if (want('selftest')) await selfTest(browser, snap.dir);
  } catch (e) { error = String(e && e.stack || e); console.error(error); }
  await browser.close();
  rmSync(snap.dir, { recursive: true, force: true });
  if (o.shell && !error) { try { await shellMotion(o, evidence); } catch (e) { error = String(e && e.stack || e); console.error(error); } }
  const failed = results.filter(r => !r.ok);
  const out = { ticket: 'T-P4-09', chrome: R.CHROME, build: { source_digest: snap.manifest.source_digest || null, flags: snap.manifest.flags || null }, ended_at: new Date().toISOString(), error, checks: results.length, failed: failed.length, results };
  writeJson(join(evidence, 'result.json'), out);
  held.release();
  console.log(`a11y: ${!error && !failed.length ? 'OK' : 'FAILED'} — ${results.length - failed.length}/${results.length} checks → ${relative(REPO, join(evidence, 'result.json')).split('\\').join('/')}`);
  return !error && !failed.length ? 0 : 1;
}
main().then(c => { process.exitCode = c; }, e => { console.error(e && e.stack || e); process.exitCode = 1; });
