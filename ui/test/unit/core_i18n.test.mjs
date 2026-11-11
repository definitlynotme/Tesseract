// T-P4-04a: the shell layer on the message catalog, in the real design target (local Chrome
// through playwright-core, file://, fixture replay rt_connect_replay, no network):
//   - status words by key (core.status.<key>, the new unknown / error / stale, last-known forms);
//   - command palette: groups are ids, named from the catalog, ordered service · go · mode ·
//     <page> · (others as given) · demo; a page group that still carries its own word joins the
//     block of the id with the same name;
//   - palette keywords: Chinese, pinyin and the fixed English id in zh_CN; in a second language
//     its own words and the English id, no pinyin;
//   - router: relocalize:'rerender' keeps what snapshotState() returned, the crumb and the window
//     title follow the language; the focus parameter reaches the page's enter(ctx);
//   - T.shellc.registerDraft returns a callable unregister; T.ui.saveBar.dirtyCount() is the
//     number the bar shows.
// The second language is a fake catalog registered as de_DE in the page ("DE " + the zh_CN text,
// own palette keywords), so the assertions do not depend on P5 translations.
// Skipped when Chrome is not installed (TESSERACT_UI_CHROME overrides the path).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { CHROME, UI, chromeAvailable } from '../fixtures/i18n/helpers.mjs';
import { compile } from '../../build/locales.mjs';
import { buildTarget, writeOutput } from '../../build.mjs';
import { scanCjkJs, scanCjkHtml } from '../../tools/scan.mjs';

const skip = !chromeAvailable() && 'local Chrome not found';
const ZH = join(UI, 'src', 'i18n', 'locales', 'zh_CN');
const zh = Object.assign({}, ...['shell', 'core', 'tier', 'mode', 'common'].map(ns => JSON.parse(readFileSync(join(ZH, `${ns}.json`), 'utf8'))));

// the fake second language: every shell / core / tier / mode / common message as "DE " + zh_CN,
// the palette keywords replaced by German-looking words (no pinyin, no Chinese)
const DE_KEYWORDS = {
  svc_toggle: 'dienst starten stoppen', svc_restart: 'neustart', svc_exit: 'ausgang pruefen', svc_copy: 'kopieren',
  go_overview: 'uebersicht', go_profiles: 'ebenen', go_logs: 'protokoll', go_settings: 'einstellungen',
  mode_system: 'netzwerkmodus systemproxy', mode_tun: 'netzwerkmodus global', mode_mixed: 'netzwerkmodus gemischt',
};
function deCatalog() {
  const out = {};
  for (const [id, v] of Object.entries(zh)) {
    const kw = id.match(/^shell\.palette\.keywords\.(.+)$/);
    const text = kw ? DE_KEYWORDS[kw[1]] : `DE ${v.m}`;
    out[id] = compile(text, `de_DE:${id}`);
  }
  return out;
}
const DE = deCatalog();
const SCENARIO = () => readFileSync(join(UI, 'test', 'fixtures', 'scenarios', 'rt_connect_replay.json'), 'utf8');

let dir, browser, designUrl;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-core-i18n-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  designUrl = pathToFileURL(join(dir, 'design', 'index.html')).href;
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

async function open(hash = '#overview') {
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
  await context.route(/^(?!file:)/, r => r.abort());
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, SCENARIO());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(designUrl + hash);
  await page.waitForFunction(() => window.T && T.i18n && T.store.get('lang') === 'zh_CN' && T.router.current);
  await page.waitForTimeout(100);
  return { context, page, errors };
}
const toDe = page => page.evaluate(async cat => { T.i18n.register('de_DE', cat); await T.i18n.set('de_DE'); return T.i18n.lang; }, DE);

// the palette: open it, type a query, read the group headers and the items (aria-label = label)
async function palette(page, q = '') {
  return page.evaluate(async query => {
    if (!T.cmd.isOpen) T.cmd.open();
    const input = document.querySelector('.cmdk-in input');
    input.value = query;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 30));
    const list = document.querySelector('.cmdk-list');
    const rows = [...list.children].filter(e => e.matches('.cmdk-g, .cmdk-item, .cmdk-empty'))
      .map(e => (e.matches('.cmdk-g') ? ['g', e.textContent] : e.matches('.cmdk-empty') ? ['empty', e.textContent] : ['i', e.getAttribute('aria-label')]));
    return { rows, groups: rows.filter(r => r[0] === 'g').map(r => r[1]), items: rows.filter(r => r[0] === 'i').map(r => r[1]), empty: rows.filter(r => r[0] === 'empty').map(r => r[1]) };
  }, q);
}

test('status words: by key only, from core.status.<key>; new keys; last-known forms; a second language', { skip }, async () => {
  const { context, page, errors } = await open();
  const read = () => page.evaluate(() => {
    // (T-P4-07: T.ui.status returns an element, no markup string)
    const w = (k, o) => { const d = document.createElement('div'); d.append(T.ui.status(k, o)); return [d.firstChild.className, d.textContent]; };
    return {
      ok: w('ok'), slow: w('slow'), down: w('down'), pending: w('pending'), recheck: w('recheck'), draft: w('draft'), current: w('current'), off: w('off'),
      unknown: w('unknown'), error: w('error'), stale: w('stale'),
      lastOk: w('ok', { last: true }), lastSlow: w('slow', { last: true }), lastDown: w('down', { last: true }), lastPending: w('pending', { last: true }),
      label: w('ok', { label: '2/3', chip: true }),
      word: w('较慢'), // a word is not a key any more (the reverse lookup ST_KEY is gone): shown as given
    };
  });
  const z = await read();
  assert.deepEqual(z.ok, ['st st-ok', '可用']);
  assert.deepEqual(z.slow, ['st st-slow', '较慢']);
  assert.deepEqual(z.down, ['st st-down', '不可用']);
  assert.deepEqual([z.pending[1], z.recheck[1], z.draft[1], z.current[1], z.off[1]], ['待检查', '待复检', '草稿', '当前', '未启用']);
  assert.deepEqual([z.unknown, z.error, z.stale], [['st st-unknown', '未知'], ['st st-error', '出错'], ['st st-stale', '未更新']]);
  assert.deepEqual([z.lastOk, z.lastSlow, z.lastDown], [['st st-ok st-last', '上次可用'], ['st st-slow st-last', '上次较慢'], ['st st-down st-last', '上次不可用']]);
  assert.deepEqual(z.lastPending, ['st st-pending', '待检查'], 'only ok / slow / down have a last-known form');
  assert.deepEqual(z.label, ['st st-ok chip-st', '2/3']);
  assert.deepEqual(z.word, ['st st-较慢', '较慢']);
  assert.equal(await toDe(page), 'de_DE');
  const d = await read();
  assert.deepEqual(d.slow, ['st st-slow', 'DE 较慢']);
  assert.deepEqual(d.unknown, ['st st-unknown', 'DE 未知']);
  assert.deepEqual(d.lastDown, ['st st-down st-last', 'DE 上次不可用']);
  assert.deepEqual(errors, []);
  await context.close();
});

test('palette groups: ids named from the catalog; service, go, mode, pages, others, demo last; same-name blocks merge', { skip }, async () => {
  const { context, page, errors } = await open();
  await page.evaluate(() => {
    const noop = () => {};
    T.cmd.register([
      { id: 't04a-other', group: 'Zeta custom', label: 'zeta other group', run: noop },
      { id: 't04a-set', group: 'settings', label: 'zeta settings cmd', run: noop },
      { id: 't04a-logs', group: 'logs', label: 'zeta logs cmd', run: noop },
      { id: 't04a-ov', group: 'overview', label: 'zeta overview cmd', run: noop },
      { id: 't04a-prof', group: 'profiles', label: 'zeta profiles cmd', run: noop },
      // a page that still registers its own word: the block of the id with that name
      { id: 't04a-go-word', group: '前往', order: 9, label: 'zeta go by word', run: noop },
    ]);
  });
  const ORDER_ZH = ['服务', '前往', '切换接管方式', '总览', '层级', '运行日志', '配置', 'Zeta custom', '演示（原型）'];
  const z = await palette(page);
  const seen = z.groups.filter(g => ORDER_ZH.includes(g));
  assert.deepEqual(seen, ORDER_ZH, `groups in order, each once: ${JSON.stringify(z.groups)}`);
  assert.equal(z.groups[z.groups.length - 1], '演示（原型）', 'demo is the last block');
  const goAt = z.rows.findIndex(r => r[0] === 'g' && r[1] === '前往');
  const modeAt = z.rows.findIndex(r => r[0] === 'g' && r[1] === '切换接管方式');
  const byWord = z.rows.findIndex(r => r[0] === 'i' && r[1] === 'zeta go by word');
  assert.ok(goAt < byWord && byWord < modeAt, 'the command registered with the word 前往 sits in the go block');
  assert.deepEqual(z.rows.slice(goAt + 1, goAt + 5).map(r => r[1]), ['总览', '层级', '运行日志', '配置']);
  // the second language: the same order, the names from the fake catalog; the word 前往 is no
  // longer a known name there, so its command forms its own block among the others
  await toDe(page);
  const d = await palette(page);
  const ORDER_DE = ['DE 服务', 'DE 前往', 'DE 切换接管方式', 'DE 总览', 'DE 层级', 'DE 运行日志', 'DE 配置'];
  assert.deepEqual(d.groups.filter(g => ORDER_DE.includes(g)), ORDER_DE, JSON.stringify(d.groups));
  // (T-P4-07: the demo block is the design target's design/demo.js, its words are not catalog
  // words: the same in every language, still last)
  assert.equal(d.groups[d.groups.length - 1], '演示（原型）');
  assert.ok(d.groups.indexOf('前往') > d.groups.indexOf('DE 配置') && d.groups.indexOf('前往') < d.groups.indexOf('演示（原型）'));
  assert.ok(d.items.includes('DE 重启服务') && d.items.includes('DE 系统代理'), JSON.stringify(d.items));
  const empty = await palette(page, 'qqqqzzzz');
  assert.deepEqual(empty.empty, ['DE 没有匹配的命令']);
  assert.deepEqual(errors, []);
  await context.close();
});

test('palette keywords: Chinese, pinyin and English ids in zh_CN; the language\'s own words and English ids elsewhere', { skip }, async () => {
  const { context, page, errors } = await open();
  const first = async q => (await palette(page, q)).items[0];
  assert.equal(await first('chongqi'), '重启服务', 'pinyin');
  assert.equal(await first('重启'), '重启服务', 'Chinese keyword (and label)');
  // words that are in the keywords only (not in any label): the Chinese keyword path on its own
  assert.equal(await first('首页'), '总览', 'Chinese keyword only: 首页');
  assert.equal(await first('节点'), '层级', 'Chinese keyword only: 节点');
  assert.equal(await first('设置'), '配置', 'Chinese keyword only: 设置');
  assert.equal(await first('restart'), '重启服务', 'fixed English id');
  assert.equal(await first('quanju'), '虚拟网卡模式（TUN）', 'the old word 全局 still finds the TUN command');
  assert.equal(await first('peizhi'), '配置');
  assert.equal(await first('settings'), '配置');
  await toDe(page);
  assert.equal(await first('neustart'), 'DE 重启服务', 'the language\'s own keyword');
  assert.equal(await first('restart'), 'DE 重启服务', 'the English id works in every language');
  assert.equal(await first('global'), 'DE 虚拟网卡模式（TUN）');
  const pinyin = await palette(page, 'chongqi');
  assert.ok(!pinyin.items.includes('DE 重启服务'), 'pinyin belongs to the zh_CN catalog only');
  assert.deepEqual(errors, []);
  await context.close();
});

test('router: relocalize rerender keeps the snapshot; crumb and window title follow; focus reaches enter(ctx)', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => {
    window.__log = [];
    const mk = id => T.page(id, {
      relocalize: 'rerender',
      render(root, ctx) { window.__log.push(`${id}:render:${T.i18n.lang}:${!!ctx.relocalized}`); root.innerHTML = `<input class="draft-${id}"><p class="w">${T.t('core.select.placeholder')}</p>`; },
      enter(ctx) { window.__log.push(`${id}:enter:${ctx.params.focus || '-'}:${!!ctx.relocalized}`); window.__nav = ctx.navParams ? { focus: ctx.navParams.focus, frozen: Object.isFrozen(ctx.navParams) } : null; },
      leave() { window.__log.push(`${id}:leave`); },
      snapshotState() { const v = document.querySelector(`.draft-${id}`).value; window.__log.push(`${id}:snapshot:${v}`); return { v }; },
      restoreState(s) { document.querySelector(`.draft-${id}`).value = s.v; window.__log.push(`${id}:restore:${s.v}:${T.router.current}`); },
    });
    mk('t04a_a'); mk('t04a_b');
  });
  // focus: T.go params and the data-go-focus markup both reach enter(ctx).params
  await page.evaluate(() => { T.go('t04a_b', { focus: 'update' }); T.go('t04a_a', { focus: 'alerts' }); });
  await page.evaluate(() => {
    const b = document.createElement('button'); b.setAttribute('data-go', 't04a_a'); b.setAttribute('data-go-focus', 'killswitch');
    document.body.append(b); b.click(); b.remove();
  });
  await page.evaluate(() => { document.querySelector('.draft-t04a_a').value = 'kept draft'; document.querySelector('.draft-t04a_b').value = 'b draft'; window.__log.length = 0; });
  const zhTitle = await page.evaluate(() => ({ title: document.title, crumb: document.querySelector('#crumb span:not(.leaving)').textContent }));
  assert.deepEqual(zhTitle, { title: 'Tesseract · t04a_a', crumb: 't04a_a' }, 'a page that is not a shell page keeps its id / def.title');
  await toDe(page);
  const r = await page.evaluate(() => ({ log: window.__log.slice(), a: document.querySelector('.draft-t04a_a').value, w: document.querySelector('.page-t04a_a .w').textContent, nav: window.__nav }));
  assert.deepEqual(r.log, ['t04a_a:snapshot:kept draft', 't04a_a:leave', 't04a_a:render:de_DE:true', 't04a_a:enter:-:true', 't04a_a:restore:kept draft:t04a_a'],
    'the page shown: snapshot → leave → render → enter (no one-time params: a re-render is not a navigation) → restore');
  assert.deepEqual(r.nav, { focus: 'killswitch', frozen: true }, 'the params of the last navigation are kept read-only in ctx.navParams');
  assert.equal(r.a, 'kept draft');
  assert.equal(r.w, 'DE 请选择');
  // the page not shown is redone on its next visit, with its state kept
  await page.evaluate(() => { window.__log.length = 0; T.go('t04a_b', { focus: 'alerts' }); });
  const b = await page.evaluate(() => ({ log: window.__log.slice(), v: document.querySelector('.draft-t04a_b').value }));
  assert.deepEqual(b.log, ['t04a_a:leave', 't04a_b:snapshot:b draft', 't04a_b:render:de_DE:true', 't04a_b:enter:alerts:false', 't04a_b:restore:b draft:t04a_b'],
    'the page redone on its next visit: the same order (render → enter → restore), restored once it is current');
  assert.equal(b.v, 'b draft');
  // the shell pages: crumb, window title and the page section's name from shell.page.<id>.title
  await page.evaluate(() => T.go('logs'));
  await page.waitForTimeout(400);
  const t = await page.evaluate(() => ({ title: document.title, crumb: document.querySelector('#crumb span:not(.leaving)').textContent, aria: document.querySelector('section.page-logs').getAttribute('aria-label'), nav: document.querySelector('.nav-item[data-nav="logs"] [data-i18n]').textContent }));
  assert.deepEqual(t, { title: 'DE Tesseract · DE 运行日志', crumb: 'DE 运行日志', aria: 'DE 运行日志', nav: 'DE 运行日志' });
  assert.deepEqual(errors, []);
  await context.close();
});

test('cross-page interfaces: registerDraft returns a callable unregister; dirtyCount is what the save bar shows', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  const r = await page.evaluate(async () => {
    const out = {};
    const off = T.shellc.registerDraft(() => 2);
    out.offType = typeof off;
    out.offCall = (() => { try { off(); off(); return 'ok'; } catch (e) { return e.message; } })();
    out.bad = (() => { try { T.shellc.registerDraft('x'); return 'no throw'; } catch (e) { return e.name; } })();
    const shown = () => { const s = [...document.querySelectorAll('.savebar .sb-count .sw')].filter(x => x.getAttribute('aria-hidden') !== 'true').pop(); return s ? s.textContent : null; };
    const wait = ms => new Promise(res => setTimeout(res, ms));
    out.none = T.ui.saveBar.dirtyCount();
    let resolve;
    const bar = T.ui.saveBar({ count: 3, effect: 'reload', onSave: () => new Promise(res => { resolve = res; }), onUndo: () => {} });
    await wait(50);
    out.dirty = [T.ui.saveBar.dirtyCount(), shown()];
    bar.update({ count: 5 }); await wait(400);
    out.five = [T.ui.saveBar.dirtyCount(), shown()];
    const p = bar.save(); await wait(50);
    out.saving = [T.ui.saveBar.dirtyCount(), shown()];
    resolve(true); await p; await wait(50);
    out.saved = [T.ui.saveBar.dirtyCount(), shown()];
    const bar2 = T.ui.saveBar({ count: 1, effect: 'instant', onSave: () => Promise.reject(new Error('')), onUndo: () => {} });
    await bar2.save(); await wait(50);
    out.failed = [T.ui.saveBar.dirtyCount(), shown(), document.querySelector('.savebar .sb-save .lab').textContent];
    bar2.hide(true);
    out.hidden = T.ui.saveBar.dirtyCount();
    return out;
  });
  assert.equal(r.offType, 'function');
  assert.equal(r.offCall, 'ok');
  assert.equal(r.bad, 'TypeError');
  assert.equal(r.none, 0);
  assert.deepEqual(r.dirty, [3, '3 项更改未保存']);
  assert.deepEqual(r.five, [5, '5 项更改未保存']);
  assert.deepEqual(r.saving, [5, '正在保存 5 项更改'], 'still unsaved while saving');
  assert.deepEqual(r.saved, [0, '已保存']);
  assert.deepEqual(r.failed, [1, '保存失败', '重试'], 'a failed save is still unsaved');
  assert.equal(r.hidden, 0);
  assert.deepEqual(errors, []);
  await context.close();
});

test('the save bar, the static shell words and the brand title follow the language in place', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => { T.ui.saveBar({ count: 2, effect: 'restart', onSave: () => Promise.resolve(true), onUndo: () => {} }); });
  await page.waitForTimeout(100);
  await toDe(page);
  await page.waitForTimeout(100);
  const r = await page.evaluate(() => {
    const vis = sel => { const s = [...document.querySelectorAll(sel + ' .sw')].filter(x => x.getAttribute('aria-hidden') !== 'true').pop(); return s ? s.textContent : null; };
    return {
      count: vis('.savebar .sb-count'), eff: vis('.savebar .sb-eff'), undo: document.querySelector('.savebar .sb-undo .lab').textContent,
      save: vis('.savebar .sb-save .lab'), aria: document.querySelector('.savebar').getAttribute('aria-label'),
      side: document.querySelector('aside.side').getAttribute('aria-label'), cmd: document.querySelector('#cmd-open [data-i18n]').textContent,
      brand: document.querySelector('.brand').title, foot: vis('.foot-upd'), footAria: document.querySelector('.foot-upd').getAttribute('aria-label'),
    };
  });
  assert.deepEqual(r, {
    count: 'DE 2 项更改未保存', eff: 'DE 保存后会重启代理服务，流量中断约 1–2 秒', undo: 'DE 撤销', save: 'DE 保存', aria: 'DE 未保存的更改',
    side: 'DE 侧边栏', cmd: 'DE 搜索与命令', brand: 'DE Tesseract · 运行中', foot: 'DE 检查更新', footAria: 'DE DE 检查更新，前往配置 · 应用', // T-P4-06: no update.state in the fixture, nothing claimed
  });
  assert.deepEqual(errors, []);
  await context.close();
});

test('no Chinese literal left in the shell sources; the @i18n-pending marks are gone', () => {
  const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/;
  for (const f of ['src/core/core.js', 'src/core/app.js', 'src/index.html', 'src/data/static.js']) {
    const s = readFileSync(join(UI, f), 'utf8');
    assert.ok(!/@i18n-pending/.test(s.split('\n')[0]), `${f}: first line still @i18n-pending`);
    if (f.endsWith('.html')) assert.ok(!CJK.test(s), `${f}: CJK text`);
    // string, template and regex literals (the cjk scan's own rule: comments are not text)
    else assert.deepEqual(scanCjkJs(s, f).map(x => `${x.line}: ${x.text}`), [], `${f}: CJK literal`);
    if (f.endsWith('.html')) assert.deepEqual(scanCjkHtml(s, f), [], `${f}: CJK text or attribute`);
  }
  // every zh_CN message of the shell namespaces has its _desc
  for (const ns of ['shell', 'core', 'tier', 'mode']) {
    const cat = JSON.parse(readFileSync(join(ZH, `${ns}.json`), 'utf8'));
    for (const [id, v] of Object.entries(cat)) {
      assert.ok(id.startsWith(`${ns}.`), id);
      assert.ok(typeof v.m === 'string' && v.m && typeof v._desc === 'string' && v._desc, `${id}: m and _desc`);
      assert.ok(!('_max' in v), `${id}: no _max`);
    }
  }
  assert.ok(readdirSync(ZH).includes('shell.json'));
});

/* ---- fix round 1 (T-P4-04a review) ---- */

test('router: a language re-render does not replay the one-time focus landing; the restored scroll stays', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => {
    window.__enter = [];
    T.page('t04a_land', {
      relocalize: 'rerender',
      render(root) { root.innerHTML = '<div class="sc" style="height:200px;overflow:auto"><div style="height:4000px"></div></div>'; },
      // the landing as a page does it (settings.js focusTo): in the next frame
      enter(ctx) { window.__enter.push(JSON.stringify(ctx.params)); if (ctx.params.focus) requestAnimationFrame(() => { ctx.root.querySelector('.sc').scrollTop = 3000; }); },
      snapshotState() { return { top: document.querySelector('.page-t04a_land .sc').scrollTop }; },
      restoreState(s) { document.querySelector('.page-t04a_land .sc').scrollTop = s.top; },
    });
  });
  const frames = () => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  await page.evaluate(() => T.go('t04a_land', { focus: 'update' }));
  await frames();
  assert.equal(await page.evaluate(() => document.querySelector('.page-t04a_land .sc').scrollTop), 3000, 'the navigation lands');
  await page.evaluate(() => { document.querySelector('.page-t04a_land .sc').scrollTop = 1200; });
  await toDe(page);
  await frames(); await page.waitForTimeout(50);
  const r = await page.evaluate(() => ({ top: document.querySelector('.page-t04a_land .sc').scrollTop, enter: window.__enter.slice() }));
  assert.equal(r.top, 1200, 'the scroll the user left is restored, not the landing of the last navigation');
  assert.deepEqual(r.enter, ['{"focus":"update"}', '{}']);
  assert.deepEqual(errors, []);
  await context.close();
});

test('router: a same-page T.go after a language re-render is a navigation (no relocalized, no stale navParams)', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => {
    window.__log = [];
    T.page('t04a_sp', {
      relocalize: 'rerender',
      render(root) { root.innerHTML = '<p>sp</p>'; },
      enter(ctx) { window.__log.push([ctx.params.focus || '-', !!ctx.relocalized, ctx.navParams ? ctx.navParams.focus : null, !!ctx.again]); },
      snapshotState() { return 1; }, restoreState() {},
    });
    T.go('t04a_sp', { focus: 'update' });
  });
  await toDe(page);
  await page.evaluate(() => T.go('t04a_sp', { focus: 'alerts' }));
  await page.evaluate(() => T.i18n.set('zh_CN'));
  const log = await page.evaluate(() => window.__log.slice());
  assert.deepEqual(log, [
    ['update', false, null, false],
    ['-', true, 'update', false],
    ['alerts', false, null, true],
    ['-', true, 'alerts', false],
  ], 'navigate · re-render · same-page navigate · re-render: navParams is always the last navigation\'s params');
  assert.deepEqual(errors, []);
  await context.close();
});

test('router: a throwing snapshotState / restoreState neither breaks the language switch nor leaves T.go half way', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => {
    window.__log = [];
    // titleId: the crumb follows the language, so a broken switch would show
    T.page('t04a_boom', {
      titleId: 'shell.page.logs.title', relocalize: 'rerender',
      render(root, ctx) { window.__log.push(`boom:render:${!!ctx.relocalized}`); root.innerHTML = '<p>boom</p>'; },
      enter() { window.__log.push('boom:enter'); },
      snapshotState() { throw new Error('snap boom'); },
      restoreState() { throw new Error('restore boom'); },
    });
    T.page('t04a_after', { relocalize: 'rerender', render(root, ctx) { window.__log.push(`after:render:${!!ctx.relocalized}`); }, snapshotState() { return 1; }, restoreState() {} });
    T.go('t04a_after'); T.go('t04a_boom'); T.go('logs');
  });
  await toDe(page);
  // the deferred re-render inside T.go: the hooks throw, the navigation completes
  const go = await page.evaluate(() => {
    window.__log.length = 0;
    let threw = null;
    try { T.go('t04a_boom'); } catch (e) { threw = e.message; }
    return { threw, current: T.router.current, hidden: document.querySelector('.page-t04a_boom').hidden, log: window.__log.slice() };
  });
  await page.waitForTimeout(400);
  assert.deepEqual(go, { threw: null, current: 't04a_boom', hidden: false, log: ['boom:render:true', 'boom:enter'] });
  // the page shown: its hooks throw inside i18n:change; the crumb, the title and the other pages still follow
  await page.evaluate(async () => { window.__log.length = 0; await T.i18n.set('zh_CN'); });
  await page.waitForTimeout(400);
  const sw = await page.evaluate(() => ({ crumb: document.querySelector('#crumb span:not(.leaving)').textContent, title: document.title, log: window.__log.slice() }));
  assert.deepEqual(sw, { crumb: '运行日志', title: 'Tesseract · 运行日志', log: ['boom:render:true', 'boom:enter'] });
  await page.evaluate(() => { window.__log.length = 0; T.go('t04a_after'); });
  assert.deepEqual(await page.evaluate(() => window.__log.slice()), ['after:render:true'], 'a page after the throwing one in the loop is still marked for its re-render');
  await page.waitForTimeout(50);
  assert.deepEqual([...errors].sort(), ['restore boom', 'restore boom', 'snap boom', 'snap boom'], 'each exception is rethrown asynchronously');
  await context.close();
});

test('router: focus inside the page shown is not lost to <body> by a language re-render', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => {
    T.page('t04a_focus', { relocalize: 'rerender', render(root) { root.innerHTML = '<input class="f-in">'; }, snapshotState() { return null; }, restoreState() {} });
    T.go('t04a_focus');
  });
  await page.evaluate(() => document.querySelector('.page-t04a_focus .f-in').focus());
  await toDe(page);
  const r = await page.evaluate(() => { const ae = document.activeElement; const sec = document.querySelector('.page-t04a_focus'); return { body: ae === document.body, inPage: sec.contains(ae), root: ae === sec }; });
  assert.deepEqual(r, { body: false, inPage: true, root: true });
  assert.deepEqual(errors, []);
  await context.close();
});

test('rt errors: the save bar and T.ui.busy say the catalog text of message_key (zh_CN)', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  const r = await page.evaluate(async () => {
    const wait = ms => new Promise(res => setTimeout(res, ms));
    const rtErr = () => Object.assign(new Error('internal'), { code: 'internal', message_key: 'errors.internal' });
    const want = T.t('errors.internal');
    const bar = T.ui.saveBar({ count: 1, effect: 'instant', onSave: () => Promise.reject(rtErr()), onUndo: () => {} });
    await bar.save(); await wait(100);
    const eff = [...document.querySelectorAll('.savebar .sb-eff .sw')].filter(x => x.getAttribute('aria-hidden') !== 'true').pop();
    const btn = document.createElement('button'); btn.className = 'icon-btn'; btn.setAttribute('aria-label', 'x'); document.body.append(btn);
    await T.ui.busy(btn, () => Promise.reject(rtErr()), { stay: true }).catch(() => {});
    await wait(50);
    return { want, eff: eff && eff.textContent, busy: btn.getAttribute('aria-label') };
  });
  assert.ok(r.want && r.want !== 'errors.internal' && /[一-鿿]/.test(r.want), r.want);
  assert.equal(r.eff, r.want);
  assert.equal(r.busy, r.want);
  assert.deepEqual(errors, []);
  await context.close();
});

test('runtime ids the static metadata does not know are shown raw, never as a message id; notice shell words relabel in place', { skip }, async () => {
  const { context, page, errors } = await open('#logs');
  await page.evaluate(() => {
    T.store.set('fallback', true);
    T.emit('rt:tier', { kind: 'fallback', carrier: 'zz_new_tier', from: 'vanguard', instant: false }); // T-P4-06: rt:* only
  });
  await page.waitForTimeout(300);
  const b = await page.evaluate(() => document.querySelector('#banners .banner[data-id="fallback"]').textContent);
  assert.ok(b.includes('zz_new_tier') && !/tier\.zz_new_tier/.test(b), b);
  await page.evaluate(() => { T.dismiss('fallback'); T.emit('rt:mode', { mode: 'zz_mode', busy: false, instant: false }); });
  await page.waitForTimeout(200);
  const t = await page.evaluate(() => document.querySelector('#toast-slot .toast:last-child .toast-msg').textContent);
  assert.ok(t.includes('zz_mode') && !/mode\.zz_mode/.test(t), t);
  // a toast's close button and a banner's level word / default title follow the language in place
  await page.evaluate(() => T.notify({ kind: 'warning', id: 't04a_b', message: 'm' }));
  await page.waitForTimeout(200);
  await toDe(page);
  const r = await page.evaluate(() => ({
    toastX: document.querySelector('#toast-slot .toast:last-child > .icon-btn').getAttribute('aria-label'),
    bannerX: document.querySelector('#banners .banner[data-id="t04a_b"] > .icon-btn').getAttribute('data-tip'),
    sev: document.querySelector('#banners .banner[data-id="t04a_b"] .banner-sev').textContent,
    title: document.querySelector('#banners .banner[data-id="t04a_b"] .banner-title > span').textContent,
  }));
  assert.deepEqual(r, { toastX: 'DE 关闭通知', bannerX: 'DE 关闭通知', sev: 'DE 警告', title: 'DE 需要注意' });
  assert.deepEqual(errors, []);
  await context.close();
});

test('the app target carries no demo hooks of the shell (window.demo, the demo palette group, demo-fallback)', async () => {
  const { files, manifest } = await buildTarget({ target: 'app' });
  const main = manifest.files.find(f => /^app\.[A-Z0-9]+\.js$/.test(f.path));
  assert.ok(main, 'the app main script');
  const js = new TextDecoder().decode(files.get(main.path));
  for (const re of [/window\.demo\b/, /shell\.demo\./, /demo-fallback/]) assert.ok(!re.test(js), `${re} in the app main script`);
});
