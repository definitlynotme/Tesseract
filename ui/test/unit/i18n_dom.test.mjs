// T-P4-03: the i18n runtime in a real browser (local Chrome through playwright-core, file://,
// no network): T.tx tag rendering and injection safety, T.ti + relabel on a language switch,
// real <script> language loading (success, no register, missing file), boot from
// navigator.language, T.html with Node interpolations, --i18n-trace marks.
// Skipped when Chrome is not installed (TESSERACT_UI_CHROME overrides the path).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { CHROME, UI, bundleIife, chromeAvailable } from '../fixtures/i18n/helpers.mjs';
import { compile, localeScript } from '../../build/locales.mjs';
import { buildTarget, writeOutput } from '../../build.mjs';

const skip = !chromeAvailable() && 'local Chrome not found';
const CAT = {
  zh_CN: {
    'a.title': '标题',
    'a.rich': '按 <kbd>Ctrl K</kbd> 打开 <b>{name}</b>，<code>{c}</code> <mono>{m}</mono> <em>注意</em>',
    'a.slot': '已回退到 <tier>{t}</tier>',
    'a.count': '{n, plural, other {# 项}}',
    'a.only_zh': '仅中文',
    'a.label': '关闭{what}',
  },
  en_US: {
    'a.title': 'Title',
    'a.rich': 'Press <kbd>Ctrl K</kbd> to open <b>{name}</b>, <code>{c}</code> <mono>{m}</mono> <em>note</em>',
    'a.slot': 'Fell back to <tier>{t}</tier>',
    'a.count': '{n, plural, one {# item} other {# items}}',
    'a.label': 'Close {what}',
  },
  de_DE: { 'a.title': 'Titel', 'a.label': '{what} schließen' },
};
const compiled = lang => Object.fromEntries(Object.entries(CAT[lang]).map(([id, s]) => [id, compile(s, `${lang}:${id}`)]));
const FILES = { zh_CN: 'locale-zh_CN.T1.js', en_US: 'locale-en_US.T2.js', de_DE: 'locale-de_DE.T3.js', fr_FR: 'locale-fr_FR.T4.js', ja_JP: 'locale-ja_JP.MISSING.js' };

let dir, browser, chromium;
async function writePage(name, define) {
  const entry = "import '../core/core.js';\nimport './i18n.js';\n";
  writeFileSync(join(dir, `${name}.js`), await bundleIife(entry, { __LOCALES__: JSON.stringify(FILES), ...define }));
  writeFileSync(join(dir, `${name}.html`), `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>i18n</title></head>
<body><div id="app"></div><script src="${FILES.zh_CN}"></script><script src="${name}.js"></script></body></html>\n`);
  return pathToFileURL(join(dir, `${name}.html`)).href;
}
async function open(url, locale = 'zh-CN') {
  const context = await browser.newContext({ locale, viewport: { width: 800, height: 600 } });
  await context.route(/^(?!file:)/, r => r.abort());
  const page = await context.newPage();
  const logs = [];
  page.on('console', m => logs.push([m.type(), m.text()]));
  page.on('pageerror', e => logs.push(['pageerror', e.message]));
  await page.goto(url);
  await page.waitForFunction(() => window.T && window.T.i18n);
  return { page, context, logs };
}

let mainUrl, traceUrl, designUrl;
before(async () => {
  if (skip) return;
  ({ chromium } = await import('playwright-core'));
  dir = mkdtempSync(join(tmpdir(), 'tesseract-i18n-dom-'));
  for (const lang of ['zh_CN', 'en_US', 'de_DE']) writeFileSync(join(dir, FILES[lang]), localeScript(lang, compiled(lang)));
  writeFileSync(join(dir, FILES.fr_FR), 'window.__frLoaded = true;\n'); // loads, never registers
  mainUrl = await writePage('main', { __I18N_DEV__: 'true' });
  traceUrl = await writePage('trace', { __I18N_DEV__: 'true', __I18N_TRACE__: 'true' });
  // the real design target (fixture replay through FakeTransport: hello arrives at connect)
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  designUrl = pathToFileURL(join(dir, 'design', 'index.html')).href;
  browser =await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

test('T.tx: whitelisted tags become elements; parameters are text nodes only (no injection)', { skip }, async () => {
  const { page, context, logs } = await open(mainUrl);
  const r = await page.evaluate(() => {
    const host = document.getElementById('app');
    const frag = T.tx('a.rich', { name: '<script>window.__pwned=1</script>', c: '<img src=x onerror="window.__pwned=2">', m: '"><svg onload="window.__pwned=3">' });
    host.appendChild(frag);
    return {
      kbd: host.querySelector('kbd')?.textContent, b: host.querySelector('b')?.textContent,
      code: host.querySelector('code.tech')?.textContent, mono: host.querySelector('span.mono')?.textContent, em: host.querySelector('em')?.textContent,
      scripts: host.querySelectorAll('script').length, imgs: host.querySelectorAll('img,svg').length,
      attrs: [...host.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.name)),
      text: host.textContent,
    };
  });
  await page.waitForTimeout(50);
  assert.equal(await page.evaluate(() => window.__pwned), undefined);
  assert.equal(r.kbd, 'Ctrl K');
  assert.equal(r.b, '<script>window.__pwned=1</script>');
  assert.equal(r.code, '<img src=x onerror="window.__pwned=2">');
  assert.equal(r.mono, '"><svg onload="window.__pwned=3">');
  assert.equal(r.em, '注意');
  assert.equal(r.scripts, 0);
  assert.equal(r.imgs, 0);
  assert.deepEqual([...new Set(r.attrs)].sort(), ['class'], 'no attribute but the built-in class names (code.tech, span.mono)');
  assert.ok(!logs.some(([t]) => t === 'pageerror'), JSON.stringify(logs));
  await context.close();
});

test('slots wrap translated words; T.t strips tags and warns in development builds', { skip }, async () => {
  const { page, context, logs } = await open(mainUrl);
  const r = await page.evaluate(() => {
    const f = T.tx('a.slot', { t: 'Sentinel' }, { tier: kids => { const a = document.createElement('a'); a.className = 'tierlink'; a.appendChild(kids); return a; } });
    const d = document.createElement('div'); d.appendChild(f);
    const missing = document.createElement('div'); missing.appendChild(T.tx('a.slot', { t: 'Bastion' }));
    return { slot: d.innerHTML, missing: missing.innerHTML, plain: T.t('a.rich', { name: 'X', c: 'c', m: 'm' }) };
  });
  assert.equal(r.slot, '已回退到 <a class="tierlink">Sentinel</a>');
  assert.equal(r.missing, '已回退到 Bastion', 'no slot: plain children');
  assert.equal(r.plain, '按 Ctrl K 打开 X，c m 注意');
  assert.ok(logs.some(([t, s]) => t === 'warning' && s.includes('use T.tx')), 'T.t on rich text warns');
  assert.ok(logs.some(([t, s]) => t === 'warning' && s.includes('no slot for <tier>')));
  await context.close();
});

test('set(): real script load → relabel text and attributes, <html lang>, store, exactly one i18n:change; same language does not reload', { skip }, async () => {
  const { page, context } = await open(mainUrl);
  const r = await page.evaluate(async () => {
    const added = [];
    new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) if (n.tagName === 'SCRIPT') added.push(n.getAttribute('src')); })
      .observe(document.head, { childList: true });
    const events = [];
    T.on('i18n:change', p => events.push(p));
    const changes = [];
    T.i18n.onChange(p => changes.push(p));
    const h = T.ti(document.createElement('h1'), 'a.title');
    const btn = document.createElement('button');
    T.ti(btn, 'a.count', { n: 1 });
    T.ti(btn, 'a.label', { what: 'X' }, { attr: 'aria-label' });
    T.ti(btn, 'a.title', null, { attr: 'title' });
    const rich = T.ti(document.createElement('p'), 'a.rich', { name: 'N', c: 'C', m: 'M' });
    const zh = T.ti(document.createElement('span'), 'a.only_zh');
    document.body.append(h, btn, rich, zh);
    const before = { h: h.textContent, btn: btn.textContent, aria: btn.getAttribute('aria-label'), attrMark: btn.getAttribute('data-i18n-attr'), richKbd: !!rich.querySelector('kbd') };
    const lang1 = await T.i18n.set('en_US');
    const afterSet = {
      lang1, h: h.textContent, btn: btn.textContent, aria: btn.getAttribute('aria-label'), title: btn.getAttribute('title'), rich: rich.textContent, richKbd: !!rich.querySelector('kbd'),
      zh: zh.textContent, htmlLang: document.documentElement.getAttribute('lang'), store: T.store.get('lang'), events: events.length, changes, added: [...added],
    };
    const lang2 = await T.i18n.set('en_US');
    await new Promise(r => setTimeout(r, 30));
    return { before, afterSet, lang2, events2: events.length, added2: [...added], stats: T.i18n.stats() };
  });
  assert.deepEqual(r.before, { h: '标题', btn: '1 项', aria: '关闭X', attrMark: 'aria-label:a.label;title:a.title', richKbd: true });
  assert.equal(r.afterSet.lang1, 'en_US');
  assert.equal(r.afterSet.h, 'Title');
  assert.equal(r.afterSet.btn, '1 item', 'plural follows the new language');
  assert.equal(r.afterSet.aria, 'Close X', 'attribute relabelled with its parameters');
  assert.equal(r.afterSet.title, 'Title');
  assert.equal(r.afterSet.rich, 'Press Ctrl K to open N, C M note');
  assert.equal(r.afterSet.richKbd, true, 'rich text re-rendered as elements');
  assert.equal(r.afterSet.zh, '仅中文', 'missing in en_US → zh_CN');
  assert.equal(r.afterSet.htmlLang, 'en-US');
  assert.equal(r.afterSet.store, 'en_US');
  assert.equal(r.afterSet.events, 1);
  assert.deepEqual(r.afterSet.changes, [{ lang: 'en_US', prev: 'zh_CN' }]);
  assert.deepEqual(r.afterSet.added, ['locale-en_US.T2.js'], 'zh_CN was preloaded; only en_US is fetched');
  assert.equal(r.lang2, 'en_US');
  assert.equal(r.events2, 1, 'same language: no event');
  assert.deepEqual(r.added2, ['locale-en_US.T2.js'], 'same language: no reload');
  assert.ok(r.stats.fallback >= 1);
  await context.close();
});

test('set(): a language file that fails to load or does not register keeps the language and rejects', { skip }, async () => {
  const { page, context } = await open(mainUrl);
  const r = await page.evaluate(async () => {
    const events = [];
    T.on('i18n:change', p => events.push(p));
    const el = T.ti(document.createElement('h1'), 'a.title');
    const out = {};
    for (const lang of ['fr_FR', 'ja_JP', 'xx_XX']) {
      try { await T.i18n.set(lang); out[lang] = 'resolved'; } catch (e) { out[lang] = e.code; }
    }
    return { out, lang: T.i18n.lang, text: el.textContent, events: events.length, fr: window.__frLoaded === true, scripts: document.querySelectorAll('script[src*="locale-"]').length };
  });
  assert.deepEqual(r.out, { fr_FR: 'not_registered', ja_JP: 'load_failed', xx_XX: 'unsupported_language' });
  assert.equal(r.fr, true, 'the fr_FR file did run');
  assert.equal(r.lang, 'zh_CN');
  assert.equal(r.text, '标题');
  assert.equal(r.events, 0);
  assert.equal(r.scripts, 1, 'failed loader scripts are removed again (only the preloaded zh_CN tag stays)');
  await context.close();
});

test('boot(): before hello, the navigator.language guess is only preloaded; hello.lang (the store write) switches at once', { skip }, async () => {
  const { page, context } = await open(mainUrl, 'de-DE');
  await page.waitForFunction(() => T.i18n.loaded().includes('de_DE') && T.i18n.loaded().includes('en_US'));
  const before = await page.evaluate(() => ({ lang: T.i18n.lang, store: T.store.get('lang'), html: document.documentElement.lang, t: T.t('a.title') }));
  assert.deepEqual(before, { lang: 'zh_CN', store: undefined, html: 'zh-CN', t: '标题' }, 'no optimistic switch (architecture §3.5)');
  const r = await page.evaluate(async () => {
    const added = [];
    new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) if (n.tagName === 'SCRIPT') added.push(n.getAttribute('src')); })
      .observe(document.head, { childList: true });
    T.store.set('lang', 'de_DE'); // what the rt client does with hello.lang
    await new Promise(res => setTimeout(res, 50));
    return { lang: T.i18n.lang, added, t: T.t('a.title'), label: T.t('a.label', { what: 'X' }), fallback: T.t('a.count', { n: 2 }), html: document.documentElement.lang };
  });
  assert.deepEqual(r, { lang: 'de_DE', added: [], t: 'Titel', label: 'X schließen', fallback: '2 items', html: 'de-DE' });
  await context.close();
});

// Fix round 1 (MUST-1): the real design target replays rt_connect_replay.json, whose hello
// arrives at connect, long before the navigator.language guess has loaded.
const SCENARIO = () => readFileSync(join(UI, 'test', 'fixtures', 'scenarios', 'rt_connect_replay.json'), 'utf8');
async function openDesign(locale, query) {
  const context = await browser.newContext({ locale, viewport: { width: 1280, height: 800 } });
  await context.route(/^(?!file:)/, r => r.abort());
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, SCENARIO());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(designUrl + query);
  await page.waitForFunction(() => window.T && T.i18n && T.store.get('lang') !== undefined);
  await page.waitForFunction(() => T.i18n.loaded().includes('en_US')); // the guess/fallback preload has settled
  await page.waitForTimeout(100);
  const state = await page.evaluate(() => ({ i18n: T.i18n.lang, store: T.store.get('lang'), html: document.documentElement.lang }));
  return { context, page, state, errors };
}

test('design target, en-US browser, hello.lang zh_CN (?lang=zh_CN): the page stays zh_CN', { skip }, async () => {
  const { context, state, errors } = await openDesign('en-US', '?lang=zh_CN');
  assert.deepEqual(state, { i18n: 'zh_CN', store: 'zh_CN', html: 'zh-CN' });
  assert.deepEqual(errors, []);
  await context.close();
});

test('design target, en-US browser, fixture hello.lang zh_CN (no ?lang): the page stays zh_CN', { skip }, async () => {
  const { context, state } = await openDesign('en-US', '');
  assert.deepEqual(state, { i18n: 'zh_CN', store: 'zh_CN', html: 'zh-CN' });
  await context.close();
});

test('design target, zh-CN browser, hello.lang en_US (?lang=en_US): the page follows hello', { skip }, async () => {
  const { context, page } = await openDesign('zh-CN', '?lang=en_US');
  await page.waitForFunction(() => T.i18n.lang === 'en_US');
  const state = await page.evaluate(() => ({ i18n: T.i18n.lang, store: T.store.get('lang'), html: document.documentElement.lang }));
  assert.deepEqual(state, { i18n: 'en_US', store: 'en_US', html: 'en-US' });
  await context.close();
});

test('T.html: escaped text that decodes to a placeholder cannot forge one; Nodes in raw text are dropped with a warning', { skip }, async () => {
  const { page, context, logs } = await open(mainUrl);
  const r = await page.evaluate(() => {
    const forged = [];
    for (let seq = 0; seq < 5; seq++) for (let k = 0; k < 3; k++) forged.push(`<!--t-node-${seq}-${k}-->`);
    const evil = forged.join('');
    const el = T.html`<div title="${evil}" data-x="${T.tx('a.title')}">${T.tx('a.title')}</div>`;
    const ta = T.html`<textarea>${T.tx('a.title')}</textarea>`;
    return { title: el.getAttribute('title'), x: el.getAttribute('data-x'), text: el.textContent, ta: ta.value };
  });
  assert.equal(r.title, Array.from({ length: 15 }, (_, i) => `<!--t-node-${Math.floor(i / 3)}-${i % 3}-->`).join(''), 'user text stays as written');
  assert.equal(r.x, '标题');
  assert.equal(r.text, '标题');
  assert.equal(r.ta, '', 'a Node cannot go into raw text: dropped, and no placeholder text is left behind');
  assert.ok(!logs.some(([t]) => t === 'pageerror'), JSON.stringify(logs));
  assert.ok(logs.some(([t, s]) => t === 'warning' && s.includes('[T.html] 1 interpolated Node(s) dropped')), JSON.stringify(logs));
  await context.close();
});

test('T.html: a Node interpolation is inserted as the node, never parsed; in an attribute it is text', { skip }, async () => {
  const { page, context } = await open(mainUrl);
  const r = await page.evaluate(() => {
    const slot = T.tx('a.slot', { t: '<b>Sentinel</b>' }, { tier: k => { const a = document.createElement('a'); a.className = 'tierlink'; a.appendChild(k); return a; } });
    const el = T.html`<div class="wrap" title="${T.tx('a.title')}">${slot}<span>${'<i>x</i>'}</span>${[T.tx('a.title'), ' tail']}</div>`;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_COMMENT);
    return {
      tag: el.tagName, title: el.getAttribute('title'), tier: el.querySelector('a.tierlink')?.textContent, tierB: !!el.querySelector('a.tierlink b'),
      span: el.querySelector('span').textContent, italics: el.querySelectorAll('i').length, text: el.textContent, comments: walker.nextNode() === null ? 0 : 1,
      plain: T.html`<p>${'a'}${T.raw('<b>b</b>')}</p>`.outerHTML,
    };
  });
  assert.equal(r.tag, 'DIV');
  assert.equal(r.title, '标题');
  assert.equal(r.tier, '<b>Sentinel</b>');
  assert.equal(r.tierB, false, 'parameter stayed text inside the slot');
  assert.equal(r.span, '<i>x</i>');
  assert.equal(r.italics, 0);
  assert.equal(r.text, '已回退到 <b>Sentinel</b><i>x</i>标题 tail');
  assert.equal(r.comments, 0, 'no placeholder comment left behind');
  assert.equal(r.plain, '<p>a<b>b</b></p>', 'strings and T.raw behave as before');
  await context.close();
});

test('--i18n-trace builds mark T.tx / T.ti output with data-i18n-id and index T.t strings', { skip }, async () => {
  const { page, context } = await open(traceUrl);
  const r = await page.evaluate(() => {
    const d = document.createElement('div');
    d.appendChild(T.tx('a.rich', { name: 'N', c: 'C', m: 'M' }));
    const h = T.ti(document.createElement('h2'), 'a.title');
    const s = T.t('a.label', { what: 'Q' });
    return { wrap: d.firstElementChild.getAttribute('data-i18n-id'), h: h.getAttribute('data-i18n-id'), traced: T.i18n.traced()[s] };
  });
  assert.deepEqual(r, { wrap: 'a.rich', h: 'a.title', traced: ['a.label'] });
  await context.close();
});
