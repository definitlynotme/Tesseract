// T-P4-11 end-to-end driver: playwright-core `chromium.connectOverCDP` to the debug shell's
// WebView2 (TESSERACT_SHELL_CDP_PORT, loopback, debug builds only), plus the facts a flow needs
// around the page: the scoped shell.log, the stand-in sidecar's control directory and request
// summaries (shell/app/src/p4_ui_fixture.py), native window actions (native.py) and screenshots.
//
// scripts/p4_ui_e2e.ps1 owns the process side (sandbox, custom scope, launch under
// process_watch.py, tray quit, residue checks); this module never starts or kills a process and
// never closes the browser (the connection is dropped, the WebView stays the shell's).
//
// The page is instrumented only through E2E_INIT (Page.addScriptToEvaluateOnNewDocument), which
// takes effect on the next load of the page (flows reload once when they need a load observed
// from its first frame): it records the bus events a flow asserts on (names and a few scalar
// fields, never frame payloads) and every change of #app[data-run] / data-stale.
//
// Shared with T-P4-08b: ui/test/e2e/csp.mjs imports REPO, openSession, waitFor, sleep, goPage,
// settingsReady, makeDraft, undoDrafts, writeJson, Failure and check, and uses the Session members
// control, eval, until, page, shot, reloadObserved and detach. Their signatures and behaviour only
// grow (new optional parameters, new exports); never rename, remove or change them without
// changing csp.mjs in the same step.
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(HERE, '..', '..', '..');
const { chromium } = await import(pathToPlaywright());

function pathToPlaywright() {
  return new URL('file:///' + join(REPO, 'ui', 'node_modules', 'playwright-core', 'index.mjs').replace(/\\/g, '/')).href;
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));

// Recorded on the page (window.__e2e): bus events with a few scalar fields, #app run/stale changes.
export const E2E_INIT = `(() => {
  if (window.__e2e) return;
  const t0 = performance.now();
  const rec = window.__e2e = { events: [], runs: [], t0: Date.now(), t0p: t0 };
  const KEEP = ['reason', 'epoch', 'generation_changed', 'state', 'prev', 'run', 'instant', 'kind', 'page', 'visible', 'language', 'delivery_id', 'topic', 'key', 'stale', 'token', 'wasFallback', 'source'];
  const note = (e, p) => {
    if (!/^(rt:|host:|page:change|i18n:change|shellc:list-stale)/.test(e)) return;
    const o = { e, at: Math.round(performance.now() - t0) };
    if (p && typeof p === 'object') for (const k of KEEP) if (k in p && (p[k] === null || typeof p[k] !== 'object')) o[k] = p[k];
    if (e === 'host:caps' && p) { o.invokes = (p.invokes || []).slice(); o.events = (p.events || []).slice(); }
    if (e === 'page:change' && p) o.id = p.id;
    rec.events.push(o);
    if (rec.events.length > 5000) rec.events.splice(0, 1000);
  };
  // core.js does "const T = window.T = window.T || {}" and then assigns T.emit: catch that first
  // assignment only and put a plain property there (later wrappers, e.g. rt/alias.js, call it).
  const T = window.T = window.T || {};
  Object.defineProperty(T, 'emit', { configurable: true, enumerable: true, get: () => undefined, set: fn => {
    Object.defineProperty(T, 'emit', { configurable: true, enumerable: true, writable: true, value: (e, p) => { try { note(e, p); } catch (err) { } return fn(e, p); } });
  } });
  const runOf = () => { const a = document.getElementById('app'); return a ? { run: a.dataset.run, stale: a.hasAttribute('data-stale') } : null; };
  // (skel: skeleton placeholders in the document at that moment, T.ui.skeleton / logs L-1)
  const push = () => { const r = runOf(); if (!r) return; const last = rec.runs[rec.runs.length - 1]; if (!last || last.run !== r.run || last.stale !== r.stale) rec.runs.push({ ...r, skel: document.querySelectorAll('.skel').length, at: Math.round(performance.now() - t0) }); };
  new MutationObserver(push).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-run', 'data-stale'] });
})();`;

export function parseArgs(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) continue;
    const k = a.slice(2);
    const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
    o[k] = v;
  }
  return o;
}

export class Failure extends Error {}
export const fail = msg => { throw new Failure(msg); };
export const check = (ok, msg) => { if (!ok) fail(msg); };

export async function waitFor(fn, label, ms = 10000, every = 100) {
  const end = Date.now() + ms;
  let last;
  for (;;) {
    try { last = await fn(); if (last) return last; } catch (e) { last = e; }
    if (Date.now() > end) fail(`timeout (${ms} ms): ${label}${last instanceof Error ? ' [' + last.message + ']' : ''}`);
    await sleep(every);
  }
}

// ---------------------------------------------------------------------------------- the session
export async function openSession(opts) {
  const s = new Session(opts);
  await s.attach();
  return s;
}

class Session {
  constructor(opts) {
    this.o = opts;
    this.port = Number(opts.port);
    this.pid = Number(opts.pid);
    this.data = opts.data;
    this.state = join(opts.data, 'p4-ui-fixture');
    this.logFile = join(opts.data, 'logs', 'shell.log');
    this.evidence = opts.evidence;
    this.real = !!opts.real;
    this.python = opts.python || join(REPO, '.venv', 'Scripts', 'python.exe');
    this.checks = {};
    this.knownFails = [];
    this.knownFixed = [];
    this.controlNo = 0;
    this.shots = 0;
    mkdirSync(this.evidence, { recursive: true });
  }

  async attach() {
    this.browser = await waitFor(async () => {
      try { return await chromium.connectOverCDP(`http://127.0.0.1:${this.port}`, { timeout: 5000, isWebView: true, noDefaults: true }); } catch (e) { return null; }
    }, 'CDP endpoint', 45000, 300);
    await this.findPage();
  }

  // The app page (http://tauri.localhost/..., never the dev-rt page) once T.rt is live.
  async findPage(ms = 30000) {
    this.page = await waitFor(async () => {
      for (const ctx of this.browser.contexts()) for (const p of ctx.pages()) {
        const u = p.url();
        if (!u.startsWith('http://tauri.localhost/') || u.includes('dev-rt')) continue;
        const live = await p.evaluate(() => !!(window.T && window.T.rt && window.T.rt.state === 'live')).catch(() => false);
        if (live) return p;
      }
      return null;
    }, 'app page with T.rt live', ms, 200);
    if (this.cdp) await this.cdp.detach().catch(() => {});
    this.cdp = await this.page.context().newCDPSession(this.page);
    if (!this.initDone) { await this.page.addInitScript(E2E_INIT); this.initDone = true; }
    return this.page;
  }

  // A new WebView2 browser process (the shell rebuilt the window after the old one died): drop the
  // dead connection and attach to the new browser on the same CDP port; E2E_INIT goes into the new
  // browser for its next load.
  async reattach(ms = 45000) {
    try { if (this.cdp) await this.cdp.detach(); } catch (e) { /* gone */ }
    try { if (this.browser) await this.browser.close(); } catch (e) { /* gone */ }
    this.cdp = null; this.browser = null; this.initDone = false;
    this.browser = await waitFor(async () => {
      try { return await chromium.connectOverCDP(`http://127.0.0.1:${this.port}`, { timeout: 5000, isWebView: true, noDefaults: true }); } catch (e) { return null; }
    }, 'CDP endpoint of the new browser', ms, 300);
    await this.findPage(ms);
  }

  // Reload with E2E_INIT installed (the page is observed from its first frame).
  async reloadObserved() {
    await this.page.reload({ waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
    await this.findPage();
    await this.until(() => window.__e2e && window.T && window.T.rt.state === 'live', 'observed reload live', 20000);
  }

  // A check that fails today because of another ticket's unfinished work (named owner): its
  // failure is recorded as known_fail and the flow goes on; once it passes it is recorded as
  // known_fixed so the entry can be removed. Never used for this ticket's own contract.
  async known(id, owner, fn) {
    try { await fn(); this.knownFixed.push({ id, owner }); }
    catch (e) { if (!(e instanceof Failure)) throw e; this.knownFails.push({ id, owner, error: e.message }); }
  }

  // facts one launch of a flow leaves for the next (<evidence>/notes.json; the flow's evidence
  // directory is shared by its launches)
  note(key, value) {
    const file = join(this.evidence, 'notes.json');
    let all = {}; try { all = JSON.parse(readFileSync(file, 'utf8')); } catch (e) { all = {}; }
    all[key] = value; writeJson(file, all);
  }
  recall(key) { try { return JSON.parse(readFileSync(join(this.evidence, 'notes.json'), 'utf8'))[key] ?? null; } catch (e) { return null; } }

  eval(fn, arg) { return this.page.evaluate(fn, arg); }
  async until(fn, label, ms = 10000, arg) { return waitFor(() => this.page.evaluate(fn, arg), label, ms); }
  events(name) { return this.eval(n => (window.__e2e ? window.__e2e.events.filter(e => !n || e.e === n) : []), name || null); }
  runs() { return this.eval(() => (window.__e2e ? window.__e2e.runs.slice() : [])); }

  // ---- shell.log: "<ms> <code> k=v ..." (fixed codes only, no frames)
  logLines() {
    if (!existsSync(this.logFile)) return [];
    return readFileSync(this.logFile, 'utf8').split(/\r?\n/).filter(Boolean).map(l => { const m = /^(\d+) (.*)$/.exec(l); return m ? { ms: Number(m[1]), text: m[2] } : { ms: 0, text: l }; });
  }
  logFind(re) { return this.logLines().filter(l => re.test(l.text)); }
  async logWait(re, label, ms = 10000) { return waitFor(() => { const f = this.logFind(re); return f.length ? f : null; }, label || String(re), ms); }

  // ---- the stand-in sidecar
  async control(op, ms = 10000) {
    check(!this.real, `fixture control "${op.op}" needs the stand-in sidecar`);
    const dir = join(this.state, 'control');
    mkdirSync(dir, { recursive: true });
    const name = `${process.pid}-${String(++this.controlNo).padStart(4, '0')}`;
    writeFileSync(join(dir, name + '.tmp'), JSON.stringify(op));
    renameSync(join(dir, name + '.tmp'), join(dir, name + '.json'));
    await waitFor(() => existsSync(join(dir, name + '.done')) || existsSync(join(dir, name + '.error')), `fixture op ${op.op}`, ms, 50);
    if (existsSync(join(dir, name + '.error'))) fail(`fixture op ${op.op}: ${readFileSync(join(dir, name + '.error'), 'utf8')}`);
  }
  requests() {
    if (!existsSync(this.state)) return [];
    const out = [];
    for (const f of readdirSync(this.state).filter(n => /^requests-\d+\.jsonl$/.test(n)).sort((a, b) => parseInt(a.slice(9), 10) - parseInt(b.slice(9), 10))) {
      const launch = parseInt(f.slice(9), 10);
      for (const line of readFileSync(join(this.state, f), 'utf8').split('\n')) if (line.trim()) { try { out.push({ launch, ...JSON.parse(line) }); } catch (e) { /* torn last line */ } }
    }
    return out;
  }
  fixtureLaunches() { return existsSync(this.state) ? readdirSync(this.state).filter(n => /^launch-\d+$/.test(n)).length : 0; }

  // ---- native window actions (native.py), always on this shell's own windows
  native(action, extra = []) {
    const r = spawnSync(this.python, ['-B', join(HERE, 'native.py'), action, '--pid', String(this.pid), ...extra], { encoding: 'utf8', timeout: 20000 });
    if (r.status !== 0) fail(`native ${action}: ${(r.stderr || r.error || '').toString().slice(-400)}`);
    return JSON.parse(r.stdout.trim().split('\n').pop());
  }
  win() { return this.native('state').state; }

  // ---- evidence
  async shot(name) {
    const file = join(this.evidence, `${String(++this.shots).padStart(2, '0')}-${name}.png`);
    try { await this.page.screenshot({ path: file, timeout: 8000 }); return file; } catch (e) { return null; }
  }
  async metrics() {
    const r = await this.cdp.send('Performance.getMetrics');
    return Object.fromEntries(r.metrics.map(m => [m.name, m.value]));
  }

  async detach() {
    // Drop the CDP connection only: for a connectOverCDP browser, close() closes the transport
    // (playwright-core: browserProcess.close = the transport's closeAndWait); the WebView, its
    // window and the shell stay as they are.
    try { if (this.cdp) await this.cdp.detach(); } catch (e) { /* gone */ }
    try { if (this.browser) await this.browser.close(); } catch (e) { /* gone */ }
  }
}

export function writeJson(file, value) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value, null, 1) + '\n');
}

// ---------------------------------------------------------------------------------- page helpers
// (only the page's public surface: T.go / T.router / T.ui.saveBar / T.notices / T.t and the DOM)
export async function goPage(s, id, params = null) {
  await s.eval(([i, p]) => { window.T.go(i, p || {}); }, [id, params]);
  await s.until(i => window.T.router.current === i, `page ${id}`, 10000, id);
}
export const dirtyCount = s => s.eval(() => window.T.ui.saveBar.dirtyCount());
export const bannerUp = (s, id) => s.eval(i => !!(window.T.notices && window.T.notices.banners && window.T.notices.banners.has(i)), id);
export const htmlLang = s => s.eval(() => ({ html: document.documentElement.lang, i18n: window.T.i18n && window.T.i18n.lang, store: window.T.store.get('lang') }));
// 配置 with its snapshot applied (the contract rows editable)
export async function settingsReady(s) {
  await goPage(s, 'settings');
  await s.until(() => {
    const sw = document.querySelector('.switch[data-k="lan"], .switch[data-k="ipv6"]');
    return !!(document.querySelector('#set-lang') && sw && !sw.inert && !sw.closest('[inert]'));
  }, 'settings snapshot applied', 20000);
}
export const DRAFT_KEYS = ['lan', 'ipv6', 'egress', 'dnsRules'];
// one switch flipped: a draft of one item; -> the key
export async function makeDraft(s) {
  const key = await s.eval(keys => keys.find(k => { const e = document.querySelector(`.switch[data-k="${k}"]`); return e && !e.inert && !e.closest('[inert]') && e.getAttribute('aria-disabled') !== 'true'; }) || null, DRAFT_KEYS);
  if (!key) fail('no editable switch for a draft on 配置');
  await s.page.click(`.switch[data-k="${key}"]`);
  await waitFor(async () => (await dirtyCount(s)) > 0, 'a settings draft', 5000);
  return key;
}
export async function switchOn(s, key) { return s.eval(k => { const e = document.querySelector(`.switch[data-k="${k}"]`); return e ? e.getAttribute('aria-checked') === 'true' : null; }, key); }
export async function setSwitch(s, key, on) {
  await s.until(k => { const e = document.querySelector(`.switch[data-k="${k}"]`); return e && !e.inert && e.getAttribute('aria-disabled') !== 'true'; }, `switch ${key} editable`, 10000, key);
  if ((await switchOn(s, key)) !== on) await s.page.click(`.switch[data-k="${key}"]`);
  await s.until(([k, v]) => { const e = document.querySelector(`.switch[data-k="${k}"]`); return e && (e.getAttribute('aria-checked') === 'true') === v; }, `switch ${key}=${on}`, 5000, [key, on]);
}
export async function undoDrafts(s) {
  if ((await dirtyCount(s)) === 0) return;
  await s.page.click('.savebar .sb-undo');
  await waitFor(async () => (await dirtyCount(s)) === 0, 'drafts undone', 5000);
}
export async function saveSettings(s) {
  await s.page.click('.savebar .sb-save');
  await waitFor(async () => (await dirtyCount(s)) === 0, 'settings saved', 15000);
}
export async function chooseLanguage(s, lang) {
  const label = await s.eval(l => window.T.t(`settings.app.lang.name.${l.toLowerCase()}`), lang);
  await s.page.click('#set-lang');
  await s.page.locator('.menu [role="option"]').filter({ hasText: label }).first().click();
  return label;
}
export async function logsTotal(s) {
  return s.eval(() => { const e = document.getElementById('logs-total'); const n = e ? (e.textContent.replace(/[,.\s\u00a0\u202f']/g, '').match(/\d+/g) || []).map(Number) : []; return n.length ? Math.max(...n) : 0; });
}
// A banner on screen (core.js banner(): #banners > .banner[data-id]): its handle's element, title,
// message and how many elements with that id the document holds.
export const bannerInfo = (s, id) => s.eval(i => {
  const b = window.T.notices && window.T.notices.banners && window.T.notices.banners.get(i);
  const el = b && b.el;
  const t = el && el.querySelector('.banner-title > span'), m = el && el.querySelector('.banner-msg');
  return { up: !!b, connected: !!(el && el.isConnected), title: t ? t.textContent.trim() : null, message: m ? m.textContent.trim() : null,
    elements: document.querySelectorAll(`#banners .banner[data-id="${i}"]`).length };
}, id);

// A same-epoch reconnect through the page's own recovery path (flows/reconnect.mjs explains it): a
// stray Channel takes the shell's replay, the page's TauriTransport sees its own Channel stuck and
// reconnects. -> the event / run marks taken just before the trigger. Waits for the reconnect's
// rt:replayed.
export async function strayReconnect(s) {
  // (a Tauri-event rt:reset within 1.5 s of the Channel's own is taken as that Channel's copy:
  // start well after the page's last replay)
  await s.until(() => { const r = window.__e2e.events.filter(e => e.e === 'rt:reset').pop(); return !r || performance.now() - (r.at + (window.__e2e.t0p || 0)) > 2500; }, 'quiet after the last replay', 10000);
  const mark = await s.eval(() => { const n = window.__e2e.runs.length; const e = window.__e2e.events.length;
    const ch = new window.__TAURI__.core.Channel(); ch.onmessage = () => {};
    window.__e2eStray = ch; window.__TAURI__.core.invoke('rt_connect', { channel: ch }); return { runs: n, events: e }; });
  await s.until(m => window.__e2e.events.slice(m.events).some(e => e.e === 'rt:conn' && e.state === 'reconnecting'), 'the page reconnects (channel stuck)', 10000, mark);
  await s.until(m => window.__e2e.events.slice(m.events).some(e => e.e === 'rt:reset') && window.T.rt.state === 'live', 'same-epoch replay', 15000, mark);
  await s.until(m => window.__e2e.events.slice(m.events).some(e => e.e === 'rt:replayed'), 'rt:replayed of the reconnect', 15000, mark);
  return mark;
}

// This shell's WebView2 browser process: msedgewebview2.exe without --type, its user data under
// this scope's data directory and its parent this shell (never another WebView). -> pid | null
export function scopedBrowserPid(s) {
  const ps = `$d=$env:E2E_DATA; $p=[int]$env:E2E_SHELL; @(Get-CimInstance Win32_Process -Filter "name = 'msedgewebview2.exe'" | Where-Object { $_.ParentProcessId -eq $p -and $_.CommandLine -notmatch '--type=' -and $_.CommandLine.IndexOf($d, [StringComparison]::OrdinalIgnoreCase) -ge 0 } | ForEach-Object { $_.ProcessId }) -join ','`;
  const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps], { encoding: 'utf8', timeout: 30000, env: { ...process.env, E2E_DATA: s.data, E2E_SHELL: String(s.pid) } });
  if (r.status !== 0) fail(`browser process query: ${(r.stderr || '').slice(-300)}`);
  const pids = r.stdout.trim().split(',').filter(Boolean).map(Number);
  if (pids.length > 1) fail(`more than one browser process of this shell: ${pids}`);
  return pids[0] || null;
}
