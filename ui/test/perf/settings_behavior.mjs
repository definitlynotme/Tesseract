// T-P4-09: 配置's first entry lays out only the sections of the first window and shows the others in
// idle periods (pages/settings.js deferSections / showUpTo). Ticket requirement 3: the save bar, the
// drafts and the jumps of the section index behave as before. Checked in real time (no fixture
// clock: the idle periods are the browser's), on the design target in fixture mode:
//   1  the first entry hides the sections below the window and every one is shown within 2 s
//   2  a jump from the index right after the click that entered the page (before the idle periods
//      have run): the target is shown, glided to its place under the top (24 px) and focused (the
//      section the index marks afterwards is recorded: the one under its midline, as before)
//   3  a jump into a section from a link (T.go('settings', {focus})) on a first entry: the target is
//      shown and in view
//   4  a draft in a section shown late (配置 · 网络): the save bar counts it, 撤销 takes it back
//   5  a language switch while scrolled to the bottom with a draft: the draft survives, the scroll
//      position comes back, every section is laid out
//   6  Ctrl S saves a draft made right after the entry
//   node ui/test/perf/settings_behavior.mjs [--dist <design build>]
// Exit 0 when every check passed; the result goes to evidence/T-P4-09/settings-behavior-<stamp>.json.
import { readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildDesign, launchChrome } from './design.mjs';
import { EVIDENCE, REPO, UI, quiet, sleep, stamp, writeJson } from './common.mjs';

const args = process.argv.slice(2);
const given = args[0] === '--dist' ? args[1] : null;
const SC = JSON.parse(readFileSync(join(UI, 'test', 'fixtures', 'scenarios', 'settings_i18n_running.json'), 'utf8'));
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok: !!ok, detail: detail ?? null }); console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${!ok && detail ? ' ' + JSON.stringify(detail).slice(0, 400) : ''}`); };

async function open(browser, dist, start, viewport = { width: 1280, height: 800 }) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, reducedMotion: 'reduce', locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
  await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, JSON.stringify(SC));
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e && e.message || e).slice(0, 300)));
  await page.goto(pathToFileURL(join(dist, 'index.html')).href + '#' + start, { waitUntil: 'load' });
  await page.waitForFunction(() => window.T && window.T.rt && window.T.rt.state === 'live');
  await page.evaluate(quiet, 800);
  return { context, page, errors };
}
const secs = page => page.evaluate(() => [...document.querySelectorAll('.page-settings .fsec')].map(s => ({ id: s.id, hidden: s.hidden })));
const editable = page => page.waitForFunction(() => { const s = document.querySelector('.switch[data-k="lan"]'); return s && !s.inert && !s.closest('[inert]') && s.getAttribute('aria-disabled') !== 'true'; }, null, { timeout: 10000 });

const dist = given ? { dir: given, remove() {} } : buildDesign();
const browser = await launchChrome();
try {
  // 1, 2: the entry, then a jump from the index at once
  for (const vp of [{ width: 1280, height: 800 }, { width: 1060, height: 700 }]) {
    for (const target of ['set-alerts', 'set-app', 'set-nodes']) {
      const { context, page, errors } = await open(browser, dist.dir, 'overview', vp);
      try {
        await page.click('.nav-item[data-nav="settings"]');
        const first = await secs(page);
        await page.click(`#set-toc .toc-a[data-target="${target}"]`);
        await sleep(1200);
        const r = await page.evaluate(id => {
          const h = document.getElementById('host'), s = document.getElementById(id);
          const cur = document.querySelector('#set-toc .toc-a[aria-current="true"]');
          return { hidden: s.hidden, top: Math.round(s.getBoundingClientRect().top - h.getBoundingClientRect().top), marked: cur ? cur.dataset.target : null, focused: document.activeElement === s };
        }, target);
        const tag = `${vp.width}x${vp.height} → ${target}`;
        if (target === 'set-alerts') check(`${vp.width}x${vp.height}: the first entry hides only sections below the window (${first.filter(x => x.hidden).length} of ${first.length} hidden, the first shown)`, !first[0].hidden && first.some(x => x.hidden), first);
        // (a section near the end may not reach the top: the host cannot scroll past its bottom)
        const atEnd = await page.evaluate(() => { const h = document.getElementById('host'); return h.scrollTop + h.clientHeight >= h.scrollHeight - 2; });
        // (the index marks the section under its spy line, the window's midline, once the scroll has
        // settled: for a short section that is the next one; the same with every section laid out
        // from the start, checked on the build before T-P4-09: --dist. Recorded, not judged)
        check(`index jump right after the entry (${tag}): shown, at its place (24 px or the end of the page), focused [index marks ${r.marked}]`, !r.hidden && (Math.abs(r.top - 24) <= 3 || atEnd) && r.focused, { ...r, atEnd });
        await page.evaluate(quiet, 1500);
        const later = await secs(page);
        check(`${tag}: every section is shown after the idle periods`, later.every(x => !x.hidden), later.filter(x => x.hidden));
        if (errors.length) check(`${tag}: no page error`, false, errors);
      } finally { await context.close(); }
    }
  }
  // 3: a link into a section on a first entry
  {
    const { context, page, errors } = await open(browser, dist.dir, 'overview');
    try {
      await page.evaluate(() => window.T.go('settings', { focus: 'update' }));
      await sleep(1500);
      const r = await page.evaluate(() => { const h = document.getElementById('host').getBoundingClientRect(), e = document.getElementById('set-upd-row'); const b = e.getBoundingClientRect(); return { hidden: !!e.closest('.fsec').hidden, inView: b.top >= h.top - 1 && b.top < h.bottom }; });
      check('T.go(settings, {focus:"update"}) on a first entry: the row is shown and in view', !r.hidden && r.inView, r);
      check('focus link: no page error', !errors.length, errors);
    } finally { await context.close(); }
  }
  // 4, 5: a draft in a section shown late, the save bar, 撤销, a language switch at the bottom
  {
    const { context, page, errors } = await open(browser, dist.dir, 'overview');
    try {
      await page.click('.nav-item[data-nav="settings"]');
      await editable(page);
      await page.evaluate(quiet, 1500);
      const key = await page.evaluate(() => { const s = document.querySelector('#set-conn .switch[data-k]'); return s ? s.dataset.k : null; });
      await page.click(`.switch[data-k="${key}"]`);
      await page.evaluate(quiet, 600);
      const n1 = await page.evaluate(() => window.T.ui.saveBar.dirtyCount());
      check(`a draft in 网络 (${key}): the save bar counts it`, n1 === 1, { n1 });
      await page.click('.savebar .sb-undo');
      await page.evaluate(quiet, 600);
      check('撤销 takes it back', await page.evaluate(() => window.T.ui.saveBar.dirtyCount()) === 0);
      await page.click(`.switch[data-k="${key}"]`);
      await page.evaluate(() => { const h = document.getElementById('host'); h.scrollTop = h.scrollHeight; });
      await page.evaluate(quiet, 600);
      const before = await page.evaluate(() => document.getElementById('host').scrollTop);
      await page.evaluate(() => window.__TESSERACT_DESIGN__.deliver({ t: 'host:language', epoch: 'rt-fixture-e1', language: 'en_US' }));
      await page.waitForFunction(() => window.T.i18n.lang === 'en_US');
      await page.evaluate(quiet, 800);
      const after = await page.evaluate(() => ({ scroll: document.getElementById('host').scrollTop, dirty: window.T.ui.saveBar.dirtyCount(), hidden: [...document.querySelectorAll('.page-settings .fsec')].filter(s => s.hidden).map(s => s.id) }));
      check('language switch at the bottom with a draft: the draft survives', after.dirty === 1, after);
      check('language switch at the bottom: the scroll position comes back', Math.abs(after.scroll - before) <= 2, { before, after: after.scroll });
      check('language switch: every section is laid out again', !after.hidden.length, after.hidden);
      check('draft / language: no page error', !errors.length, errors);
    } finally { await context.close(); }
  }
  // 6: Ctrl S right after the entry
  {
    const { context, page, errors } = await open(browser, dist.dir, 'overview');
    try {
      await page.click('.nav-item[data-nav="settings"]');
      await editable(page);
      await page.click('.switch[data-k="lan"]');
      await page.keyboard.press('Control+S');
      await page.waitForFunction(() => window.T.ui.saveBar.dirtyCount() === 0, null, { timeout: 8000 }).catch(() => {});
      const calls = await page.evaluate(() => (window.T.rt.__fake.invocations || []).filter(i => i.cmd === 'settings.apply').length);
      check('Ctrl S right after the entry: settings.apply is sent', calls >= 1, { calls });
      check('Ctrl S: no page error', !errors.length, errors);
    } finally { await context.close(); }
  }
} finally {
  await browser.close();
  dist.remove();
}
const failed = results.filter(r => !r.ok);
const file = join(EVIDENCE, `settings-behavior-${stamp()}.json`);
writeJson(file, { ticket: 'T-P4-09', checks: results.length, failed: failed.length, results });
console.log(`settings behaviour: ${failed.length ? 'FAILED' : 'OK'} — ${results.length - failed.length}/${results.length} → ${relative(REPO, file).split('\\').join('/')}`);
process.exitCode = failed.length ? 1 : 0;
