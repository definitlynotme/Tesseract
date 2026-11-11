// T-P5-10: core.js T.ui.busy (the fail face never takes its button out of its container) and
// T.ui.select (closed, the accessible name is the label and the current value), in the real design
// target (local Chrome through playwright-core, file://, fixture replay rt_connect_replay, no network).
//   - busy: the reserved width is capped at the room the container gives; a fail face whose whole
//     words do not fit shows the short core.busy.failed word (by code), the whole words become the
//     hover tip and the accessible description, and both go away with the fail face (the button's
//     own tip and description come back); where the words fit nothing changes; in a list row the
//     container is the fixed action cell (fix round 1: never spread over the row), and when even
//     the short word does not fit there, the short word without its glyph, then the glyph alone
//     (the short word its .sr-only name); the short word is reserved at the click (no widening at
//     the fail); the tip shows at once under a resting pointer or keyboard focus; T.ui.reserve is
//     capped too, element by element (a neighbour that already sticks out further hides nothing);
//   - select: aria-labelledby = the label (own aria-label, caller's aria-labelledby, <label for>,
//     .field > .field-lab), then .select-v; Chrome's accessible name has both and follows the value;
//     without a label the name is the value; option lang / translate stay on .select-v; with
//     motion on, the fading old value (aria-hidden) is not in the name.
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
const SCENARIO = () => readFileSync(join(UI, 'test', 'fixtures', 'scenarios', 'rt_connect_replay.json'), 'utf8');

let dir, browser, designUrl;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-core-busy-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  designUrl = pathToFileURL(join(dir, 'design', 'index.html')).href;
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

async function open(reducedMotion = 'reduce') {
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 800 }, reducedMotion });
  await context.route(/^(?!file:)/, r => r.abort());
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, SCENARIO());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(designUrl + '#logs');
  await page.waitForFunction(() => window.T && T.i18n && T.store.get('lang') === 'zh_CN' && T.router.current);
  await page.waitForTimeout(100);
  // a stage on top of the page: footers and rows of a given width
  await page.evaluate(() => {
    const s = document.createElement('div'); s.id = 'stage';
    s.style.cssText = 'position:fixed;left:20px;top:20px;width:900px;z-index:999;background:#fff';
    document.body.append(s);
  });
  return { context, page, errors };
}

/* in page: a sheet-like footer (flex, end-aligned, gap 8, padding 0 20px) of width w with 取消 and
   a primary button; returns nothing (the elements are #foot, #cancel, #go) */
const footer = (page, w) => page.evaluate(width => {
  const st = document.getElementById('stage'); st.replaceChildren();
  st.insertAdjacentHTML('beforeend', `<div id="foot" style="display:flex;justify-content:flex-end;gap:8px;padding:0 20px;width:${width}px;box-sizing:border-box;overflow:hidden">
    <button id="cancel" class="btn" type="button"><span class="lab">取消</span></button>
    <button id="go" class="btn btn-primary" type="button" data-tip="原提示" aria-describedby="own-d"><span class="lab">导入 3 个节点</span></button></div><span id="own-d" hidden>原描述</span>`);
}, w);
/* in page: run T.ui.busy on #go with a failing job; wait for the fail face; report the geometry */
const failOn = (page, { err, hold = 60000, fail } = {}) => page.evaluate(async ({ err: e, hold: h, fail: f }) => {
  const b = document.getElementById('go');
  const o = { failHold: h }; if (f !== undefined) o.fail = f;
  const run = T.ui.busy(b, () => new Promise((res, rej) => setTimeout(() => rej(Object.assign(new Error(e.message || 'x'), e)), 30)), o);
  const busyW = b.getBoundingClientRect().width;
  await run.catch(() => {});
  await new Promise(r => setTimeout(r, 450));
  const box = el => { const r = el.getBoundingClientRect(); return [r.left, r.right]; };
  const foot = document.getElementById('foot'), fr = foot.getBoundingClientRect(), cs = getComputedStyle(foot);
  const face = [...b.querySelectorAll('.lab .sw')].filter(s => !s.closest('[aria-hidden="true"]')).pop();
  const ids = (b.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
  return {
    fail: b.classList.contains('is-fail'), face: face ? face.textContent.trim() : '', tip: b.getAttribute('data-tip'),
    desc: ids.map(i => (document.getElementById(i) || {}).textContent || ''), ids,
    inner: [fr.left + parseFloat(cs.paddingLeft), fr.right - parseFloat(cs.paddingRight)], go: box(b), cancel: box(document.getElementById('cancel')),
    min: parseFloat(b.style.minWidth) || 0, why: b.querySelectorAll('.busy-why').length, busyW, failW: b.getBoundingClientRect().width,
  };
}, { err, hold, fail });
const inside = (r, [l, rr]) => r[0] >= l - 0.5 && r[1] <= rr + 0.5;

test('T.ui.busy: a fail face too wide for its footer shows the short word, the whole words as tip and description', { skip }, async () => {
  const { context, page, errors } = await open();
  await footer(page, 300);
  const want = 'The content you submitted is far too large to be processed in one go, so it was not run at all. Submit less and try again.';
  const r = await failOn(page, { err: { code: 'frame_too_large', message: want } });
  const short = await page.evaluate(() => T.t('core.busy.failed', { code: 'other' }));
  assert.equal(r.fail, true);
  assert.equal(r.face, short, 'the short word on the face');
  assert.equal(r.tip, want, 'the whole words are the hover tip');
  assert.deepEqual(r.desc, ['原描述', want], 'the whole words join the button\'s own description');
  assert.equal(r.why, 1);
  assert.ok(inside(r.go, r.inner), `the button stays in the footer: ${r.go} in ${r.inner}`);
  assert.ok(inside(r.cancel, r.inner), `取消 stays in the footer: ${r.cancel} in ${r.inner}`);
  assert.ok(!/…/.test(r.face));
  assert.ok(Math.abs(r.failW - r.busyW) <= 0.5, `the short word was reserved at the click: busy ${r.busyW}, fail ${r.failW}`);
  // Chrome: the name is the short word (the visible label), the description has the whole words
  const a = await ax(context, page, '#go');
  assert.equal(a.name, short);
  assert.equal(a.desc, `原描述 ${want}`);
  // back to idle: the button's own tip and description come back, the hidden note is gone
  const idle = await page.evaluate(async () => {
    const b = document.getElementById('go');
    await T.ui.busy(b, () => new Promise(res => setTimeout(() => res(1), 30)), { done: false });
    await new Promise(res => setTimeout(res, 300));
    return { tip: b.getAttribute('data-tip'), desc: b.getAttribute('aria-describedby'), why: b.querySelectorAll('.busy-why').length, fail: b.classList.contains('is-fail') };
  });
  assert.deepEqual(idle, { tip: '原提示', desc: 'own-d', why: 0, fail: false });
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.ui.busy: the short word follows the code (runtime_gone); a fail hold ends in the idle face without the note', { skip }, async () => {
  const { context, page, errors } = await open();
  await footer(page, 240);
  const long = 'A very long reason that cannot possibly fit into this narrow footer next to its cancel button at all.';
  const r = await failOn(page, { err: { code: 'runtime_gone' }, fail: long, hold: 1200 });
  const short = await page.evaluate(() => T.t('core.busy.failed', { code: 'runtime_gone' }));
  assert.equal(r.face, short);
  assert.equal(r.tip, long);
  assert.ok(inside(r.cancel, r.inner) && inside(r.go, r.inner), `both buttons in the footer: ${r.cancel} ${r.go} in ${r.inner}`);
  await page.waitForTimeout(1400);
  const after = await page.evaluate(() => { const b = document.getElementById('go'); return { tip: b.getAttribute('data-tip'), desc: b.getAttribute('aria-describedby'), why: b.querySelectorAll('.busy-why').length }; });
  assert.deepEqual(after, { tip: '原提示', desc: 'own-d', why: 0 });
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.ui.busy: a fail face that fits shows the whole words, no tip or description added (zh_CN behaviour)', { skip }, async () => {
  const { context, page, errors } = await open();
  await footer(page, 640);
  const want = await page.evaluate(() => T.t('errors.frame_too_large'));
  const r = await failOn(page, { err: { code: 'frame_too_large', message_key: 'errors.frame_too_large' } });
  assert.equal(r.face, want);
  assert.equal(r.tip, '原提示');
  assert.deepEqual(r.ids, ['own-d']);
  assert.equal(r.why, 0);
  assert.ok(r.min >= r.go[1] - r.go[0] - 1, 'the whole face is reserved');
  assert.ok(inside(r.cancel, r.inner) && inside(r.go, r.inner));
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.ui.busy: in a list row the container is the fixed action cell: short word, then no glyph, then the glyph alone', { skip }, async () => {
  const { context, page, errors } = await open();
  const r = await page.evaluate(async () => {
    const st = document.getElementById('stage'); st.replaceChildren();
    const mk = (width, cell, idle = '设为当前') => {
      const row = document.createElement('div');
      row.className = 'li'; row.style.cssText = `--cols: minmax(0,1fr) ${cell}px; width:${width}px; box-sizing:border-box`;
      row.innerHTML = '<span class="li-t">row</span><span class="li-act" style="opacity:1"><button class="btn btn-xs use" type="button"><span class="lab">${idle}</span></button><button class="icon-btn sm" type="button" aria-label="more">⋯</button></span>';
      st.append(row); return row;
    };
    const wide = mk(700, 104), narrow = mk(260, 104), roomy = mk(700, 160), tight = mk(700, 80, '用');
    const run = async row => {
      const b = row.querySelector('.use');
      T.ui.reserve(b, [b.textContent.trim()]);
      const act = row.querySelector('.li-act'), ar = act.getBoundingClientRect(), acs = getComputedStyle(act);
      const cell = [ar.left + parseFloat(acs.paddingLeft), ar.right - parseFloat(acs.paddingRight)];
      const job = T.ui.busy(b, () => Promise.reject(Object.assign(new Error('x'), { code: 'needs_route', message_key: 'errors.needs_route' })));
      const pre = b.getBoundingClientRect();
      await job.catch(() => {});
      await new Promise(res => setTimeout(res, 450));
      const br = b.getBoundingClientRect(), lab = b.querySelector('.lab');
      const face = [...b.querySelectorAll('.lab .sw')].filter(s => !s.closest('[aria-hidden="true"]')).pop();
      return { face: face.textContent.trim(), glyph: !!face.querySelector('svg'), alone: !!face.querySelector('.sr-only'), tip: b.getAttribute('data-tip'), b: [br.left, br.right], cell,
        grew: br.width - pre.width, clipped: lab.scrollWidth > lab.clientWidth + 0.5, more: (() => { const m = row.querySelector('.icon-btn').getBoundingClientRect(); return [m.left, m.right]; })() };
    };
    return { want: T.t('errors.needs_route'), short: T.t('core.busy.failed', { code: 'other' }), wide: await run(wide), narrow: await run(narrow), roomy: await run(roomy), tight: await run(tight) };
  });
  for (const k of ['wide', 'narrow', 'roomy', 'tight']) {
    const x = r[k];
    assert.equal(x.face, r.short, `${k}: the short word (shown or as the glyph's name), never the whole words over the row`);
    assert.equal(x.tip, r.want, `${k}: the whole words are the tip`);
    assert.ok(inside(x.b, x.cell), `${k}: the button stays in its action cell: ${x.b} in ${x.cell}`);
    assert.ok(inside(x.more, x.cell), `${k}: 更多 stays in the action cell: ${x.more} in ${x.cell}`);
    assert.ok(Math.abs(x.grew) <= 0.5, `${k}: the fail face keeps the width reserved at the click (${x.grew})`);
    assert.equal(x.clipped, false, `${k}: nothing is clipped`);
  }
  assert.deepEqual([r.roomy.glyph, r.roomy.alone], [true, false], 'a cell with room: the short word with its glyph');
  assert.deepEqual([r.wide.glyph, r.wide.alone], [false, false], 'a 104 px cell (zh_CN 设为当前): the short word without its glyph');
  assert.deepEqual([r.tight.glyph, r.tight.alone], [true, true], 'an 80 px cell: the glyph alone, the short word its name');
  const a = await ax(context, page, '.li:last-child .use');
  assert.equal(a.name, r.short, 'the glyph alone is still named by the short word');
  assert.ok(a.desc.includes(r.want), 'and described by the whole words');
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.ui.busy: the whole words show as a tip at once under the resting pointer and under keyboard focus', { skip }, async () => {
  const { context, page, errors } = await open();
  await footer(page, 300);
  const want = 'The content you submitted is far too large to be processed in one go, so it was not run at all. Submit less and try again.';
  await page.evaluate(w => {
    const b = document.getElementById('go');
    b.addEventListener('click', () => { T.ui.busy(b, () => new Promise((res, rej) => setTimeout(() => rej(Object.assign(new Error(w), { code: 'frame_too_large' })), 30)), { failHold: 900 }).catch(() => {}); });
  }, want);
  const tipNow = () => page.evaluate(() => { const t = document.querySelector('.tip.on'); return t ? t.textContent : null; });
  // the pointer: press, release, stay
  await page.hover('#go'); await page.waitForTimeout(600);
  assert.equal(await tipNow(), '原提示', 'the button\'s own tip before the press');
  await page.mouse.down(); await page.mouse.up();
  await page.waitForTimeout(150);
  assert.equal(await tipNow(), want, 'the whole words show at once under the resting pointer');
  await page.waitForTimeout(1100);
  assert.notEqual(await tipNow(), want, 'and go with the fail face');
  // the keyboard: focus with Tab, run with Enter, the pointer far away
  await page.mouse.move(5, 790);
  await page.evaluate(() => document.getElementById('cancel').focus());
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'go');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(150);
  assert.equal(await tipNow(), want, 'the whole words show at once under keyboard focus');
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.ui.reserve: the reserved width is capped at the room the container gives', { skip }, async () => {
  const { context, page, errors } = await open();
  await footer(page, 300);
  const r = await page.evaluate(() => {
    const b = document.getElementById('go');
    T.ui.reserve(b, ['导入 3 个节点', 'An extremely long busy label that would never fit in this footer at all, whatever happens']);
    const foot = document.getElementById('foot'), fr = foot.getBoundingClientRect(), cs = getComputedStyle(foot);
    const box = el => { const x = el.getBoundingClientRect(); return [x.left, x.right]; };
    return { min: parseFloat(b.style.minWidth) || 0, inner: [fr.left + parseFloat(cs.paddingLeft), fr.right - parseFloat(cs.paddingRight)], go: box(b), cancel: box(document.getElementById('cancel')) };
  });
  assert.ok(r.min > 0 && r.min < 260, `capped: ${r.min}`);
  assert.ok(inside(r.go, r.inner) && inside(r.cancel, r.inner), `both buttons in the footer: ${r.cancel} ${r.go} in ${r.inner}`);
  // element by element: a neighbour that already sticks out 60 px does not let the button stick out
  // up to 60 px of its own
  const e = await page.evaluate(() => {
    const foot = document.getElementById('foot'), c = document.getElementById('cancel'), b = document.getElementById('go');
    foot.style.justifyContent = 'flex-start'; foot.style.overflow = 'visible'; c.style.marginLeft = '-60px'; b.style.minWidth = '';
    T.ui.reserve(b, ['导入 3 个节点', 'An extremely long busy label that would never fit in this footer at all, whatever happens']);
    const fr = foot.getBoundingClientRect(), cs = getComputedStyle(foot), x = b.getBoundingClientRect();
    return { go: [x.left, x.right], right: fr.right - parseFloat(cs.paddingRight) };
  });
  assert.ok(e.go[1] <= e.right + 0.5, `the button stays in the footer beside a neighbour that sticks out: ${e.go} right edge ${e.right}`);
  assert.deepEqual(errors, []);
  await context.close();
});

/* Chrome's accessibility tree: name and description of the node at `sel` */
async function ax(context, page, sel) {
  const cdp = await context.newCDPSession(page);
  const { root } = await cdp.send('DOM.getDocument', { depth: 0 });
  const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: sel });
  const { nodes } = await cdp.send('Accessibility.getPartialAXTree', { nodeId, fetchRelatives: false });
  await cdp.detach();
  const n = nodes[0];
  return { name: n.name ? n.name.value : '', desc: n.description ? n.description.value : '', role: n.role ? n.role.value : '' };
}

test('T.ui.select: closed, the accessible name is the label and the current value', { skip }, async () => {
  const { context, page, errors } = await open();
  await page.evaluate(() => {
    const st = document.getElementById('stage'); st.replaceChildren();
    st.insertAdjacentHTML('beforeend', `
      <button id="s-aria" class="select" aria-label="界面语言"></button>
      <span id="ext-l">外部标签</span><button id="s-by" class="select" aria-labelledby="ext-l"></button>
      <label for="s-for">关联标签</label><button id="s-for" class="select"></button>
      <div class="field"><label class="field-lab">字段标签 <span class="opt-l">可选</span></label><button id="s-fld" class="select" type="button"></button><div class="field-msg"></div></div>
      <button id="s-none" class="select"></button>`);
    const opts = [{ value: 'a', label: '简体中文', lang: 'zh-CN', translate: 'no' }, { value: 'b', label: 'Deutsch', lang: 'de-DE', translate: 'no' }];
    window.__sel = ['s-aria', 's-by', 's-for', 's-fld', 's-none'].map(id => T.ui.select(document.getElementById(id), { value: 'a', options: opts }));
  });
  const attrs = await page.evaluate(() => ['s-aria', 's-by', 's-for', 's-fld', 's-none'].map(id => {
    const e = document.getElementById(id), v = e.querySelector('.select-v');
    return { id, by: e.getAttribute('aria-labelledby'), v: v.id, lang: v.getAttribute('lang'), tr: v.getAttribute('translate'), ownLang: e.getAttribute('lang') };
  }));
  const [aria, by, fr, fld, none] = attrs;
  assert.equal(aria.by, `s-aria ${aria.v}`, 'own aria-label: the button names itself first');
  assert.equal(by.by, `ext-l ${by.v}`, "the caller's aria-labelledby keeps its ids");
  assert.match(fr.by, new RegExp(`^\\S+ ${fr.v}$`));
  assert.match(fld.by, new RegExp(`^\\S+ ${fld.v}$`));
  assert.equal(none.by, null, 'no label: the name stays the value');
  for (const a of attrs) assert.deepEqual([a.lang, a.tr, a.ownLang], ['zh-CN', 'no', null], `option lang / translate on .select-v only (${a.id})`);
  assert.deepEqual(await ax(context, page, '#s-aria'), { name: '界面语言 简体中文', desc: '', role: 'button' });
  assert.equal((await ax(context, page, '#s-by')).name, '外部标签 简体中文');
  assert.equal((await ax(context, page, '#s-for')).name, '关联标签 简体中文');
  assert.equal((await ax(context, page, '#s-fld')).name, '字段标签 可选 简体中文');
  assert.equal((await ax(context, page, '#s-none')).name, '简体中文');
  // the name follows the value (the fading old value is aria-hidden) and the label's language
  await page.evaluate(() => { window.__sel[0].set('b', false); document.getElementById('s-aria').setAttribute('aria-label', 'Interface language'); });
  await page.waitForTimeout(50);
  assert.equal((await ax(context, page, '#s-aria')).name, 'Interface language Deutsch');
  await page.evaluate(() => window.__sel[0].set('a', true));
  await page.waitForTimeout(80);
  assert.equal((await ax(context, page, '#s-aria')).name, 'Interface language 简体中文');
  // a placeholder is the value while nothing is chosen
  await page.evaluate(() => window.__sel[3].set('zz', false));
  const ph = await page.evaluate(() => T.t('core.select.placeholder'));
  assert.equal((await ax(context, page, '#s-fld')).name, `字段标签 可选 ${ph}`);
  assert.deepEqual(errors, []);
  await context.close();
});

test('T.ui.select: with motion on, the fading old value is not in the name', { skip }, async () => {
  const { context, page, errors } = await open('no-preference');
  await page.evaluate(() => {
    const st = document.getElementById('stage'); st.replaceChildren();
    st.insertAdjacentHTML('beforeend', '<button id="s-anim" class="select" aria-label="测试标签"></button>');
    window.__s = T.ui.select(document.getElementById('s-anim'), { value: 'a', options: [{ value: 'a', label: '甲甲' }, { value: 'b', label: '乙乙' }] });
  });
  await page.waitForTimeout(400);
  const mid = await page.evaluate(() => { window.__s.set('b', true); return document.querySelector('#s-anim .select-v').innerHTML; });
  const a = await ax(context, page, '#s-anim');
  assert.match(mid, /aria-hidden="true"[^>]*>甲甲</, `the old value fades out aria-hidden: ${mid}`);
  assert.equal(a.name, '测试标签 乙乙');
  assert.deepEqual(errors, []);
  await context.close();
});
