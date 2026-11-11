// T-P4-04d: message extraction · logs (ui/src/pages/logs.js, zh_CN/logs.json).
//
// Static: the logs catalog (shape, ICU, ids used ↔ ids defined, the v1 enums of PROTOCOL §5.5,
// the two prototype-only entries), no CJK literal and no @i18n-pending in logs.js / logs.css,
// the design module (design/logs_demo.js) reached only inside if (__DEMO__), the app bundle free
// of the logs demo, palette keywords (English ids in code, the rest in the catalog).
// Browser (local Chrome through playwright-core; skipped when Chrome is missing):
//   * regression (critique E1): the design target in sim mode under the deterministic fixture
//     renders the logs page with the T-P4-01 baseline text tree (test/baseline/logs.textree.json),
//     except the listed differences (ALLOWED: Intl month-day wording); and, in the raw view, the
//     reader's title and subtitle for four rows (READER_SNAPSHOT: 「常规」 now reads 「其他」);
//   * the fake second language (pseudo) in fixture mode (ASCII-only log rows,
//     test/fixtures/scenarios/logs_i18n_running.json): list, raw table, reader, filter popover,
//     footer show no CJK outside translate="no" and every text attribute comes from the catalog;
//   * the day separators in zh_CN, en_US and ru_RU (Intl), as a snapshot;
//   * a language switch rewrites the page in place (pseudo and back to zh_CN gives the same tree)
//     and closes an open row menu;
//   * the Bastion warning in the reader follows the event key; the palette finds the logs commands
//     by pinyin in zh_CN and by the English ids in any language.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import * as acorn from 'acorn';
import * as walk from 'acorn-walk';
import { compile } from '../../build/locales.mjs';
import { buildTarget, writeOutput } from '../../build.mjs';
import { scanCjkJs, scanCjkCss, isPending } from '../../tools/scan.mjs';

const UI = new URL('../../', import.meta.url);
const read = rel => readFileSync(new URL(rel, UI), 'utf8');
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const skip = !existsSync(CHROME) && 'local Chrome not found';

const CAT = JSON.parse(read('src/i18n/locales/zh_CN/logs.json'));
const SRC = read('src/pages/logs.js');
const CSS = read('css/pages/logs.css');
const DEMO_SRC = read('src/design/logs_demo.js');
const NOT_YET = 'B-23 / C7 未交付前不显示';

/* ------------------------------------------------------------------ the catalog */
test('logs.json: {m, _desc} entries, no _max, every message compiles, ids are logs.* lower-case', () => {
  const ids = Object.keys(CAT);
  assert.ok(ids.length >= 140, `only ${ids.length} messages`);
  for (const id of ids) {
    const e = CAT[id];
    assert.match(id, /^logs(\.[a-z0-9_]+)+$/, id);
    assert.deepEqual(Object.keys(e).sort(), ['_desc', 'm'], `${id}: keys`);
    assert.equal(typeof e.m, 'string'); assert.ok(e.m.length, `${id}: empty`);
    assert.ok(typeof e._desc === 'string' && e._desc.trim(), `${id}: _desc`);
    assert.doesNotThrow(() => compile(e.m, `zh_CN:${id}`), `${id}: ICU`);
  }
});

test('every logs.* id the page uses is in the catalog, and every catalog id is used', () => {
  const used = new Set([...SRC.matchAll(/['"`](logs\.[a-z0-9_.]+?)['"`]|data-t="(logs\.[a-z0-9_.]+)"|:(logs\.[a-z0-9_.]+)[;"]/g)].map(m => m[1] || m[2] || m[3]));
  // (T-P4-05c: logs.feed.reason is shown now, a dynamic family like the others)
  const families = ['logs.level.', 'logs.category.', 'logs.source.', 'logs.noise.', 'logs.palette.keywords.', 'logs.feed_reason.'];
  for (const id of used) {
    if (id.endsWith('.')) { assert.ok(families.includes(id), `dynamic id family ${id}`); continue; }
    assert.ok(CAT[id], `used but not in the catalog: ${id}`);
  }
  // logs.feed.reason (PROTOCOL logs.feed, T-P4-C2 §12): the words are in the catalog (Claude's
  // ruling of 10-12) and T-P4-05c shows them (the footer, a dynamic family above): exactly these three
  const RESERVED = 'logs.feed_reason.';
  assert.deepEqual(Object.keys(CAT).filter(id => id.startsWith(RESERVED)).sort(),
    ['controller_disabled', 'core_not_running', 'stream_error'].map(c => RESERVED + c));
  assert.ok(used.has(RESERVED), 'the page shows logs.feed.reason (the family is used)');
  for (const id of Object.keys(CAT)) {
    if (families.some(f => id.startsWith(f)) || id.startsWith(RESERVED)) continue;
    assert.ok(used.has(id), `in the catalog but never used: ${id}`);
  }
  // every palette command has its keywords
  const cmds = [...SRC.matchAll(/cmdOf\('logs-([a-z-]+)', '([a-z ]+)'/g)];
  assert.equal(cmds.length, 6);
  for (const [, id, en] of cmds) {
    const kw = CAT['logs.palette.keywords.' + id.replace(/-/g, '_')];
    assert.ok(kw, `keywords of ${id}`);
    assert.match(en, /^[a-z]+( [a-z]+)*$/, `${id}: English ids in code`);
    assert.match(kw.m, /[a-z]/, `${id}: pinyin in the zh_CN catalog`);
  }
});

test('levels, sources and categories cover the v1 enums (PROTOCOL §5.5); the prototype-only two say so', () => {
  const proto = readFileSync(new URL('../bridge_schema/PROTOCOL.md', UI), 'utf8');
  const list = re => { const m = re.exec(proto); assert.ok(m, String(re)); return m[1].split('|').map(s => s.trim()); };
  const levels = list(/`level` 为 `([a-z |]+)`/);
  const sources = list(/`source` 为 `([A-Z |]+)`/);
  const cats = list(/category 使用 `([a-z_ |]+)`/);
  assert.deepEqual(levels, ['debug', 'info', 'warn', 'error', 'critical']);
  assert.deepEqual(sources, ['TESSERACT', 'MIHOMO', 'ALERT', 'UNKNOWN']);
  assert.equal(cats.length, 9);
  for (const l of levels) assert.ok(CAT['logs.level.' + l], l);
  for (const s of sources) assert.ok(CAT['logs.source.' + s.toLowerCase()], s);
  for (const c of cats) assert.ok(CAT['logs.category.' + c], c);
  // the prototype's 正常 and 验证: one entry each, marked until B-23 / C7
  assert.ok(CAT['logs.level.ok'] && CAT['logs.level.ok']._desc.includes(NOT_YET));
  assert.ok(CAT['logs.category.verification'] && CAT['logs.category.verification']._desc.includes(NOT_YET));
  const extra = Object.keys(CAT).filter(k => /^logs\.(level|category)\./.test(k)).map(k => k.split('.')[2]).filter(k => !levels.includes(k) && !cats.includes(k));
  assert.deepEqual(extra.sort(), ['ok', 'verification']);
  // the page keeps the v1 ids on its records
  assert.match(SRC, /const CATS = \['runtime', 'routing', 'health', 'circuit_breaker', 'verification', 'update', 'configuration', 'connection', 'alert', 'other'\]/);
  assert.match(SRC, /const SRCS = \['TESSERACT', 'MIHOMO', 'UNKNOWN'\]/);
});

test('no CJK literal and no @i18n-pending in logs.js / logs.css; no Chinese text matching', () => {
  assert.deepEqual(scanCjkJs(SRC, 'logs.js'), []);
  assert.deepEqual(scanCjkCss(CSS, 'logs.css'), []);
  assert.equal(isPending(SRC, 'js'), false);
  assert.equal(isPending(CSS, 'css'), false);
  // the role words, the Bastion note and the 原因 prefix are no longer matched as text; 11-05: the
  // reader adds no Bastion warning at all (the letter for users and the fallback notice say it)
  assert.doesNotMatch(SRC, /BASTION_NOTE|logs-why|BASTION_KEYS/);
});

// T-P4-07 (决定_第八波裁定, which authorises changing this assertion): the B-22 suggestion card
// is a B item behind T.rt.feature('B-22'), not behind __DEMO__ (P4 rule 14); the page imports
// nothing from design/ and asks the advice source at T.ext 'logs/advice', which the design
// target's design/demo.js fills with design/logs_demo.js FIX.
test('design/logs_demo.js is not imported by the page; B-22 advice comes from T.ext logs/advice', () => {
  assert.doesNotMatch(SRC, /^\s*import\b[^;]*design\//m, 'the page imports nothing from design/');
  assert.doesNotMatch(SRC, /\bDEMO\.|__DEMO__/);
  const ast = acorn.parse(SRC, { ecmaVersion: 'latest', sourceType: 'module', locations: true });
  let adviceCalls = 0;
  walk.ancestor(ast, {
    CallExpression(n, _, anc) {
      if (n.callee.type !== 'Identifier' || n.callee.name !== 'adviceFor') return;
      adviceCalls++;
      const gated = anc.some(x => x.type === 'ConditionalExpression' && JSON.stringify(x.test).includes('"value":"B-22"'));
      assert.ok(gated, `adviceFor at line ${n.loc.start.line} outside T.rt.feature('B-22')`);
    },
  });
  assert.ok(adviceCalls >= 1, `adviceFor calls: ${adviceCalls}`);
  assert.match(SRC, /T\.ext\.get\('logs\/advice'\)/);
  // the demo module keeps the prototype's wording; the page module none of it
  assert.match(DEMO_SRC, /export const FIX/);
  assert.match(DEMO_SRC, /export function seedRecords/);
  assert.match(read('src/design/demo.js'), /T\.ext\.set\('logs\/advice'/);
  // no static import of the simulator's data: esbuild would list design/demo-data.js among the
  // app bundle's inputs even with the code dropped (design_sim.test.mjs forbids that)
  assert.doesNotMatch(DEMO_SRC, /^\s*import\b[^;]*demo-data/m);
  assert.doesNotMatch(SRC, /^\s*import\b[^;]*demo-data/m);
});

test('the app bundle carries no logs demo (seed, sim mapping, suggestion cards, DEBUG lines)', async () => {
  const { files } = await buildTarget({ target: 'app' });
  const main = [...files.keys()].find(k => /^app\.[A-Z0-9]+\.js$/.test(k));
  const js = new TextDecoder().decode(files.get(main));
  // (T-P4-07: the B-22 card builder is in the app bundle behind T.rt.feature('B-22'); its prototype
  // words stay in the design target: the FIX texts below must not be there)
  for (const marker of ['seedLogRecords', 'Desktop runtime start requested', 'Sniffer] sniffed', 'logs-demo', 'sub403', 'selfheal', 'proxy.system_overridden']) {
    assert.ok(!js.includes(marker), `app bundle contains ${marker}`);
  }
});

/* ------------------------------------------------------------------ in the browser */
let dir, browser, run, cases, designIndex;
before(async () => {
  if (skip) return;
  dir = mkdtempSync(join(tmpdir(), 'tesseract-logs-i18n-'));
  const { files } = await buildTarget({ target: 'design', pseudo: true });
  writeOutput(join(dir, 'design'), files);
  designIndex = join(dir, 'design', 'index.html');
  run = await import('../render/run.mjs');
  cases = await import('../render/cases/logs_i18n.mjs');
  browser = await run.launch();
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

async function fixturePage(lang, scenario = cases.RUNNING) {
  const cell = await run.openCell(browser, { distDir: join(dir, 'design'), scenario, lang, size: '1280x800', page: 'logs' });
  await run.runAction(cell.page, { eval: cases.feedFixtureLogs });
  await run.runAction(cell.page, { page_ms: 400 });
  return cell;
}
const clickRow = text => run => run.page.evaluate(t => [...document.querySelectorAll('#logs-events .logs-row, #logs-rows .logs-rrow')].find(r => r.textContent.includes(t)).click(), text);
const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/u;

// The only accepted differences from the T-P4-01 baseline (sim mode, deterministic fixture):
// the list's day labels use Intl month-day wording (zh-CN: 「9月28日」, the prototype wrote
// 「9 月 28 日」). Everything else is identical, row by row.
const ALLOWED = [{ why: 'Intl DateTimeFormat month-day (zh-CN)', fix: row => (row[1] === 'span' && /\d+ 月 \d+ 日 · 周/.test(row[2]) ? [row[0], row[1], row[2].replace(/(\d+) 月 (\d+) 日/, '$1月$2日'), row[3], row[4], row[5]] : row) }];

// T-P4-05c (logs from logs.batch / logs.tail, demo code out of the page; out-of-scope test update
// listed in its report): the records are the design simulator's own v1 Log rows, so
//   - the history row 「这里从 … 开始 · 更早的 1,208 条只保存在日志文件里 · 打开日志文件夹」 is gone
//     (the prototype's DROPPED = 1208 was made up; the simulator's history lost nothing);
//   - the demo button 「层级回退」 is gone (映射_logs §6);
//   - 正常 reads 信息 (B-23: the v1 Log has no positive level);
//   - one sim record keeps the simulator's own detail (the page no longer rewrites sim records);
//   - the running line and a folded group's time sit one level up or down (written as nodes /
//     through the odometer swap instead of the prototype's html swap): depth only, same words.
const ALLOWED_05C = {
  drop: row => (row[1] === 'button' && row[2] === '层级回退') || (row[1] === 'button' && row[2] === '打开日志文件夹')
    || /^这里从 /.test(row[2]) || /^更早的 .+ 条只保存在日志文件里$/.test(row[2]),
  fix: row => (row[2] === '正常' ? [row[0], row[1], '信息', ...row.slice(3)]
    : row[2] === '出口校验通过，流量经 东京 Gateway 01 离开（3/3 可用）。' ? [row[0], row[1], '东京 Gateway 01 · 3/3', ...row.slice(3)] : row),
  depth: new Set(['已运行 2 小时 14 分', '09:45:44 开始 ·', '22:29:15', '10:45:59']),
};
// F-03 (user 10-28): a tier is named by its code name alone (自建 · 订阅 · 应急 · 固定出口 were the
// product's advice): the role rows of the tier links go, and a name + role in the text keeps the name
const ROLE_WORDS = ['自建', '订阅', '应急', '固定出口'];
const ALLOWED_F03 = {
  drop: row => row[1] === 'span' && ROLE_WORDS.includes(row[2]),
  fix: row => row.map(v => (typeof v === 'string' ? v.replace(/\b(Vanguard|Sentinel|Bastion|Gateway) (自建|订阅|应急|固定出口)/g, '$1') : v)),
};

// 11-05 in-page title bar: the window's own buttons (#wc, after #app; named, no text) are new
// chrome on every page, not part of the logs page
const WINDOW_BUTTONS = ['最小化', '最大化', '关闭'];
const ALLOWED_WC = { drop: row => row[1] === 'button' && row[2] === '' && WINDOW_BUTTONS.includes(row[3]) };
// 11-05 the letter for users: the product name is a button (named 「Tesseract · 给用户的信…」), so its
// row is new and the wordmark under it sits one level deeper; the shell's frame, not the logs page
const LETTER_BUTTON = row => row[1] === 'button' && row[2] === '' && /^Tesseract · /.test(row[3]);
function withoutLetterButton(tree) {
  const out = [];
  for (let i = 0; i < tree.length; i++) {
    if (!LETTER_BUTTON(tree[i])) { out.push(tree[i]); continue; }
    const word = tree[i + 1];
    if (word && word[2] === 'Tesseract') { out.push([word[0] - 1, ...word.slice(1)]); i++; }
  }
  return out;
}

test('regression: sim mode, deterministic fixture: the logs text tree equals the T-P4-01 baseline (allowed: Intl day labels)', { skip }, async () => {
  const cap = await import('../baseline/capture.mjs');
  const context = await cap.newContext(browser);
  const captured = await cap.capturePage(context, designIndex, 'logs');
  await context.close();
  const r = { ...captured, tree: withoutLetterButton(captured.tree.filter(row => !ALLOWED_WC.drop(row))) };
  assert.deepEqual(captured.tree.slice(-3).map(row => row[3]), WINDOW_BUTTONS, '11-05: the three window buttons close the tree');
  assert.equal(captured.tree.filter(LETTER_BUTTON).length, 1, '11-05: the product name is the letter button');
  assert.equal(captured.tree.length - r.tree.length, 4);
  assert.deepEqual(r.errors, []);
  const base = JSON.parse(read('test/baseline/logs.textree.json'));
  const intl = base.map(row => ALLOWED.reduce((x, a) => a.fix(x), row));
  const changed = base.filter((row, i) => JSON.stringify(row) !== JSON.stringify(intl[i]));
  assert.equal(changed.length, 2, 'two day labels differ by the Intl wording');
  const after05c = intl.filter(row => !ALLOWED_05C.drop(row)).map(ALLOWED_05C.fix);
  assert.equal(base.length - after05c.length, 4, 'T-P4-05c: the history row (three parts) and the demo button');
  const expected = after05c.filter(row => !ALLOWED_F03.drop(row)).map(ALLOWED_F03.fix);
  assert.ok(after05c.length - expected.length > 0, 'F-03: the role rows were in the prototype tree');
  assert.equal(r.tree.length, expected.length);
  for (let i = 0; i < expected.length; i++) {
    const want = ALLOWED_05C.depth.has(expected[i][2]) ? [r.tree[i][0], ...expected[i].slice(1)] : expected[i];
    assert.deepEqual(r.tree[i], want, `row ${i}`);
  }
});

// The reader in sim mode (raw view): the title and the subtitle 「source · category」. The
// prototype wrote its own category word (`${l.src} · ${l.cat}`); the page writes the catalog's
// word for the v1 id. The one accepted change: 「常规」 (v1 `other`) now reads 「其他」, as on the
// filter chips. Every other row reads as in the prototype.
const READER_SNAPSHOT = [
  ['DNS resolver qualified', ['DNS 解析器已就绪', 'Tesseract · 其他']], // prototype: Tesseract · 常规
  ['Egress check failed', ['代理出口不可用', 'Tesseract · 路由']],
  ['Alert delivered', ['提醒已推送到手机', 'Tesseract · 运行告警']],
  ['Traceback', ['Tesseract 原始日志', 'Tesseract · 更新']],
];
test('regression: sim mode, raw view: the reader\'s title and subtitle (accepted: 常规 → 其他)', { skip }, async () => {
  const cap = await import('../baseline/capture.mjs');
  const context = await cap.newContext(browser);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e && e.message || e)));
  await page.goto(pathToFileURL(designIndex).href + '#logs', { waitUntil: 'load' });
  const settle = async ms => { await page.evaluate(m => window.__fixtureClock.advance(m), ms); await page.waitForTimeout(100); };
  await settle(cap.SETTLE_MS);
  await page.click('#logs-view [data-v="raw"]'); await settle(400);
  for (const [text, want] of READER_SNAPSHOT) {
    await page.evaluate(t => [...document.querySelectorAll('#logs-rows .logs-rrow')].find(r => r.textContent.includes(t)).click(), text);
    await settle(400);
    const got = await page.evaluate(() => { const r = document.querySelector('.logs-reader'); return [r.querySelector('.ovl-title').textContent.trim(), r.querySelector('.ovl-sub').textContent.trim()]; });
    assert.deepEqual(got, want, text);
  }
  assert.deepEqual([...errors, ...await page.evaluate(() => window.__fixtureClock.errors.slice())], []);
  await context.close();
});

test('pseudo (fixture mode, ASCII log rows): no CJK outside translate="no" in the list, raw table, reader, filter, footer', { skip }, async () => {
  const cell = await fixturePage('pseudo');
  const { page } = cell;
  const look = async where => {
    const r = await page.evaluate(cases.logsTextInPage);
    const bad = [...r.texts.filter(s => CJK.test(s)), ...r.attrs.filter(([, v]) => CJK.test(v)).map(([a, v]) => `${a}=${v}`)];
    assert.deepEqual(bad, [], where);
    const plain = r.attrs.filter(([, v]) => !v.includes('⟦') && !/^(?:Tesseract|Mihomo)$/.test(v));
    assert.deepEqual(plain, [], `${where}: attributes not from the catalog`);
    assert.ok(r.texts.some(s => s.includes('⟦')), `${where}: catalog text shown`);
    return r;
  };
  const ev = await look('events');
  assert.ok(ev.days.length >= 3, `day separators ${ev.days}`);
  // the data is there, under translate="no"
  const data = await page.evaluate(() => [...document.querySelectorAll('#logs-events [translate="no"]')].map(e => e.textContent).join(' | '));
  assert.match(data, /Traffic on Bastion/);
  await clickRow('Traffic on Bastion')(cell); await run.runAction(page, { page_ms: 400 });
  await look('reader');
  await page.click('.logs-reader .logs-rd-more'); await run.runAction(page, { page_ms: 400 });
  await look('reader diagnostics');
  await page.click('#logs-view [data-v="raw"]'); await run.runAction(page, { page_ms: 400 });
  await look('raw');
  await clickRow('Traceback')(cell); await run.runAction(page, { page_ms: 400 });
  await look('raw reader');
  await page.click('.logs-reader .ovl-x'); await run.runAction(page, { page_ms: 400 });
  await page.click('#logs-filter'); await run.runAction(page, { page_ms: 400 });
  await look('filter');
  await page.keyboard.press('Escape'); await run.runAction(page, { page_ms: 400 });
  await page.click('#logs-more'); await run.runAction(page, { page_ms: 400 });
  await look('more');
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

// Intl wording of the list's day separators (fixture clock: 2026-09-29 12:00 Asia/Shanghai; rows
// on 09-21, 09-26, 09-28, 09-29). zh_CN is the source; en_US and ru_RU show the Intl parts joined
// by their own catalog's message (logs.list.day.recent / .date: only separators and order are
// translated). (T-P5-04) en_US writes the English order, weekday first: "Mon, September 21"
// ({weekday}, {date}); ru_RU keeps the zh_CN order.
const DAY_SNAPSHOT = {
  zh_CN: ['9月21日 · 周一', '9月26日 · 周六', '昨天 · 9月28日 · 周一', '今天 · 9月29日 · 周二'],
  en_US: ['Mon, September 21', 'Sat, September 26', 'Yesterday · Mon, September 28', 'Today · Tue, September 29'],
  ru_RU: ['21 сентября · пн', '26 сентября · сб', 'Вчера · 28 сентября · пн', 'Сегодня · 29 сентября · вт'],
};
for (const lang of Object.keys(DAY_SNAPSHOT)) {
  test(`day separators by Intl in ${lang}`, { skip }, async () => {
    const cell = await fixturePage(lang);
    const d = await cell.page.evaluate(cases.dayLabelsInPage);
    assert.deepEqual(d.events, DAY_SNAPSHOT[lang]);
    assert.deepEqual(cell.errors, []);
    await cell.context.close();
  });
}

test('a language switch rewrites the page in place; back in zh_CN the page reads as before', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  await clickRow('Traffic on Bastion')(cell); await run.runAction(page, { page_ms: 400 });
  const tree0 = await page.evaluate(cases.logsTreeInPage);
  await page.evaluate(() => T.i18n.set('pseudo')); await run.runAction(page, { page_ms: 600 });
  const r = await page.evaluate(cases.logsTextInPage);
  assert.deepEqual(r.texts.filter(s => CJK.test(s)), [], 'pseudo: CJK left after the switch');
  assert.deepEqual(r.attrs.filter(([, v]) => CJK.test(v)), [], 'pseudo: CJK attributes left after the switch');
  assert.ok(await page.evaluate(() => !!document.querySelector('.logs-reader .logs-rd-kv')), 'reader kept open and repainted');
  await page.evaluate(() => T.i18n.set('zh_CN')); await run.runAction(page, { page_ms: 600 });
  const tree1 = await page.evaluate(cases.logsTreeInPage);
  // same words in the same order; the reader's title and subtitle may now sit one level deeper
  // (T.drawer setTitle / setSubtitle write through a swap span after the first paint)
  const words = tree => tree.map(r => r.slice(2));
  assert.deepEqual(words(tree1), words(tree0));
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

test('a language switch closes an open row menu (it is rebuilt in the new language on the next open)', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  const row = await page.evaluateHandle(() => [...document.querySelectorAll('#logs-events .logs-row')].find(r => r.textContent.includes('Traffic on Bastion')));
  await row.asElement().click({ button: 'right' }); await run.runAction(page, { page_ms: 400 });
  const menus = () => page.evaluate(() => [...document.querySelectorAll('.menu')].filter(m => m.isConnected && m.checkVisibility()).map(m => m.textContent));
  const open = await menus();
  assert.equal(open.length, 1, 'row menu open');
  assert.ok(CJK.test(open[0]), 'row menu in zh_CN');
  await page.evaluate(() => T.i18n.set('pseudo')); await run.runAction(page, { page_ms: 600 });
  assert.deepEqual(await menus(), [], 'row menu closed by the switch');
  await row.asElement().click({ button: 'right' }); await run.runAction(page, { page_ms: 400 });
  const again = await menus();
  assert.equal(again.length, 1);
  assert.ok(!CJK.test(again[0]) && again[0].includes('⟦'), `row menu rebuilt in pseudo (${again[0]})`);
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

// 11-05: was 「the Bastion warning follows the event key」; the reader no longer adds a standing
// public-node warning to any event (said once at the fallback notice and in the letter for users)
test('the reader adds no standing warning to a Bastion event or any other', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  const warn = () => page.evaluate(() => !!document.querySelector('.logs-reader .logs-rd .callout-warning'));
  await clickRow('Traffic on Bastion')(cell); await run.runAction(page, { page_ms: 400 });
  assert.equal(await warn(), false, 'tier.bastion_active');
  await clickRow('Traffic on Sentinel')(cell); await run.runAction(page, { page_ms: 400 });
  assert.equal(await warn(), false, 'tier.sentinel_active');
  await cell.context.close();
});

test('palette: logs commands by pinyin (zh_CN catalog) and by English ids (any language)', { skip }, async () => {
  const find = async (cell, q) => {
    await cell.page.evaluate(() => { if (!T.cmd.isOpen) T.cmd.open(); });
    await run.runAction(cell.page, { page_ms: 300 });
    await cell.page.fill('.cmdk input', q);
    await run.runAction(cell.page, { page_ms: 300 });
    return cell.page.evaluate(() => [...document.querySelectorAll('.cmdk-list .cmdk-item')].map(e => e.textContent.replace(/\s+/g, ' ').trim()));
  };
  const zh = await fixturePage('zh_CN');
  assert.ok((await find(zh, 'zhenduan')).some(s => s.includes('导出诊断包')), 'zh_CN: pinyin');
  assert.ok((await find(zh, 'bundle')).some(s => s.includes('导出诊断包')), 'zh_CN: English id');
  await zh.context.close();
  const ps = await fixturePage('pseudo');
  const hits = await find(ps, 'bundle');
  assert.ok(hits.length >= 1 && hits.every(s => !CJK.test(s)), `pseudo: English id finds the command (${hits})`);
  assert.equal((await find(ps, 'zhenduan')).length, 0, 'pseudo: no pinyin outside zh_CN');
  await ps.context.close();
});
