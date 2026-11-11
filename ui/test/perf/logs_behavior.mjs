// T-P4-09 fix 2 (review N1): 运行日志's views after a rebuild planned ahead (pages/logs.js ahead()).
// The planning build must not take the place of the build the following render makes: that render
// would find nothing to patch and the list would keep what it held. Checked on the design target in
// fixture mode, real time, with the 15 lines of logs_i18n_running (the rebuild stays synchronous:
// fewer than PRE_MIN rows) and the 4,000 lines of logs_stress_4000 (the rebuild goes in slices):
//   1  a level filter set on 关键事件, then 原始日志: the raw list holds the rows the same filter set
//      on 原始日志 itself gives (fewer than unfiltered)
//   2  a language switch on 总览 after a visit, then back: both views' day labels in the new language
//   3  a language switch on 运行日志, then 原始日志: its day labels in the new language
//   4  records arriving while on 总览: they are in the list on return
// Day labels are written by Intl (dayLabel): en_US gives "September 21 · Monday" (no CJK); the count
// beside them comes from the catalog (the P4 en_US catalog falls back to zh_CN) and is not judged.
//   node ui/test/perf/logs_behavior.mjs [--dist <design build>]
// Exit 0 when every check passed; the result goes to evidence/T-P4-09/logs-behavior-<stamp>.json.
import { readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildDesign, launchChrome } from './design.mjs';
import { EVIDENCE, REPO, UI, quiet, stamp, writeJson } from './common.mjs';

const args = process.argv.slice(2);
const given = args[0] === '--dist' ? args[1] : null;
const scenario = name => {
  const base = JSON.parse(readFileSync(join(UI, 'test', 'fixtures', 'scenarios', `${name}.json`), 'utf8'));
  // each logs.tail page twice: the history fetched again in the new language (relabelTail, O-9) is
  // answered as a backend answers it (see design.mjs langLogs)
  return { ...base, steps: [...base.steps, ...base.steps.filter(s => s.on_invoke && s.on_invoke.cmd === 'logs.tail')] };
};
const epochOf = sc => { const s = sc.steps.find(x => x.deliver && x.deliver.epoch); return s ? s.deliver.epoch : 'rt-fixture-e1'; };
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok: !!ok, detail: detail ?? null }); console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${!ok && detail ? ' ' + JSON.stringify(detail).slice(0, 400) : ''}`); };

const CASES = [
  { name: 'logs_i18n_running', label: '15 lines', min: 1 },
  { name: 'logs_stress_4000', label: '4,000 lines', min: 500 },
];

async function open(browser, dist, sc, min) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light' });
  await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, JSON.stringify(sc));
  await context.addInitScript(() => { const w = console.warn; console.warn = function (...a) { if (typeof a[0] === 'string' && a[0].startsWith('[i18n]')) return undefined; return w.apply(this, a); }; });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e && e.message || e).slice(0, 300)));
  await page.goto(pathToFileURL(join(dist, 'index.html')).href + '#overview', { waitUntil: 'load' });
  await page.waitForFunction(() => window.T && window.T.rt && window.T.rt.state === 'live' && window.T.router.current === 'overview', null, { timeout: 20000 });
  await page.evaluate(quiet, 800);
  await page.click('.nav-item[data-nav="logs"]');
  await page.waitForFunction(n => document.querySelectorAll('#logs-events .logs-row').length >= n, min, { timeout: 20000 });
  await page.evaluate(quiet, 2000);
  return { context, page, errors };
}
// the rows and day labels shown in each view (a row inside a closed fold or a hidden view is not shown)
const state = page => page.evaluate(() => {
  const shown = sel => [...document.querySelectorAll(sel)].filter(e => !e.hidden && !e.closest('[hidden]'));
  const label = sel => [...document.querySelectorAll(sel)].map(e => ((e.firstElementChild || e).textContent || '').trim());
  return { lang: document.documentElement.lang, events: shown('#logs-events .logs-row').length, raw: shown('#logs-rows .logs-rrow').length,
    evDays: label('#logs-events .logs-day'), rawDays: label('#logs-rows .logs-day') };
});
const CJK = /[\u3400-\u9fff]/;
const latin = days => days.length > 0 && days.every(d => d && !CJK.test(d));
const view = async (page, v) => { await page.click(`#logs-view [data-v="${v}"]`); await page.evaluate(quiet, 1500); };
const level = async (page, v) => { await page.click(`#logs-level [data-v="${v}"]`); await page.evaluate(quiet, 2000); };
const nav = async (page, id, ms = 1000) => { await page.click(`.nav-item[data-nav="${id}"]`); await page.evaluate(quiet, ms); };
const language = async (page, sc, lang) => {
  await page.evaluate(([e, l]) => setTimeout(() => window.__TESSERACT_DESIGN__.deliver({ t: 'host:language', epoch: e, language: l }), 0), [epochOf(sc), lang]);
  await page.waitForFunction(l => window.T.i18n.lang === l, lang, { timeout: 5000 });
  await page.evaluate(quiet, 2500);
};

const dist = given ? { dir: given, remove() {} } : buildDesign();
const browser = await launchChrome();
try {
  for (const c of CASES) {
    const sc = scenario(c.name), tag = `${c.label}`;
    // 1: the filter set on 关键事件, then 原始日志 · against the same filter set on 原始日志
    let direct = null;
    {
      const { context, page, errors } = await open(browser, dist.dir, sc, c.min);
      try {
        await view(page, 'raw');
        const all = await state(page);
        await level(page, 'error');
        direct = { all: all.raw, error: (await state(page)).raw };
        if (errors.length) check(`${tag}: filter on 原始日志: no page error`, false, errors);
      } finally { await context.close(); }
    }
    {
      const { context, page, errors } = await open(browser, dist.dir, sc, c.min);
      try {
        await level(page, 'error');
        await view(page, 'raw');
        const s = await state(page);
        check(`${tag}: 错误 set on 关键事件, then 原始日志: ${s.raw} rows, as 错误 set on 原始日志 (${direct.error} of ${direct.all})`, s.raw === direct.error && direct.error < direct.all, { raw: s.raw, direct });
        check(`${tag}: filter, then view: no page error`, !errors.length, errors);
      } finally { await context.close(); }
    }
    // 2: the language switched on 总览 after a visit (both views built), then back
    {
      const { context, page, errors } = await open(browser, dist.dir, sc, c.min);
      try {
        await view(page, 'raw'); await view(page, 'events');
        const before = await state(page);
        await nav(page, 'overview');
        await language(page, sc, 'en_US');
        await nav(page, 'logs', 2500);
        const ev = await state(page);
        await view(page, 'raw');
        const raw = await state(page);
        check(`${tag}: en_US on 总览, back: 关键事件 day labels in en_US (${ev.evDays[0] || '-'})`, /^en\b/.test(ev.lang) && latin(ev.evDays) && ev.events === before.events, { before: before.evDays.slice(0, 3), after: ev.evDays.slice(0, 3), lang: ev.lang, rows: [before.events, ev.events] });
        check(`${tag}: en_US on 总览, back: 原始日志 day labels in en_US (${raw.rawDays[0] || '-'})`, latin(raw.rawDays), raw.rawDays.slice(0, 3));
        check(`${tag}: language away: no page error`, !errors.length, errors);
      } finally { await context.close(); }
    }
    // 3: the language switched on 运行日志, then the other view
    {
      const { context, page, errors } = await open(browser, dist.dir, sc, c.min);
      try {
        await view(page, 'raw'); await view(page, 'events');
        await language(page, sc, 'en_US');
        const ev = await state(page);
        await view(page, 'raw');
        const raw = await state(page);
        check(`${tag}: en_US on 运行日志: 关键事件 day labels in en_US (${ev.evDays[0] || '-'})`, latin(ev.evDays), ev.evDays.slice(0, 3));
        check(`${tag}: en_US on 运行日志, then 原始日志: its day labels in en_US (${raw.rawDays[0] || '-'})`, latin(raw.rawDays), raw.rawDays.slice(0, 3));
        await language(page, sc, 'zh_CN');
        const raw2 = await state(page);
        await view(page, 'events');
        const ev2 = await state(page);
        check(`${tag}: back to zh_CN on 原始日志, then 关键事件: both views' day labels in zh_CN`, raw2.rawDays.length && raw2.rawDays.every(d => CJK.test(d)) && ev2.evDays.length && ev2.evDays.every(d => CJK.test(d)), { raw: raw2.rawDays.slice(0, 3), events: ev2.evDays.slice(0, 3) });
        check(`${tag}: language on the page: no page error`, !errors.length, errors);
      } finally { await context.close(); }
    }
    // 4: records arriving while away
    {
      const { context, page, errors } = await open(browser, dist.dir, sc, c.min);
      try {
        const n0 = (await state(page)).events;
        await nav(page, 'overview');
        const a = (() => { const b = sc.steps.filter(x => x.deliver && x.deliver.topic === 'logs.batch').map(x => x.deliver); const last = b[b.length - 1]; const ls = last ? last.data.records[last.data.records.length - 1].seq : 0; return [epochOf(sc), ls + 1, (last ? last.rev : 0) + 1, (last && last.seq ? last.seq : 0) + 1]; })();
        await page.evaluate(([epoch, from, rev, seq]) => { const now = Date.now(), records = []; for (let j = 0; j < 30; j++) records.push({ seq: from + j, ts_ms: now, rx_ms: now + 9, source: 'TESSERACT', level: 'info', category: 'runtime', key: 'perf.key_line', title: 'Probe key line ' + j, detail: 'probe', raw: '', is_key_event: true, run_id: 'run-42' }); window.__TESSERACT_DESIGN__.deliver({ t: 'evt', epoch, produced_at_ms: now, sent_at_ms: now + 1, topic: 'logs.batch', rev, kind: 'edge', seq, data: { from_seq: from, records, dropped: 0, dropped_total: 0, gap: false } }); }, a);
        await page.evaluate(quiet, 1500);
        await nav(page, 'logs', 2500);
        const probe = await page.evaluate(() => [...document.querySelectorAll('#logs-events .logs-row')].filter(e => /Probe key line/.test(e.textContent)).length);
        check(`${tag}: 30 records arriving on 总览 are in 关键事件 on return`, probe === 30, { probe, before: n0 });
        check(`${tag}: records away: no page error`, !errors.length, errors);
      } finally { await context.close(); }
    }
  }
} finally {
  await browser.close();
  dist.remove();
}
const failed = results.filter(r => !r.ok);
const file = join(EVIDENCE, `logs-behavior-${stamp()}.json`);
writeJson(file, { ticket: 'T-P4-09', checks: results.length, failed: failed.length, results });
console.log(`logs behaviour: ${failed.length ? 'FAILED' : 'OK'} — ${results.length - failed.length}/${results.length} → ${relative(REPO, file).split('\\').join('/')}`);
process.exitCode = failed.length ? 1 : 0;
