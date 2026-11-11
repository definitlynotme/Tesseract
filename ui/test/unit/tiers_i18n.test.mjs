// T-P4-04c: message extraction of the tiers page (pages/tiers.js, catalog tiers.*); updated by
// T-P4-05b (the page on the runtime's data: the prototype regression baseline is retired, see
// render/cases/tiers_i18n.mjs).
//
// Source checks (no browser): no @i18n-pending, no CJK literal, names sort with the language's
// collator (no localeCompare 'zh-CN'), no ${E(x.name)}-style splice of a name / region / host / url /
// protocol into an HTML string (the §14.1 sentences of 映射_tiers; see the first test), every id the page names exists in the zh_CN catalog and every tiers.* id is
// used, each with _desc and without _max (critique F8); the diagnosis reasons cover the
// nodes.diagnose / Node.reason_code set of tools/backend_keys.py (tiers.reason.<code> and .fix),
// and the latency ceiling in the advice is a parameter.
//
// Browser checks (local Chrome through playwright-core, the design target built in memory with the
// pseudo language, file://, no network, deterministic fixture clock; skipped without Chrome):
//  - sim mode (T-P4-05b in place of the T-P4-04c regression against the prototype's words, which
//    the removal of the synthetic data retires): the STATES walk of render/cases/tiers_i18n.mjs
//    (four tiers, a subscription row opened, the side panel with and without a diagnosis, the
//    import sheet's forms, search without a match, stopped) renders zh_CN words without errors;
//  - language switch: switching to the pseudo language leaves no CJK outside translate="no",
//    only catalog attributes and no leak of the matrix's check in the page or its overlays;
//    switching back to zh_CN gives the same words and attributes again (T.ti relabel + relocalize);
//  - fixture mode (critique E2): the ASCII scenarios of T-P4-05b in pseudo, every FIXTURE_STATES
//    walk on its FIXTURE_SCENARIOS (empty tier, Bastion on and off, drill-ins, panels, the three
//    import forms); the import sheet with a preview (tiers_import) across a language switch.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { buildTarget, writeOutput } from '../../build.mjs';
import { isPending, scanCjkJs } from '../../tools/scan.mjs';
import { cellChecksInPage, LEAK_WHITELIST } from '../render/checks.mjs';
import {
  DEMO_BADGE, FIXTURE_SCENARIOS, FIXTURE_STATES, STATES, runState, tiersWordsInPage, wordProblems,
} from '../render/cases/tiers_i18n.mjs';

const UI = new URL('../../', import.meta.url);
const REPO = fileURLToPath(new URL('../', UI));
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const skip = !existsSync(CHROME) && 'local Chrome not found';
const SRC = readFileSync(new URL('src/pages/tiers.js', UI), 'utf8');
// the page's derivations (T-P4-05b, criticism C10): the reason code list lives there
const PROJECT = readFileSync(new URL('src/rt/project_tiers.js', UI), 'utf8');
const catalog = ns => JSON.parse(readFileSync(new URL(`src/i18n/locales/zh_CN/${ns}.json`, UI), 'utf8'));
const TIERS = ['vanguard', 'sentinel', 'bastion', 'gateway'];

/* ---------------------------------------------------------------- source */

test('tiers.js: no @i18n-pending, no CJK literal, the language\'s collator, no name into HTML', () => {
  assert.equal(isPending(SRC, 'js'), false, 'the first line still marks the file @i18n-pending');
  assert.deepEqual(scanCjkJs(SRC, 'src/pages/tiers.js'), []);
  assert.doesNotMatch(SRC, /localeCompare\([^)]*zh-CN/, 'names sort with Intl.Collator(T.i18n.lang), not zh-CN');
  assert.match(SRC, /F\.compare\(a\.name, b\.name\)/);
  // 映射_tiers §14.1: the sentences that name a node, subscription, pool place, protocol, region,
  // address or link are built as text (DOM text nodes / T.tx slots). What this checks is narrower:
  // no `${E(<identifier chain ending in a data field>)}` in the source. It does not see an escaped
  // data value that reaches HTML another way — a form field's value attribute (fieldHTML({ value:
  // n.name / n.host / g.name / g.url }) → value="${E(o.value)}") or the masked host
  // (${E(V.reveal ? v : maskHost(v))}); those are escaped, outside the §14.1 sentences, and left to
  // 05b (build the input, then set input.value / textContent). The tier code names (D.tier(x).name:
  // Vanguard … Gateway) are constants of the page, not data.
  const sinks = [...SRC.matchAll(/\$\{E\(([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\)/g)].map(m => m[1]);
  const named = sinks.filter(s => /\.(name|region|host|url|raw|p|proto|protocols|src)$/.test(s) && s !== 'd.name');
  assert.deepEqual(named, [], 'data escaped into HTML');
  // T-P4-05b: markup is built with T.html (escaping by the tag) and DOM nodes; no ${E(...)} splices
  assert.doesNotMatch(SRC, /\$\{E\(/, 'no escaped splice into an HTML string');
  assert.match(SRC, /<span class="tiers-nm" translate="no">\$\{d\.name\}<\/span>/, 'd.name is the tier code name in the switcher (T.html)');
});

test('every message id the page names exists; every tiers.* id is used; _desc, no _max', () => {
  const cats = {};
  const has = id => { const ns = id.split('.')[0]; cats[ns] = cats[ns] || catalog(ns); return Object.prototype.hasOwnProperty.call(cats[ns], id); };
  const literal = [...SRC.matchAll(/['"`(:]((?:tiers|tier|common|core|fmt|shell|mode)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)(?![\w.]*\$\{)(?!\w|\.<)/g)].map(m => m[1]).filter(id => id !== 'core.js');
  // ids built at run time (template literals): each family and its keys (T-P4-05b: the protocol
  // fields come from import.specs' catalog.* ids, T-P4-04e, so the tiers.pf.* family is gone)
  // (F-01 L2: the tier descriptions, the import sheet's subtitles and the context line's sentences are
  // Bastion's two literal ids now; the empty tiers have no body)
  // (F-03: the tier role words, tier.<x>.role, are gone; the switcher names a tier by its code name)
  const built = [
    ...TIERS.map(x => `tiers.search.placeholder.${x}`),
    ...['30m', '6h', '12h', '1d', 'manual'].map(k => `tiers.sub.auto.${k}`),
    ...['vanguard', 'sentinel', 'gateway'].map(k => `tiers.empty.${k}.title`),
    ...['vless_link', 'hysteria2_link', 'naive_link', 'uri'].map(k => `tiers.edit.link.${k}`),
  ];
  assert.doesNotMatch(SRC, /`tier\.\$\{x\}\.role`/, 'F-03: no role word');
  for (const re of [/`tiers\.search\.placeholder\.\$\{V\.tier\}`/,
    /`tiers\.sub\.auto\.\$\{/, /`tiers\.empty\.\$\{k\}\.title`/, /`tiers\.edit\.link\.\$\{fld\}`/,
    /reasonText\('tiers\.reason', P\.reasonKey\(r\)\)/, /`tiers\.reason\.\$\{P\.reasonKey\(r\)\}\.fix`/]) assert.match(SRC, re);
  // not message ids: topic names (DS.summary reads 'tiers.summary') and the reason namespace
  // handed to T.rt.reasonText('tiers.reason', code)
  const topics = new Set(Object.keys(JSON.parse(readFileSync(new URL('src/rt/spec.gen.json', UI), 'utf8')).topics));
  const used = new Set([...literal.filter(id => !topics.has(id) && id !== 'tiers.reason'), ...built]);
  const missing = [...used].filter(id => !has(id));
  assert.deepEqual(missing, [], 'ids not in the zh_CN catalog');
  // tiers.reason.* is read through P.reasonKey() (next test)
  const unused = Object.keys(catalog('tiers')).filter(id => !used.has(id) && !id.startsWith('tiers.reason.'));
  assert.deepEqual(unused, [], 'tiers.* ids the page never uses');
  for (const [id, e] of Object.entries(catalog('tiers'))) {
    assert.ok(typeof e.m === 'string' && e.m, `${id}: m`);
    assert.ok(typeof e._desc === 'string' && e._desc.trim(), `${id}: _desc`);
    assert.equal(e._max, undefined, `${id}: no _max (critique F8)`);
  }
});

test('diagnosis reasons: tiers.reason.<code> and .fix for the backend reason_code set; the ceiling is a parameter', () => {
  const r = spawnSync(join(REPO, '.venv', 'Scripts', 'python.exe'), ['-B', join(REPO, 'ui', 'tools', 'backend_keys.py'), '--repo', REPO],
    { cwd: REPO, encoding: 'utf8', maxBuffer: 64 << 20, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' } });
  assert.equal(r.status, 0, `backend_keys.py: ${(r.stderr || '').slice(-400)}`);
  const backend = JSON.parse(r.stdout).reasons['tiers.reason'];
  assert.ok(backend.length >= 15, 'the nodes.diagnose reason codes were read');
  const codes = JSON.parse(PROJECT.match(/export const REASON_CODES = Object\.freeze\((\[[^\]]*\])\)/)[1].replace(/'/g, '"'));
  assert.deepEqual(backend.filter(c => !codes.includes(c)), [], 'backend reason codes the page does not name');
  // the prototype's 配置异常 is the one page-side code; nothing else beyond the backend's set
  assert.deepEqual(codes.filter(c => !backend.includes(c)), ['configuration_invalid']);
  const cat = catalog('tiers');
  for (const c of codes) {
    assert.ok(cat[`tiers.reason.${c}`], `tiers.reason.${c}`);
    assert.ok(cat[`tiers.reason.${c}.fix`], `tiers.reason.${c}.fix`);
  }
  const reasonIds = Object.keys(cat).filter(id => id.startsWith('tiers.reason.'));
  assert.equal(reasonIds.length, codes.length * 2, 'only word and fix per code');
  const fix = cat['tiers.reason.latency_limit_exceeded.fix'].m;
  assert.match(fix, /\{max_ms, number, integer\}/);
  assert.doesNotMatch(fix, /800/);
  assert.match(SRC, /max_ms: ceil\(\)/);
});

/* --------------------------------------------------------------- browser */

let dir, browser;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-ti04c-'));
  const { files } = await buildTarget({ target: 'design', pseudo: true });
  writeOutput(dir, files);
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu', '--font-render-hinting=none', '--host-resolver-rules=MAP * ~NOTFOUND'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

// one page per state: 1280x800, reduced motion, zh-CN, the fixture clock, on #profiles; sim mode
// unless a scenario is given (fixture mode)
async function open({ scenario = null, lang = null } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, reducedMotion: 'reduce', locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light' });
  await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
  await context.addInitScript({ path: fileURLToPath(new URL('test/render/fixture_clock.js', UI)) });
  if (scenario) await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, readFileSync(new URL(`test/fixtures/scenarios/${scenario}.json`, UI), 'utf8'));
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message)));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await page.goto(pathToFileURL(join(dir, 'index.html')).href + (lang ? `?lang=${lang}` : '') + '#profiles', { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(ms => window.__fixtureClock.advance(ms), 3000);
  await page.waitForTimeout(100);
  await page.evaluate(() => window.__fixtureClock.advance(0));
  return { context, page, errors: () => errors.concat([]) };
}

// the words and attributes of the page and its overlays, in the shape of BEFORE
async function capture(page) {
  const r = await page.evaluate(tiersWordsInPage);
  return { snap: { pages: r.words.pages.join(' '), layer: r.words.layer.join(' '), attrs: r.attrs.pages.concat(r.attrs.layer) }, report: r };
}
// the first difference of two word strings, for the message
function firstDiff(a, b) {
  let i = 0; while (i < a.length && a[i] === b[i]) i++;
  return `at ${i}:\n  want …${a.slice(Math.max(0, i - 40), i + 80)}\n  got  …${b.slice(Math.max(0, i - 40), i + 80)}`;
}
function sameSnap(want, got, what) {
  assert.ok(want.pages === got.pages, `${what}: page words differ ${firstDiff(want.pages, got.pages)}`);
  assert.ok(want.layer === got.layer, `${what}: overlay words differ ${firstDiff(want.layer, got.layer)}`);
  assert.deepEqual(got.attrs, want.attrs, `${what}: attributes`);
}
// the matrix's own leak check (letters outside ⟦ ⟧, CSS content, attributes): the page and #layer
const leaksOf = async page => (await page.evaluate(cellChecksInPage, { pseudo: true, whitelist: [...LEAK_WHITELIST, DEMO_BADGE] })).leaks
  .filter(l => l.region === 'pages' || l.region === 'layer').map(l => `${l.region} ${l.selector}: ${l.text || l.msg || l.value || ''}`);

test('sim mode, zh_CN: every state renders without errors (T-P4-05b; the prototype baseline is retired)', { skip, timeout: 300000 }, async () => {
  for (const [state, steps] of Object.entries(STATES)) {
    const { context, page, errors } = await open();
    try {
      await runState(page, steps);
      const { snap, report } = await capture(page);
      assert.ok(snap.pages.split(' ').length > 10, `${state}: the page rendered`);
      assert.deepEqual(wordProblems(report, 'zh_CN'), [], `${state}: words`);
      assert.deepEqual(errors(), [], state);
      assert.deepEqual(await page.evaluate(() => window.__fixtureClock.errors.slice()), [], `${state}: timers`);
    } finally { await context.close(); }
  }
});

test('language switch: pseudo shows catalog words only; back to zh_CN is the same page', { skip, timeout: 300000 }, async () => {
  // The language changes on 配置 (P4 architecture §3.7), so the page itself and its modeless side
  // panel are what can be on screen at a switch. A shell host:language push can also arrive while
  // the import sheet or an edit is open: the test after this one. The import forms' initial states
  // are checked in pseudo from the start (fixture test). The confirmations (T.confirm) are not
  // walked by any test: their title and body name a node in core-built plain text (no
  // translate="no"), a core change handed on in the T-P4-04c report.
  const SWITCH = ['vanguard', 'sentinel', 'sentinel_drill', 'vanguard_diag', 'gateway_panel', 'sentinel_panel', 'bastion_off', 'search_empty', 'stopped'];
  for (const state of SWITCH) {
    const { context, page, errors } = await open();
    try {
      const settle = async () => { await page.evaluate(() => window.__fixtureClock.advance(0)); await page.waitForTimeout(80); await page.evaluate(() => window.__fixtureClock.advance(0)); };
      // past the transient faces (a button's result hold, T.ui.busy): a rebuild may end them early
      await runState(page, [...STATES[state], { ms: 2500 }]);
      const before0 = (await capture(page)).snap;
      await page.evaluate(() => T.i18n.set('pseudo'));
      await settle();
      assert.equal(await page.evaluate(() => T.i18n.lang), 'pseudo');
      const { report } = await capture(page);
      assert.deepEqual(wordProblems(report, 'pseudo'), [], `${state}: words in pseudo`);
      assert.deepEqual(await leaksOf(page), [], `${state}: leaks (pseudo)`);
      assert.ok(report.catalogAttrs.length > 5, `${state}: attributes were read`);
      await page.evaluate(() => T.i18n.set('zh_CN'));
      await settle();
      // a rebuilt line shows the time of the rebuild (「17 秒前」 painted at the last tick can read
      // 「18 秒前」 when redone between ticks): seconds-ago numbers are not compared
      const ago = s => ({ ...s, pages: s.pages.replace(/\d+ 秒前/g, '# 秒前'), layer: s.layer.replace(/\d+ 秒前/g, '# 秒前') });
      sameSnap(ago(before0), ago((await capture(page)).snap), `${state}: zh_CN → pseudo → zh_CN`);
      assert.deepEqual(errors(), [], state);
    } finally { await context.close(); }
  }
});

test('language switch with the import sheet (pasted) or an edit open: their dynamic lines follow', { skip, timeout: 300000 }, async () => {
  // T-P4-05b: the paste is recognised by import.preview (fixture tiers_import: three nodes in two
  // physical-node groups, G38; one group named by the sidecar: data under translate="no")
  const PASTE = ['hysteria2://a@hk.example.net:443#a', 'vless://b@hk.example.net:8443#b', 'trojan://c@jp.example.net:443#c'].join('\n');
  const settle = async page => { await page.evaluate(() => window.__fixtureClock.advance(0)); await page.waitForTimeout(80); await page.evaluate(() => window.__fixtureClock.advance(1500)); await page.waitForTimeout(80); await page.evaluate(() => window.__fixtureClock.advance(0)); };
  {
    const { context, page, errors } = await open({ scenario: 'tiers_import' });
    try {
      await runState(page, [...STATES.import_paste, { fill: ['.tiers-imp-ta textarea', PASTE], ms: 400 }]);
      await page.evaluate(() => { if (T.clock && T.clock.advance) T.clock.advance(20); });
      await runState(page, [{ ms: 300 }]);
      const zh = await page.evaluate(() => ({ sum: document.querySelector('.tiers-imp-sum').textContent, groups: document.querySelectorAll('.tiers-imp-groups .li').length,
        data: [...document.querySelectorAll('.tiers-imp-groups [translate="no"]')].map(e => e.textContent) }));
      assert.equal(zh.groups, 2, 'two physical-node groups');
      assert.deepEqual(zh.data, ['hk-box'], 'the group label is data');
      await page.evaluate(() => T.i18n.set('pseudo'));
      await settle(page);
      assert.deepEqual(await leaksOf(page), [], 'import sheet: leaks (pseudo)');
      const ps = await page.evaluate(() => ({ title: document.querySelector('.tiers-imp .ovl-title').textContent, sum: document.querySelector('.tiers-imp-sum').textContent, groups: document.querySelectorAll('.tiers-imp-groups .li').length }));
      assert.ok(!/[一-鿿]/.test(ps.title + ps.sum), `title and summary in pseudo: ${ps.title} / ${ps.sum}`);
      assert.equal(ps.groups, 2, 'the preview is kept');
      assert.deepEqual(errors(), [], 'import');
    } finally { await context.close(); }
  }
  {
    const { context, page, errors } = await open();
    try {
      await runState(page, STATES.vanguard_panel);
      await page.evaluate(() => { const b = [...document.querySelectorAll('.tiers-panel .ovl-foot button')].find(x => x.textContent.trim() === T.t('tiers.panel.edit')); if (!b) throw new Error('no edit button'); b.click(); });
      await runState(page, [{ fill: ['.tiers-panel [data-f="name"] input', 'typed-name'], ms: 600 }]);
      await page.evaluate(() => T.i18n.set('pseudo'));
      await settle(page);
      const r = await page.evaluate(() => ({
        foot: [...document.querySelectorAll('.tiers-panel .ovl-foot button')].map(b => b.textContent.trim()),
        sub: document.querySelector('.tiers-panel .ovl-sub').textContent,
        sub_cjk_outside: [...document.querySelectorAll('.tiers-panel .ovl-sub *, .tiers-panel .ovl-sub')].flatMap(e => [...e.childNodes].filter(n => n.nodeType === 3 && /[一-鿿]/.test(n.data) && !n.parentElement.closest('[translate="no"]')).map(n => n.data)),
        name: document.querySelector('.tiers-panel [data-f="name"] input').value,
      }));
      assert.equal(r.name, 'typed-name', 'what was typed stays');
      assert.ok(r.foot.length === 2 && r.foot.every(t => !/[一-鿿]/.test(t)), `footer in pseudo: ${JSON.stringify(r.foot)}`);
      assert.deepEqual(r.sub_cjk_outside, [], `subtitle in pseudo: ${r.sub}`);
      assert.deepEqual(errors(), [], 'edit');
    } finally { await context.close(); }
  }
});

test('fixture mode, pseudo: ASCII scenarios show only catalog words outside translate="no"',{ skip, timeout: 300000 }, async () => {
  const cells = Object.entries(FIXTURE_STATES).flatMap(([state, steps]) => FIXTURE_SCENARIOS[state].map(scenario => [scenario, state, steps]));
  for (const [scenario, state, steps] of cells) {
    const what = `${scenario} ${state}`;
    const { context, page, errors } = await open({ scenario, lang: 'pseudo' });
    try {
      assert.equal(await page.evaluate(() => window.__TESSERACT_DESIGN__ && window.__TESSERACT_DESIGN__.fixture), true, `${what}: fixture mode`);
      assert.equal(await page.evaluate(() => T.i18n.lang), 'pseudo');
      await runState(page, steps);
      const { report } = await capture(page);
      assert.ok(report.words.pages.length > 10, `${what}: the page rendered`);
      assert.deepEqual(wordProblems(report, 'pseudo'), [], `${what}: words`);
      assert.deepEqual(await leaksOf(page), [], `${what}: leaks`);
      assert.deepEqual(errors(), [], what);
    } finally { await context.close(); }
  }
});
