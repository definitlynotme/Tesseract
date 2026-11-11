// T-P4-04b: message extraction of the overview page (pages/overview.js, catalog overview.*).
//
// Source checks (no browser): no @i18n-pending, no CJK literal, no injection / inline-style sink
// (scan.mjs rules), every id the page names exists in the zh_CN catalog and every overview.* id is
// used; the demo control and its words do not reach the app bundle.
//
// Browser checks (local Chrome through playwright-core, the real design target built in memory,
// file://, no network, deterministic fixture clock; skipped when Chrome is not installed):
//  - regression (critique E1): the simulator walk of render/cases/overview_i18n.mjs; running,
//    stopped and starting (phase 0 and 1) must give the prototype's visible text tree and
//    data-tips (running: the T-P4-01 baseline file; the others: BEFORE), up to T.fmt's allowed
//    differences;
//  - language switch: running with the mode confirmation open, fallback, kill switch off with the
//    administrator note, stopped, starting: switching to the pseudo language leaves no CJK text
//    outside translate="no", every attribute catalog text and no leak of the matrix's check in the
//    page; switching back to zh_CN gives the same tree and tips again (T.ti relabel + relocalize());
//  - copy / busy buttons: a language switch before a copy, or during a T.ui.busy job, still ends
//    with the new language on the button (aria-label, data-tip, label);
//  - fixture mode (critique E2): the three ASCII scenarios overview_i18n_* in pseudo. (The page
//    does not read the fixture's data yet, T-P4-05a: node names and cities still come from the
//    design data, all under translate="no"; this checks that no catalog word is left out, not
//    that catalog words are kept out of translate="no".)
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildTarget, writeOutput } from '../../build.mjs';
import { isPending, scanCjkJs, scanInjectJs, scanStyleJs } from '../../tools/scan.mjs';
import { diffTrees, fmtAllowances } from '../render/textree.mjs';
import { cellChecksInPage, LEAK_WHITELIST } from '../render/checks.mjs';
import {
  BEFORE, DEMO_BADGE, SIM_STEPS, baselineSlice, overviewTreeInPage, overviewWordsInPage, readBaseline, relabelRoundTrip, walkSim, wordProblems,
} from '../render/cases/overview_i18n.mjs';

const UI = new URL('../../', import.meta.url);
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const skip = !existsSync(CHROME) && 'local Chrome not found';
const SRC = readFileSync(new URL('src/pages/overview.js', UI), 'utf8');
const catalog = ns => JSON.parse(readFileSync(new URL(`src/i18n/locales/zh_CN/${ns}.json`, UI), 'utf8'));

/* ---------------------------------------------------------------- source */

test('overview.js: no @i18n-pending, no CJK literal, no HTML sink or inline style attribute', () => {
  assert.equal(isPending(SRC, 'js'), false, 'the first line still marks the file @i18n-pending');
  assert.deepEqual(scanCjkJs(SRC, 'src/pages/overview.js'), []);
  assert.deepEqual(scanInjectJs(SRC, 'src/pages/overview.js'), []);
  assert.deepEqual(scanStyleJs(SRC, 'src/pages/overview.js'), []);
  // the three high-risk sinks of mapping_overview §6 (node names into HTML) are gone with the rest
  assert.doesNotMatch(SRC, /\{\s*html:\s*true/);
  assert.doesNotMatch(SRC, /insertAdjacentHTML/);
});

test('every message id the page names exists; every overview.* id is used', () => {
  const cats = {};
  const has = id => { const ns = id.split('.')[0]; cats[ns] = cats[ns] || catalog(ns); return Object.prototype.hasOwnProperty.call(cats[ns], id); };
  const literal = [...SRC.matchAll(/['"`:]((?:overview|mode|tier|common|fmt)\.[a-z0-9_]+(?:\.[a-z0-9_]+)+)/g)].map(m => m[1]);
  // (T-P4-07: the demo control's words overview.demo.* moved with it to the design target's
  // design/demo.js, which registers it at the page's extension point)
  const demoSrc = readFileSync(new URL('../../src/design/demo.js', import.meta.url), 'utf8');
  literal.push(...[...demoSrc.matchAll(/['"`]((?:overview)\.demo\.[a-z0-9_]+)/g)].map(m => m[1]));
  // ids built at run time: the phase words (F-01: the label only; the pill says the phase and
  // nothing repeats it), the traffic direction words (F-03: no tier role words any more)
  const built = [
    ...['collect', 'verify', 'ready'].map(k => `overview.phase.${k}.label`),
    'overview.traffic.down', 'overview.traffic.up',
  ];
  assert.doesNotMatch(SRC, /tier\.\$\{[^}]+\}\.role|ov-role|dc-role/, 'F-03: no role word');
  assert.match(SRC, /`overview\.phase\.\$\{phaseKey\(ph\)\}\.label`/);
  assert.doesNotMatch(SRC, /overview\.phase\.[a-z_${}()]*\.hint/, 'F-01: no phase hint');
  assert.match(SRC, /`overview\.traffic\.\$\{k\}`/);
  const used = new Set([...literal, ...built]);
  const missing = [...used].filter(id => !has(id));
  assert.deepEqual(missing, [], 'ids not in the zh_CN catalog');
  // overview.exit_reason.* (reason_code words, T-P4-05a) are read through T.rt.reasonText, not by id
  const unused = Object.keys(catalog('overview')).filter(id => !used.has(id) && !id.startsWith('overview.exit_reason.'));
  assert.deepEqual(unused, [], 'overview.* ids the page never uses');
  for (const [id, e] of Object.entries(catalog('overview'))) {
    assert.ok(typeof e.m === 'string' && e.m, `${id}: m`);
    assert.ok(typeof e._desc === 'string' && e._desc.trim(), `${id}: _desc`);
    assert.equal(e._max, undefined, `${id}: no _max (critique F8)`);
  }
});

// (T-P4-07, 决定_第八波裁定: the demo control moved to the design target's design/demo.js, which
// registers it at the page's extension point T.ext 'overview/deck'; the page has no demo code)
test('the demo control and its words stay out of the app bundle (design/demo.js via T.ext)', async () => {
  const { files, manifest } = await buildTarget({ target: 'app' });
  const main = manifest.files.find(f => /^app\.[A-Z0-9]+\.js$/.test(f.path));
  const js = new TextDecoder().decode(files.get(main.path));
  assert.ok(js.includes('overview.hero.eyebrow'), 'the overview page is in the app bundle');
  for (const s of ['ov-demo', 'overview.demo.']) assert.ok(!js.includes(s), `app bundle contains ${s}`);
  assert.doesNotMatch(SRC, /ov-demo|overview\.demo\.|__DEMO__|T\.sim/);
  assert.match(SRC, /T\.ext\.get\('overview\/deck'\)/);
  assert.match(readFileSync(new URL('../../src/design/demo.js', import.meta.url), 'utf8'), /T\.ext\.set\('overview\/deck'[\s\S]*id: 'ov-demo'/);
});

/* --------------------------------------------------------------- browser */

let dir, browser;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-ov04b-'));
  const { files } = await buildTarget({ target: 'design', pseudo: true });
  writeOutput(dir, files);
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu', '--font-render-hinting=none', '--host-resolver-rules=MAP * ~NOTFOUND'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

// one page per check: 1280x800, reduced motion, zh-CN, the fixture clock; sim mode unless a
// scenario is given (fixture mode)
async function open({ scenario = null, lang = null } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, reducedMotion: 'reduce', locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light' });
  await context.route('**/*', r => (r.request().url().startsWith('file:') ? r.continue() : r.abort()));
  await context.addInitScript({ path: new URL('test/render/fixture_clock.js', UI).pathname.replace(/^\/([A-Za-z]:)/, '$1') });
  if (scenario) await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, readFileSync(new URL(`test/fixtures/scenarios/${scenario}.json`, UI), 'utf8'));
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message)));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await page.goto(pathToFileURL(join(dir, 'index.html')).href + (lang ? `?lang=${lang}` : '') + '#overview', { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(ms => window.__fixtureClock.advance(ms), 3000);
  await page.waitForTimeout(200);
  await page.evaluate(() => window.__fixtureClock.advance(0));
  return { context, page, errors: () => errors.concat([]) };
}

const ALLOW = fmtAllowances();
// T-P4-05a: the running page reads the runtime where the prototype had fixed words or numbers;
// these three rows differ from the T-P4-01 baseline by design (mapping_overview §3 G2, L3, E13)
const DATA_05A = [
  { from: '本次会话', to: '累计', why: 'G2 / B-02b: the traffic scope says cumulative (since the last config apply), never this session; F-01: the word alone' },
  { from: '44', to: '42', why: 'E11 / E13 (B-01): the carrier node line shows nodes.list latency_ms, no 1 Hz simulated jitter' },
];
// T-P4-H2 item 3 (8th-wave ruling): the design simulator reports a chosen Gateway as the carrier
// itself, the runtime's shape (desktop_runtime published route: FINAL-EXIT -> EXIT-SELECT ->
// <Gateway node>, managed_tier gateway; the tier under it is not reported). The demo data chooses
// 东京 Gateway 01, so the running page shows the Gateway carrier where the prototype showed
// Vanguard with the Gateway behind it ("经由 香港 IEPL 03 → 东京 Gateway 01", a chain the runtime
// never publishes). These rows say the T-P4-05a M1 words (frozen, overview.json) by design:
const DATA_H2 = [
  { from: 'Vanguard', to: 'Gateway', why: 'hero: the carrier is the chosen Gateway (T-P4-H2 item 3, the runtime shape)' },
  { from: '经由', to: '经由未知节点', why: 'route line: the Gateway exit through an unreported node (overview.exit.route.gateway, 05a M1; F-04 wording)' },
  { from: '承载流量', to: '承载未确认', why: 'front deck card pill: the carrier under the Gateway is unconfirmed (overview.deck.pill_unconfirmed)' },
  { from: '香港 IEPL 03 · Hysteria2 · ms', to: '当前节点未知 · 由 Gateway 出口', why: 'front deck card node line (overview.deck.node.unconfirmed)' },
  { from: '2/2 可用', to: '2/2 · 承载未确认', why: 'front deck card stat (overview.deck.stat.unconfirmed)' },
];
// ...and two prototype rows have no counterpart: the route's via node (the runtime does not report
// the tier under the Gateway) and that node's latency on the front deck card
const H2_GONE = [[10, 'span', '香港 IEPL 03'], [13, 'b', '44']];
const withoutGone = rows => {
  const out = rows.filter(r => !H2_GONE.some(g => r[0] === g[0] && r[1] === g[1] && r[2] === g[2]));
  assert.equal(rows.length - out.length, H2_GONE.length, 'each H2_GONE row is in the prototype tree once');
  return out;
};
// stopped: the same scope word and the protocol total (D6: configured while stopped; F-01: entries
// only, without its source); starting, a dash until the Core loaded the configuration (F-01: no
// 正在加载配置…). (The stopped route line is gone with F-01, F01_GONE)
const DATA_05A_STOPPED = [
  { from: '上次会话', to: '累计', why: 'G2 / B-02b: the traffic scope says cumulative (since the last config apply), never the last session; F-01: the word alone' },
  { from: '未连接', to: '未启动', why: 'F-01 round 1: stopped, the hero says the service is not started (not "not connected")' },
  { from: '正在加载配置…', to: '—', why: 'L7 / F-01: starting, the total is a dash until the Core has loaded the configuration' },
];
/* F-04 (user 11-01, .build/新界面迁移_工单/F-04.md): no internal words. The pool cell is 节点 (not
   节点池); the protocol total (协议入口) is gone, and so is the Gateway hero sentence (经过的回退层
   暂时无法确认: the hero says nothing where it has no count, as for any carrier) */
const DATA_F04 = [{ from: '节点池', to: '节点', why: 'F-04: the pool cell, its accessible name and the strip name say 节点' }];
const F04_GONE = {
  running: [[10, 'span', '全部 2 个节点可用'], [10, 'span', '10 个节点 · 13 个协议入口']],
  stopped: [[10, 'span', '上次加载的配置 · 启动后刷新']],
};
const withoutF04 = (rows, state) => {
  const out = rows.slice();
  for (const g of F04_GONE[state] || []) {
    const i = out.findIndex(r => r[0] === g[0] && r[1] === g[1] && r[2] === g[2]);
    assert.ok(i >= 0, `F04_GONE ${state}: ${JSON.stringify(g)} is not in the expected tree`);
    out.splice(i, 1);
  }
  return out;
};
/* 配置整理 P1 (user 11-04, .build/配置可视化整理方案_20261104.md): 网络模式 is 接管方式, two toggles
   系统代理 / 虚拟网卡 that can both be on; 全局 (it meant TUN, not Clash's global) is 虚拟网卡, and the
   third button 混合 is gone (both toggles on) in every state */
const DATA_P1 = [
  // (one rule per field: the strip name carries F-04's 节点 too)
  { from: '网络模式、熔断保护与节点池', to: '接管方式、熔断保护与节点', why: 'P1 + F-04: the strip name says 接管方式 and 节点' },
  { from: '网络模式', to: '接管方式', why: 'P1: the mode cell and its accessible name say 接管方式' },
  { from: '全局', to: '虚拟网卡', why: 'P1: the TUN toggle says 虚拟网卡' },
];
const withoutP1 = rows => {
  const out = rows.slice(), i = out.findIndex(r => r[1] === 'button' && r[2] === '混合');
  assert.ok(i >= 0, 'P1: the 混合 button is in the expected tree');
  out.splice(i, 1);
  return out;
};
/* F-01 (user feedback 10-27, .build/新界面迁移_工单/F-01.md): the page no longer explains how it
   works, and no line repeats a state the name, the pill or a badge already shows. These prototype
   rows are gone by design: each is taken out of the expected tree once (it must be there) before
   the diff. Running: the armed kill switch's rule. Stopped: the hero hint with its tier tag, the
   exit card's two lines, the route forecast, 服务未启动 beside the deck title, the front card's
   fallback order, the chart's paused note, the mode / kill switch / pool sentences. Starting: the
   phase repeated in the hero, on the front card and in the pool cell, the exit card's two lines,
   the route forecast, the chart note, the mode and kill switch forecasts */
const START_GONE = phase => [
  [10, 'span', phase], [12, 'span', '服务启动中'], [11, 'span', '就绪后经当前出口自动查询，不暴露本机 IP'],
  [9, 'span', '启动后经由'], [10, 'span', 'Vanguard'], [10, 'span', '东京 Gateway 01'], [12, 'span', phase],
  [9, 'span', '正在连接，就绪后继续采样'], [11, 'span', '就绪后接管 ·'], [12, 'span', '127.0.0.1:7890'], [12, 'span', '就绪后自动布防'], [12, 'span', '正在收集节点…'],
];
const F01_GONE = {
  running: [[12, 'span', '所有层级都不可用时阻断出站']],
  stopped: [
    [10, 'span', '服务未启动 · 启动后优先使用'], [12, 'b', 'Vanguard'], [12, 'span', '自建'],
    [12, 'span', '服务未启动'], [11, 'span', '启动后会经当前出口自动查询，不暴露本机 IP'],
    [9, 'span', '启动后经由'], [10, 'span', 'Vanguard'], [10, 'span', '东京 Gateway 01'],
    [9, 'span', '服务未启动'], [12, 'span', 'Vanguard → Sentinel；都不可用时阻断出站'], [13, 'span', '启动后依次回退：'],
    [9, 'span', '服务已停止，暂停采样'], [11, 'span', '未接管 · 启动后接管'], [12, 'span', '127.0.0.1:7890'],
    [12, 'span', '启动后自动布防'], [12, 'span', '启动后检测'],
    // (round 1: not running, the front card's top band folds away, its state pill with it)
    [13, 'span', '未连接'],
  ],
  starting0: START_GONE('正在收集节点…'),
  starting1: START_GONE('正在验证出口…'),
};
// F-03 (user 10-28): the role words beside the tier names (自建 · 订阅 · 应急 · 固定出口) are gone: their
// rows leave the prototype tree, and a name + role in an accessible name or a tip keeps the name
const ROLES = ['自建', '订阅', '应急', '固定出口'];
const ROLE_PAIR = /\b(Vanguard|Sentinel|Bastion|Gateway) (自建|订阅|应急|固定出口)/g;
const noRole = v => (typeof v === 'string' ? v.replace(ROLE_PAIR, '$1') : v);
const withoutRoles = rows => rows.filter(r => !(r[1] === 'span' && ROLES.includes(r[2]))).map(r => r.map(noRole));
const tipsNoRole = tips => tips.map(([p, t]) => [p, noRole(t)]);
const withoutF01 = (rows, state) => {
  const out = rows.slice();
  for (const g of F01_GONE[state] || []) {
    const i = out.findIndex(r => r[0] === g[0] && r[1] === g[1] && r[2] === g[2]);
    assert.ok(i >= 0, `F01_GONE ${state}: ${JSON.stringify(g)} is not in the prototype tree`);
    out.splice(i, 1);
  }
  return out;
};
// ...and the tips that explained how the exit check, the network line and Gateway work; the
// restart tip keeps only its consequence
const F01_TIP_GONE = new Set(['span#ov-prov', 'button#ov-refresh', 'section#ov-exit>div.exit-stage>div.exit-full>div.ip-block>div.ip-meta>span.ip-net', 'button#ov-gate']);
const F01_TIP_NEW = { 'button#ov-restart': '流量会短暂中断' };
const f01Tips = tips => tips.filter(([p]) => !F01_TIP_GONE.has(p)).map(([p, t]) => [p, F01_TIP_NEW[p] || t]);
// data-tips: the page sets no empty tip (the prototype's idle badge carried data-tip=""), and a
// stopped deck card's fallback order (one line, ellipsized on a narrow card) carries itself whole
// as its tip (T-P4-05a)
const tipsOf = (tips, stopped) => tips.filter(([path, tip]) => !(path === 'span#ov-prov' && tip === '')
  && !(stopped && /^div#ov-deck>button\.dcard\[tier=[a-z]+\]>span\.dcard-top>span\.dc-node$/.test(path)));
// starting: the exit card's 刷新 is there while the service starts (mapping_overview D4 / S-E4: a
// manual check is deferred until ready), the prototype hid it; the row is taken out before the diff
const STARTING_ADDED = row => row[1] === 'span' && row[2] === '刷新' && !row[3];
const withoutAdded = (tree, pred) => tree.filter(r => !pred(r));
function sameTree(want, got, what, extra = []) {
  const r = diffTrees(want, got, { allow: [...ALLOW, ...extra] });
  assert.ok(r.equal, `${what}: the visible text tree differs from the prototype\n${r.diff.slice(0, 20).join('\n')}`);
}

test('regression: running, stopped and starting show the prototype text and tips (sim mode)', { skip, timeout: 120000 }, async () => {
  const { context, page, errors } = await open();
  try {
    const states = await walkSim(page, { steps: SIM_STEPS });
    sameTree(withoutP1(withoutF04(withoutRoles(withoutF01(withoutGone(baselineSlice(readBaseline())), 'running')), 'running')), states.running.tree, 'running (T-P4-01 baseline)', [...DATA_05A, ...DATA_H2, ...DATA_F04, ...DATA_P1]);
    assert.deepEqual(states.running.tips, tipsNoRole(f01Tips(BEFORE.running.tips)), 'running: data-tips');
    for (const k of ['stopped', 'starting0', 'starting1']) {
      if (k === 'stopped') sameTree(withoutP1(withoutF04(withoutRoles(withoutF01(BEFORE[k].tree, k)), k)), states[k].tree, k, [...DATA_05A_STOPPED, ...DATA_F04, ...DATA_P1]);
      else sameTree(withoutP1(withoutF04(withoutRoles(withoutF01(BEFORE[k].tree, k)), k)), withoutAdded(states[k].tree, STARTING_ADDED), k, [...DATA_05A, ...DATA_05A_STOPPED, ...DATA_F04, ...DATA_P1]);
      assert.deepEqual(tipsOf(states[k].tips, k === 'stopped'), tipsNoRole(f01Tips(tipsOf(BEFORE[k].tips, false))), `${k}: data-tips`);
    }
    // the walk passes every state of the page (fallback, kill switch, gated mode, confirmation,
    // restart, refresh, cancelling); none may leave an error behind
    assert.deepEqual(errors(), []);
    assert.deepEqual(await page.evaluate(() => window.__fixtureClock.errors.slice()), []);
  } finally { await context.close(); }
});

test('language switch: pseudo leaves no CJK and only catalog attributes; back to zh_CN is the same tree', { skip, timeout: 120000 }, async () => {
  const { context, page, errors } = await open();
  try {
    const adv = ms => page.evaluate(m => window.__fixtureClock.advance(m), ms);
    const click = sel => page.evaluate(s => document.querySelector(s).click(), sel);
    const walk = [
      // the inline mode confirmation carries its own sentence
      ['running + confirmation', async () => { await click('#ov-mode [data-v="tun"]'); await adv(300); }],
      ['fallback', async () => { await click('.ov-confirm [data-a="no"]'); await adv(600); await page.evaluate(() => T.sim.scenario('fallback')); await adv(1800); }],
      ['kill switch off, gate note', async () => {
        await page.evaluate(() => { T.sim.scenario('recover'); }); await adv(3000);
        await page.evaluate(() => { T.store.set('ksEnabled', false); T.store.set('admin', false); }); await adv(50);
        await click('#ov-mode [data-v="tun"]'); await adv(100);
      }],
      ['stopped', async () => { await page.evaluate(() => { T.store.set('ksEnabled', true); T.store.set('admin', true); T.sim.stop(); }); await adv(3500); }],
      ['starting', async () => { await page.evaluate(() => T.sim.start()); await adv(1600); }],
    ];
    for (const [state, go] of walk) {
      await go(); await page.waitForTimeout(80); await adv(0);
      const r = await relabelRoundTrip(page, 'pseudo');
      assert.deepEqual(wordProblems(r.report, 'pseudo'), [], `${state}: words in pseudo`);
      assert.deepEqual(r.leaks, [], `${state}: leaks in the overview page (pseudo)`);
      assert.ok(r.report.attrs.length > 10, `${state}: attributes were read`);
      const d = diffTrees(r.tree0.tree, r.tree1.tree);
      assert.ok(d.equal, `${state}: zh_CN → pseudo → zh_CN changed the tree\n${d.diff.slice(0, 20).join('\n')}`);
      assert.deepEqual(r.tree1.tips, r.tree0.tips, `${state}: zh_CN → pseudo → zh_CN changed the data-tips`);
    }
    assert.deepEqual(errors(), []);
  } finally { await context.close(); }
});

test('language switch around a copy and a busy job: the buttons end in the new language', { skip, timeout: 120000 }, async () => {
  const { context, page, errors } = await open();
  try {
    const adv = async (ms, steps = 1) => { for (let i = 0; i < steps; i++) { await page.evaluate(m => window.__fixtureClock.advance(m), ms); await page.waitForTimeout(20); } };
    const CJK = /[㐀-鿿]/;
    const pseudo = s => typeof s === 'string' && s.includes('⟦') && !CJK.test(s);
    assert.equal(await page.evaluate(() => document.getElementById('app').getAttribute('data-run')), 'running');
    // copy after a switch: T.ui.copy puts back the strings it read when it was wired (the start
    // language); the page rewrites the bindings once the copied face is gone
    await page.evaluate(() => T.i18n.set('pseudo')); await adv(0, 3);
    for (const sel of ['#ov-copy4', '#ov-copy6']) {
      await page.evaluate(s => document.querySelector(s).click(), sel);
      await page.waitForTimeout(100); await adv(500, 5);
      const a = await page.evaluate(s => { const b = document.querySelector(s); return { aria: b.getAttribute('aria-label'), tip: b.getAttribute('data-tip'), done: b.classList.contains('is-done') }; }, sel);
      assert.equal(a.done, false, `${sel}: the copied face is gone`);
      assert.ok(pseudo(a.aria), `${sel}: aria-label after a copy is ${a.aria}`);
      assert.ok(pseudo(a.tip), `${sel}: data-tip after a copy is ${a.tip}`);
    }
    // a switch while a busy job runs: the idle face T.ui.busy writes back is the click's language
    for (const [sel, lang] of [['#ov-restart', 'zh_CN'], ['#ov-refresh', 'pseudo'], ['#ov-restart', 'pseudo']]) {
      const before = await page.evaluate(() => T.i18n.lang);
      await page.evaluate(s => document.querySelector(s).click(), sel); await adv(50, 2);
      assert.ok(await page.evaluate(s => document.querySelector(s).classList.contains('is-busy'), sel), `${sel}: busy`);
      await page.evaluate(l => T.i18n.set(l), lang); await adv(0, 2);
      await adv(500, 14);
      const lab = await page.evaluate(s => { const b = document.querySelector(s); return { text: b.querySelector('.lab').textContent, state: b.className }; }, sel);
      assert.doesNotMatch(lab.state, /is-(busy|done|fail)/, `${sel}: back to idle`);
      if (lang === 'pseudo') assert.ok(pseudo(lab.text), `${sel} (${before} → pseudo): label ${lab.text}`);
      else assert.ok(CJK.test(lab.text) && !lab.text.includes('⟦'), `${sel} (${before} → zh_CN): label ${lab.text}`);
    }
    assert.deepEqual(errors(), []);
  } finally { await context.close(); }
});

test('fixture mode, pseudo: the three ASCII scenarios show only catalog words outside translate="no"', { skip, timeout: 120000 }, async () => {
  for (const [scenario, run] of [['overview_i18n_running', 'running'], ['overview_i18n_stopped', 'stopped'], ['overview_i18n_starting', 'starting']]) {
    const { context, page, errors } = await open({ scenario, lang: 'pseudo' });
    try {
      assert.equal(await page.evaluate(() => window.__TESSERACT_DESIGN__ && window.__TESSERACT_DESIGN__.fixture), true, `${scenario}: fixture mode`);
      assert.equal(await page.evaluate(() => T.i18n.lang), 'pseudo');
      assert.equal(await page.evaluate(() => document.getElementById('app').getAttribute('data-run')), run, `${scenario}: run state`);
      const report = await page.evaluate(overviewWordsInPage);
      assert.deepEqual(wordProblems(report, 'pseudo'), [], scenario);
      // the matrix's own leak check (letters outside ⟦ ⟧, CSS content, attributes), page region only
      const checks = await page.evaluate(cellChecksInPage, { pseudo: true, whitelist: [...LEAK_WHITELIST, DEMO_BADGE] });
      assert.deepEqual(checks.leaks.filter(l => l.in_pages), [], `${scenario}: leaks in the overview page`);
      const tree = await page.evaluate(overviewTreeInPage);
      assert.ok(tree.tree.length > 20, `${scenario}: the page rendered`);
      assert.deepEqual(errors(), [], scenario);
    } finally { await context.close(); }
  }
});
