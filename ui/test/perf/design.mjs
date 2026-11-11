// T-P4-09 performance, Chrome target (P4 architecture §10): playwright-core + the local Chrome on
// the design target in fixture mode (window.__TESSERACT_FIXTURE__, boot/rt-design.js), real time.
//
// The page is a private design build (--pseudo, without --i18n-trace (the trace adds a data attribute to
// every translated node: a cost the product does not have)) in a fresh temp directory, loaded from
// file: offline (every other request is aborted). No fixture clock: page timers, frames and idle
// callbacks run in real time as in the product; only the replay's own rt clock is the fixture's.
// 1280x800, device scale 1, zh_CN, full motion (prefers-reduced-motion: no-preference, the
// default user and the slower path). Every run is a fresh browser context.
//
// Items (one run each):
//   entry.<page>       load at another page, settle, a click on the page's sidebar item: the first
//                      entry of the page
// Every action reaches the page as a real input event or from a page task (a timer): work run
// inside a CDP Runtime.evaluate is not reported by the Long Tasks API (checked: an 80 ms loop in
// page.evaluate gives no entry, the same loop in a setTimeout callback gives one).
//   logs4000.load      logs_stress_4000 (4 seed pages, 4,000 lines, 500 live) + 50 lines/s live
//                      from the test: first entry of 运行日志
//   logs4000.filter    then: 关键事件 问题 -> 全部, 原始日志, 错误 -> 全部, back to 关键事件
//   logs4000.search    then: typing "example" key by key (110 ms apart) and erasing it, in both views
//   logs4000.leave     then: 总览 from 运行日志 holding the 4,000 lines
//   lang.overview      host:language zh_CN -> en_US -> zh_CN on 总览 (the first switch is cold: the
//                      catalog is loaded then); switch -> first frame. PERF_LANGS=a,b,… overrides
//                      the sequence (e.g. pseudo: every string changes, see the report)
//   lang.settings      the same on 配置 holding a draft (the draft must survive)
//   lang.logs4000      the same on 运行日志 holding the 4,000 lines of logs_stress_4000 (review M1)
//   hidden30           (once per round) 运行日志, host:visibility false, 30 s of 50 lines/s, CDP
//                      Performance LayoutCount / RecalcStyleCount deltas, then visible: the rows
//                      and the footer catch up in one write (the first list batch already holds the
//                      rows the view holds at the end; common.mjs installWrites)
import { chromium } from 'playwright-core';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { LANG_FRAME_MS, LONG_MS, PROBE, UI, installWrites, quiet, sleep, takeSince, writesEnd } from './common.mjs';

export const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const SCEN = join(UI, 'test', 'fixtures', 'scenarios');
const scenario = name => JSON.parse(readFileSync(join(SCEN, `${name}.json`), 'utf8'));
const epochOf = sc => { const s = sc.steps.find(x => x.deliver && x.deliver.epoch); return s ? s.deliver.epoch : 'rt-fixture-e1'; };

export function buildDesign() {
  const out = mkdtempSync(join(tmpdir(), 'tesseract-perf-design-'));
  // (--pseudo only adds the pseudo catalog, loaded only when that language is chosen: PERF_LANGS)
  const r = spawnSync(process.execPath, [join(UI, 'build.mjs'), '--target', 'design', '--out', out, '--pseudo'], { cwd: UI, encoding: 'utf8' });
  if (r.status !== 0) { rmSync(out, { recursive: true, force: true }); throw new Error(`design build failed:\n${r.stdout}\n${r.stderr}`); }
  const manifest = JSON.parse(readFileSync(join(out, 'build-manifest.json'), 'utf8'));
  return { dir: out, source_digest: manifest.source_digest || null, remove: () => rmSync(out, { recursive: true, force: true }) };
}

export async function launchChrome() {
  return chromium.launch({ executablePath: CHROME, headless: true, args: ['--host-resolver-rules=MAP * ~NOTFOUND'] });
}

// one fresh page on the fixture -> {context, page, errors, cdp}
async function open(browser, dist, sc, start, { reducedMotion = 'no-preference', viewport = { width: 1280, height: 800 } } = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, reducedMotion, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light' });
  await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
  await context.addInitScript(PROBE);
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, JSON.stringify(sc));
  // The design target is an i18n dev build: it warns on the console once for every message it falls
  // back to zh_CN for (the P4 en_US catalog is empty), and the console messages also cross the CDP
  // connection. The app target has neither: those warnings are dropped here (and only those).
  await context.addInitScript(() => { const w = console.warn; console.warn = function (...a) { if (typeof a[0] === 'string' && a[0].startsWith('[i18n]')) return undefined; return w.apply(this, a); }; });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e && e.message || e).slice(0, 300)));
  await page.goto(pathToFileURL(join(dist, 'index.html')).href + '#' + start, { waitUntil: 'load' });
  await page.waitForFunction(id => window.T && window.T.rt && window.T.rt.state === 'live' && window.T.router.current === id, start, { timeout: 20000 });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(quiet, 1200);
  return { context, page, errors };
}

const longestOf = l => Math.round(Math.max(0, ...l.lt.map(e => e.dur)));
// PERF_TRACE=1: the longest main-thread task of the window from a Chrome trace as well (RunTask on
// the page's renderer main thread, every task, also those under 50 ms): the margin under the limit.
// Informational, never the verdict (the trace itself costs a little).
const TRACE = process.env.PERF_TRACE === '1';
async function window_(page, fn, settleMs = 1500) {
  const browser = page.context().browser();
  if (TRACE) await browser.startTracing(page, { categories: ['devtools.timeline', 'disabled-by-default-devtools.timeline'] });
  const t0 = await page.evaluate(() => performance.now());
  const extra = await fn();
  await page.evaluate(quiet, settleMs);
  const l = await page.evaluate(takeSince, t0);
  const out = { longest: longestOf(l), lt: l.lt.map(e => e.dur), loaf: l.loaf, extra };
  if (TRACE) out.trace_longest = traceLongest(await browser.stopTracing());
  return out;
}
function traceLongest(buf) {
  let ev; try { ev = JSON.parse(buf.toString('utf8')).traceEvents || []; } catch (e) { return null; }
  const main = new Set(ev.filter(e => e.name === 'thread_name' && e.args && e.args.name === 'CrRendererMain').map(e => `${e.pid}:${e.tid}`));
  const tasks = ev.filter(e => e.ph === 'X' && e.name === 'RunTask' && main.has(`${e.pid}:${e.tid}`) && e.dur).map(e => e.dur / 1000);
  tasks.sort((a, b) => b - a);
  return { max_ms: tasks.length ? Math.round(tasks[0] * 10) / 10 : 0, top5_ms: tasks.slice(0, 5).map(x => Math.round(x * 10) / 10), tasks: tasks.length };
}

// in-page: a steady logs.batch stream through the fixture transport (10 lines every 200 ms), seq
// and rev continuing the scenario's
function feedLogs([epoch, fromSeq, rev, seq]) {
  if (window.__feed) return;
  let n = fromSeq, r = rev, q = seq;
  const D = window.__TESSERACT_DESIGN__;
  window.__feed = setInterval(() => {
    const now = Date.now(), records = [];
    for (let j = 0; j < 10; j++) {
      const s = n++;
      const key = s % 25 === 0;
      records.push(key
        ? { seq: s, ts_ms: now, rx_ms: now + 9, source: 'TESSERACT', level: s % 75 === 0 ? 'warning' : 'info', category: 'runtime', key: 'perf.key_line', title: `Perf key line ${s}`, detail: 'perf feed', raw: '', is_key_event: true, run_id: 'run-42' }
        : { seq: s, ts_ms: now, rx_ms: now + 9, source: 'MIHOMO', level: s % 37 === 0 ? 'warning' : 'info', category: 'connection', key: 'log.raw', title: '', detail: '', raw: `[TCP] 127.0.0.1:${50000 + (s % 10000)} --> h${s % 60}.example.net:443`, is_key_event: false, run_id: 'run-42' });
    }
    D.deliver({ t: 'evt', epoch, produced_at_ms: now, sent_at_ms: now + 1, topic: 'logs.batch', rev: r++, kind: 'edge', seq: q++, data: { from_seq: records[0].seq, records, dropped: 0, dropped_total: 0, gap: false } });
  }, 200);
}
function feedArgs(sc) {
  const b = sc.steps.filter(x => x.deliver && x.deliver.topic === 'logs.batch').map(x => x.deliver);
  const last = b[b.length - 1];
  const lastSeq = last ? last.data.records[last.data.records.length - 1].seq : 0;
  return [epochOf(sc), lastSeq + 1, (last ? last.rev : 0) + 1, (last && last.seq ? last.seq : 0) + 1];
}

// in-page: host:language through the fixture transport; -> {t_change, t_frame} (ms after the
// language store write, which is when the page learns of the switch; the frame is the first one
// rendered after i18n:change: a message posted from its rAF callback runs after its paint)
function switchLanguage([epoch, lang]) {
  return new Promise((res, rej) => {
    const T = window.T;
    let t0 = null;
    const offS = T.store.sub('lang', v => { if (v === lang && t0 === null) t0 = performance.now(); });
    const offC = T.on('i18n:change', p => {
      if (!p || p.lang !== lang) return;
      const tc = performance.now();
      offC && offC(); offS && offS();
      requestAnimationFrame(() => { const ch = new MessageChannel(); ch.port1.onmessage = () => res({ t_change: Math.round(tc - t0), t_frame: Math.round(performance.now() - t0), html_lang: document.documentElement.lang }); ch.port2.postMessage(0); });
    });
    setTimeout(() => rej(new Error(`no i18n:change to ${lang} in 5 s`)), 5000);
    // (from a task of its own: work run inside a CDP evaluation is not reported as a long task)
    setTimeout(() => window.__TESSERACT_DESIGN__.deliver({ t: 'host:language', epoch, language: lang }), 0);
  });
}

const DRAFT_KEYS = ['lan', 'ipv6', 'egress', 'dnsRules'];

// ------------------------------------------------------------------------------------ the items
const ENTRY = {
  'entry.overview': { scenario: 'overview_running', start: 'logs', go: 'overview' },
  'entry.tiers': { scenario: 'tiers_running', start: 'overview', go: 'profiles' },
  'entry.logs': { scenario: 'logs_i18n_running', start: 'overview', go: 'logs' },
  'entry.settings': { scenario: 'settings_i18n_running', start: 'overview', go: 'settings' },
};

export const ITEMS = [...Object.keys(ENTRY), 'logs4000.load', 'logs4000.filter', 'logs4000.search', 'logs4000.leave', 'lang.overview', 'lang.settings', 'lang.logs4000', 'hidden30'];
export const ONCE_PER_ROUND = new Set(['hidden30']);

export async function runItem(browser, dist, item) {
  if (ENTRY[item]) {
    const e = ENTRY[item];
    const { context, page, errors } = await open(browser, dist, scenario(e.scenario), e.start);
    try {
      const w = await window_(page, () => page.click(`.nav-item[data-nav="${e.go}"]`), 1800);
      const shown = await page.evaluate(id => { const p = document.querySelector(`.page[data-page="${id}"]`); return !!(p && !p.hidden && p.childElementCount); }, e.go);
      return { ...w, ok: w.longest <= LONG_MS && shown && !errors.length, shown, errors };
    } finally { await context.close(); }
  }
  if (item.startsWith('logs4000.')) {
    const sc = scenario('logs_stress_4000');
    const { context, page, errors } = await open(browser, dist, sc, 'overview');
    try {
      await page.evaluate(feedLogs, feedArgs(sc));
      await page.evaluate(quiet, 600);
      const load = await window_(page, () => page.click('.nav-item[data-nav="logs"]'), 2500);
      const rows = await page.evaluate(() => ({ events: document.querySelectorAll('#logs-events .logs-row').length, raw: document.querySelectorAll('#logs-rows .logs-rrow').length, total: (document.getElementById('logs-total') || {}).textContent || '' }));
      if (item === 'logs4000.load') return { ...load, ok: load.longest <= LONG_MS && !errors.length && rows.events > 0, rows, errors };
      if (item === 'logs4000.leave') {
        await page.evaluate(quiet, 1500);
        const w = await window_(page, () => page.click('.nav-item[data-nav="overview"]'), 1800);
        const shown = await page.evaluate(() => window.T.router.current === 'overview');
        return { ...w, ok: w.longest <= LONG_MS && shown && !errors.length, rows, shown, errors };
      }
      const steps = {};
      const act = async (name, fn, settle = 900) => { steps[name] = await window_(page, fn, settle); };
      if (item === 'logs4000.filter') {
        await act('events_issue', () => page.click('#logs-level [data-v="issue"]'));
        await act('events_all', () => page.click('#logs-level [data-v="all"]'));
        await act('view_raw', () => page.click('#logs-view [data-v="raw"]'));
        await act('raw_error', () => page.click('#logs-level [data-v="error"]'));
        await act('raw_all', () => page.click('#logs-level [data-v="all"]'));
        await act('view_events', () => page.click('#logs-view [data-v="events"]'));
      } else {
        const typeAndErase = async view => {
          if (view === 'raw') await page.click('#logs-view [data-v="raw"]');
          await page.evaluate(quiet, 600);
          await act(`${view}_type`, async () => { await page.click('#logs-search input'); await page.keyboard.type('example', { delay: 110 }); }, 900);
          await act(`${view}_erase`, async () => { for (let i = 0; i < 7; i++) { await page.keyboard.press('Backspace'); await sleep(110); } }, 900);
        };
        await typeAndErase('events');
        await typeAndErase('raw');
      }
      const longest = Math.max(0, ...Object.values(steps).map(s => s.longest));
      return { longest, ok: longest <= LONG_MS && !errors.length, steps: Object.fromEntries(Object.entries(steps).map(([k, v]) => [k, { longest: v.longest, lt: v.lt, loaf: v.loaf, trace_longest: v.trace_longest }])), rows, errors };
    } finally { await context.close(); }
  }
  if (item === 'lang.logs4000') return langLogs(browser, dist);
  if (item.startsWith('lang.')) {
    const onSettings = item === 'lang.settings';
    const sc = scenario(onSettings ? 'settings_i18n_running' : 'overview_running');
    const { context, page, errors } = await open(browser, dist, sc, onSettings ? 'settings' : 'overview');
    try {
      let draft = null;
      if (onSettings) {
        await page.waitForFunction(keys => keys.some(k => { const e = document.querySelector(`.switch[data-k="${k}"]`); return e && !e.inert && !e.closest('[inert]'); }), DRAFT_KEYS, { timeout: 15000 });
        const key = await page.evaluate(keys => keys.find(k => { const e = document.querySelector(`.switch[data-k="${k}"]`); return e && !e.inert && !e.closest('[inert]') && e.getAttribute('aria-disabled') !== 'true'; }), DRAFT_KEYS);
        await page.click(`.switch[data-k="${key}"]`);
        await page.waitForFunction(() => window.T.ui.saveBar.dirtyCount() > 0, null, { timeout: 5000 });
        await page.evaluate(quiet, 800);
        draft = { key, before: await page.evaluate(() => window.T.ui.saveBar.dirtyCount()) };
      }
      const switches = {};
      for (const lang of (process.env.PERF_LANGS || 'en_US,zh_CN').split(',')) {
        let timing = null;
        const w = await window_(page, async () => { timing = await page.evaluate(switchLanguage, [epochOf(sc), lang]); return timing; }, 1200);
        switches[`${Object.keys(switches).length}:${lang}`] = { longest: w.longest, lt: w.lt, loaf: w.loaf, trace_longest: w.trace_longest, ...timing };
      }
      if (draft) draft.after = await page.evaluate(() => window.T.ui.saveBar.dirtyCount());
      const longest = Math.max(0, ...Object.values(switches).map(s => s.longest));
      const frame = Math.max(0, ...Object.values(switches).map(s => s.t_frame));
      const ok = longest <= LONG_MS && frame < LANG_FRAME_MS && !errors.length && (!draft || (draft.after === draft.before && draft.after > 0));
      return { longest, frame_ms: frame, ok, switches, draft, errors };
    } finally { await context.close(); }
  }
  if (item === 'hidden30') return hidden30(browser, dist);
  throw new Error(`unknown item ${item}`);
}

// ------------------------------------------------------------------------------------ lang.logs4000
// (T-P4-09 fix 1, review M1) the language switch on 运行日志 holding the 4,000 lines of
// logs_stress_4000 (entered from the sidebar, settled; no live feed: the switch alone): every row's
// words change, so both views rebuild. switch -> first frame as lang.overview; the window takes in
// the shown view's sliced rebuild and the other view's idle one (settle 2500 ms)
async function langLogs(browser, dist) {
  // The scenario answers each logs.tail page once (transport-fake on_invoke): the history fetched
  // again in the new language (relabelTail, O-9) would fail and raise its warning banner, whose
  // entrance (FLIP + the scrollbar width read) lays out the whole document in that task. A backend
  // answers it: the pages are registered twice here, so the switch takes the path it takes in the
  // product (same titles: nothing relabelled). The failure path is noted in the report.
  const base = scenario('logs_stress_4000');
  const sc = { ...base, steps: [...base.steps, ...base.steps.filter(s => s.on_invoke && s.on_invoke.cmd === 'logs.tail')] };
  const { context, page, errors } = await open(browser, dist, sc, 'overview');
  try {
    await page.click('.nav-item[data-nav="logs"]');
    await page.waitForFunction(() => document.querySelectorAll('#logs-events .logs-row').length > 500, null, { timeout: 15000 });
    await page.evaluate(quiet, 2500);
    const count = () => page.evaluate(() => ({ events: document.querySelectorAll('#logs-events .logs-row').length, raw: document.querySelectorAll('#logs-rows .logs-rrow').length, lang: document.documentElement.lang,
      day: ((document.querySelector('#logs-events .logs-day') || {}).textContent || '').trim().slice(0, 40) }));
    const rows0 = await count();
    const switches = {};
    for (const lang of (process.env.PERF_LANGS || 'en_US,zh_CN').split(',')) {
      let timing = null;
      const w = await window_(page, async () => { timing = await page.evaluate(switchLanguage, [epochOf(sc), lang]); return timing; }, 2500);
      switches[`${Object.keys(switches).length}:${lang}`] = { longest: w.longest, lt: w.lt, loaf: w.loaf, trace_longest: w.trace_longest, ...timing, rows: await count() };
    }
    const rows1 = await count();
    const longest = Math.max(0, ...Object.values(switches).map(s => s.longest));
    const frame = Math.max(0, ...Object.values(switches).map(s => s.t_frame));
    // the rows stay (the same records, relabelled): the counts after the round trip are the ones before
    const kept = rows1.events === rows0.events && rows0.events > 500;
    return { longest, frame_ms: frame, ok: longest <= LONG_MS && frame < LANG_FRAME_MS && kept && !errors.length, switches, rows_before: rows0, rows_after: rows1, errors };
  } finally { await context.close(); }
}

// ------------------------------------------------------------------------------------ hidden 30 s
// (the logs_hidden_30s connect and seed, as cases/logs.mjs cuts it; the 30 s are played here in
// real time)
async function hidden30(browser, dist, seconds = 30) {
  const full = scenario('logs_hidden_30s');
  const steps = full.steps.filter(s => !(s.deliver && (s.deliver.t === 'host:visibility' || s.deliver.topic === 'logs.batch')) && !('advance_ms' in s));
  const sc = { ...full, name: 'perf_hidden30', steps };
  const { context, page, errors } = await open(browser, dist, sc, 'logs');
  try {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Performance.enable');
    const metrics = async () => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m => [m.name, m.value]));
    const E = epochOf(full);
    await page.evaluate(quiet, 800);
    const before = await page.evaluate(() => ({ rows: document.querySelectorAll('#logs-rows .logs-rrow, #logs-events .logs-row').length }));
    await page.evaluate(e => window.__TESSERACT_DESIGN__.deliver({ t: 'host:visibility', visible: false, epoch: e }), E);
    await page.evaluate(quiet, 500);
    const m0 = await metrics();
    // the writes the page makes while hidden, and the catch-up batches once shown, by region
    // (common.mjs installWrites: the lists, the footer, the sidebar badge)
    await page.evaluate(installWrites);
    await page.evaluate(feedLogs, [E, 15, 1, 1]);
    await sleep(seconds * 1000);
    await page.evaluate(() => { clearInterval(window.__feed); window.__feed = null; });
    const m1 = await metrics();
    await page.evaluate(() => { window.__writes.phase = 'shown'; window.__writes.t0 = performance.now(); });
    await page.evaluate(e => window.__TESSERACT_DESIGN__.deliver({ t: 'host:visibility', visible: true, epoch: e }), E);
    await page.evaluate(quiet, 1500);
    const end = await page.evaluate(writesEnd);
    const after = { ...end, rows: await page.evaluate(() => document.querySelectorAll('#logs-rows .logs-rrow, #logs-events .logs-row').length), view_rows: end.rows, total: await page.evaluate(() => (document.getElementById('logs-total') || {}).textContent || '') };
    const d = k => Math.round((m1[k] || 0) - (m0[k] || 0));
    const delta = { LayoutCount: d('LayoutCount'), RecalcStyleCount: d('RecalcStyleCount'), ScriptDuration_ms: Math.round(((m1.ScriptDuration || 0) - (m0.ScriptDuration || 0)) * 1000), TaskDuration_ms: Math.round(((m1.TaskDuration || 0) - (m0.TaskDuration || 0)) * 1000) };
    // caught up in one write: the first content batch in the view shown already held the rows the
    // view holds at the end (later batches: the other regions, see writes.regions)
    const first = after.writes.first[after.writes.cur];
    const oneWrite = !!first && first.rows === after.view_rows;
    const ok = delta.LayoutCount === 0 && delta.RecalcStyleCount === 0 && after.writes.hidden === 0 && after.rows > before.rows && oneWrite && !errors.length;
    return { longest: 0, ok, seconds, delta, before, after, one_write: oneWrite, errors };
  } finally { await context.close(); }
}

export function distOk(dir) { return existsSync(join(dir, 'index.html')); }

// run.mjs: the environment of one design run (its own build; the browser is closed while the CPU is
// sampled)
export async function setup({ log, dist: given }) {
  // (--dist: a design build made elsewhere, e.g. from the sources before this ticket's changes)
  const dist = given ? { dir: given, source_digest: JSON.parse(readFileSync(join(given, 'build-manifest.json'), 'utf8')).source_digest || null, remove: () => {} } : buildDesign();
  log(`[design] build ${dist.dir} (source ${String(dist.source_digest).slice(0, 12)})`);
  let browser = await launchChrome();
  const facts = { chrome: browser.version(), build: { source_digest: dist.source_digest, flags: 'design --pseudo, no --i18n-trace' }, viewport: '1280x800', motion: 'no-preference', locale: 'zh_CN' };
  return {
    facts,
    runItem: item => runItem(browser, dist.dir, item),
    pause: async () => { await browser.close(); browser = null; },
    resume: async () => { browser = await launchChrome(); },
    close: async () => { if (browser) await browser.close(); dist.remove(); return 'browser closed, build removed'; },
  };
}
