// T-P4-03: i18n runtime without a browser — lookup and fallback, set/boot semantics,
// language guess, the T.page relocalize contract, pseudo language (architecture §3.1–§3.8).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadI18n, REPO } from '../fixtures/i18n/helpers.mjs';

const m = await loadI18n();
const { icu, pseudo } = m;
const P = s => icu.parse(s);

// A fake document whose <script> loads are driven by the test: load(lang) registers the
// catalog (or not) and fires onload/onerror on the next tick.
function fakeDoc(behaviour) {
  const scripts = [];
  const doc = {
    documentElement: { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } },
    createElement: () => ({ src: '', parentNode: null }),
    head: {
      appendChild(s) {
        scripts.push(s.src);
        s.parentNode = { removeChild() {} };
        setTimeout(() => behaviour(s), 0);
      },
    },
    querySelectorAll: () => [],
    createDocumentFragment: () => ({}),
  };
  return { doc, scripts };
}
function fakeStore() {
  const state = {}, subs = new Map();
  return {
    get: k => state[k],
    set(k, v) { if (Object.is(state[k], v)) return; state[k] = v; for (const fn of subs.get(k) || []) fn(v); },
    sub(k, fn) { if (!subs.has(k)) subs.set(k, new Set()); subs.get(k).add(fn); return () => subs.get(k).delete(fn); },
  };
}

test('lookup: current language, then en_US, then zh_CN; unknown id → id; dev counts and warns once', async () => {
  const warns = [];
  const rt = m.createI18n({ dev: true, win: { console: { warn: w => warns.push(w) }, setTimeout } });
  rt.register('zh_CN', { 'a.only_zh': P('只有中文'), 'a.both': P('中文'), 'a.three': P('中') });
  rt.register('en_US', { 'a.both': P('English'), 'a.three': P('en') });
  rt.register('de_DE', { 'a.three': P('de') });
  await rt.set('de_DE');
  assert.equal(rt.lang, 'de_DE');
  assert.equal(rt.t('a.three'), 'de');
  assert.equal(rt.t('a.both'), 'English');
  assert.equal(rt.t('a.only_zh'), '只有中文');
  assert.equal(rt.t('a.only_zh'), '只有中文');
  assert.equal(rt.t('a.nowhere'), 'a.nowhere');
  const s = rt.stats();
  assert.equal(s.fallback, 3);
  assert.equal(s.missing, 1);
  assert.equal(warns.filter(w => w.includes('a.only_zh')).length, 1, 'one warning per id');
  assert.equal(rt.has('a.both'), false, 'has() is about the current language');
  assert.equal(rt.exact('a.both'), null, 'exact() never falls back (T.fmt uses Intl instead)');
  assert.equal(rt.text('a.both'), 'English');
});

test('hasAny(id) follows the T.t chain (current → en_US → zh_CN); has(id) stays current-only (T-P4-H4, P5 #34)', async () => {
  const rt = m.createI18n({});
  rt.register('zh_CN', { 'a.zh': P('只有中文'), 'a.en': P('中文'), 'a.de': P('中') });
  rt.register('en_US', { 'a.en': P('English'), 'a.de': P('en') });
  rt.register('de_DE', { 'a.de': P('de') });
  await rt.set('de_DE');
  for (const [id, text] of [['a.de', 'de'], ['a.en', 'English'], ['a.zh', '只有中文']]) {
    assert.equal(rt.hasAny(id), true, `${id} is found somewhere on the chain`);
    assert.equal(rt.t(id), text, `${id}: T.t takes the same message hasAny found`);
  }
  assert.equal(rt.has('a.en'), false, 'has() looks at the current language only');
  assert.equal(rt.hasAny('a.nowhere'), false);
  assert.equal(rt.stats().missing, 0, 'hasAny does not count as a lookup');
  // zh_CN current: en_US is still a step of the chain, but zh_CN is the source of every id
  await rt.set('zh_CN');
  assert.equal(rt.hasAny('a.zh'), true);
  assert.equal(rt.hasAny('a.nowhere'), false);
});

test('errorText / reasonText with the real runtime: a message_key missing in the current language falls back like T.t, not to errors.unknown (T-P4-H4, P5 #34)', async () => {
  const mod = await loadI18n({ contents: "export * from './t.js'; export { errorText, reasonText } from '../rt/errors.js';" });
  const rt = mod.createI18n({});
  rt.register('zh_CN', { 'errors.shell.mode_failed': P('中文原因'), 'errors.unknown': P('未知错误'), 'errors.io': P('读写失败'),
    'tiers.reason.timeout': P('超时'), 'common.reason_unknown': P('未知原因') });
  rt.register('en_US', { 'errors.unknown': P('unknown error'), 'errors.io': P('io failed') });
  rt.register('ja_JP', {});
  await rt.set('ja_JP');
  const T = { t: rt.t, i18n: rt };
  // message_key only in zh_CN → the zh_CN text (what T.t gives), not errors.unknown
  assert.equal(mod.errorText(T, { code: 'shell_mode_failed', message_key: 'errors.shell.mode_failed' }), rt.t('errors.shell.mode_failed'));
  assert.equal(mod.errorText(T, { code: 'shell_mode_failed', message_key: 'errors.shell.mode_failed' }), '中文原因');
  // errors.<code> only in en_US / zh_CN → the en_US text
  assert.equal(mod.errorText(T, { code: 'io', message_key: 'errors.not_anywhere' }), 'io failed');
  // nothing anywhere → errors.unknown through the same chain
  assert.equal(mod.errorText(T, { code: 'brand_new', message_key: 'errors.not_anywhere' }), 'unknown error');
  assert.equal(mod.reasonText(T, 'tiers.reason', 'timeout'), '超时');
  assert.equal(mod.reasonText(T, 'tiers.reason', 'brand_new'), '未知原因');
});

test('release builds do not count or warn', async () => {
  const warns = [];
  const rt = m.createI18n({ dev: false, win: { console: { warn: w => warns.push(w) }, setTimeout } });
  rt.register('zh_CN', { 'a.x': P('<b>x</b>') });
  assert.equal(rt.t('a.missing'), 'a.missing');
  assert.equal(rt.t('a.x'), 'x');
  assert.deepEqual(rt.stats(), { fallback: 0, missing: 0, byId: {} });
  assert.deepEqual(warns, []);
});

test('T.t on a message with tags drops the tags and warns in development builds', () => {
  const warns = [];
  const rt = m.createI18n({ dev: true, win: { console: { warn: w => warns.push(w) }, setTimeout } });
  rt.register('zh_CN', { 'a.rich': P('按 <kbd>Ctrl K</kbd> 打开') });
  assert.equal(rt.t('a.rich'), '按 Ctrl K 打开');
  assert.ok(warns.some(w => /use T\.tx/.test(w)));
});

test('set(): load → <html lang> → store lang → one i18n:change; same language resolves without reloading', async () => {
  let rt;
  const { doc, scripts } = fakeDoc(s => { if (s.src.includes('en_US')) rt.register('en_US', { 'a.x': P('x') }); s.onload(); });
  const store = fakeStore();
  const events = [];
  rt = m.createI18n({ doc, store, emit: (e, p) => events.push([e, p]), files: { zh_CN: 'locale-zh_CN.A.js', en_US: 'locale-en_US.B.js' } });
  rt.register('zh_CN', { 'a.x': P('叉') });
  const changes = [];
  rt.onChange(p => changes.push(p));
  assert.equal(await rt.set('en_US'), 'en_US');
  assert.equal(rt.lang, 'en_US');
  assert.equal(doc.documentElement.attrs.lang, 'en-US');
  assert.equal(store.get('lang'), 'en_US');
  assert.deepEqual(events, [['i18n:change', { lang: 'en_US', prev: 'zh_CN' }]]);
  assert.deepEqual(changes, [{ lang: 'en_US', prev: 'zh_CN' }]);
  assert.deepEqual(scripts, ['locale-en_US.B.js']);
  assert.equal(await rt.set('en_US'), 'en_US');
  assert.deepEqual(scripts, ['locale-en_US.B.js'], 'no second load');
  assert.equal(events.length, 1, 'no second change event');
});

test('set(): a failed load keeps the language and rejects; a file that does not register rejects; unknown language rejects', async () => {
  let rt;
  const { doc } = fakeDoc(s => {
    if (s.src.includes('de_DE')) s.onerror();
    else if (s.src.includes('fr_FR')) s.onload(); // loads but never registers
    else { rt.register('ja_JP', {}); s.onload(); }
  });
  const events = [];
  rt = m.createI18n({ doc, emit: e => events.push(e), files: { zh_CN: 'z.js', de_DE: 'd.js?de_DE', fr_FR: 'f.js?fr_FR', ja_JP: 'j.js' } });
  rt.register('zh_CN', {});
  await assert.rejects(rt.set('de_DE'), e => e.code === 'load_failed');
  assert.equal(rt.lang, 'zh_CN');
  await assert.rejects(rt.set('fr_FR'), e => e.code === 'not_registered');
  assert.equal(rt.lang, 'zh_CN');
  await assert.rejects(rt.set('xx_XX'), e => e.code === 'unsupported_language');
  assert.deepEqual(events, []);
  assert.equal(await rt.set('ja_JP'), 'ja_JP', 'a working language still switches afterwards');
  assert.deepEqual(events, ['i18n:change']);
});

test('set(): the latest request wins when loads overlap', async () => {
  let rt;
  const pending = {};
  const { doc } = fakeDoc(s => { pending[s.src] = s; });
  rt = m.createI18n({ doc, files: { zh_CN: 'zh', de_DE: 'de', en_US: 'en' } });
  rt.register('zh_CN', {});
  const slow = rt.set('de_DE');
  await new Promise(r => setTimeout(r, 5));
  const fast = rt.set('en_US');
  await new Promise(r => setTimeout(r, 5));
  rt.register('en_US', {}); pending.en.onload();
  assert.equal(await fast, 'en_US');
  rt.register('de_DE', {}); pending.de.onload();
  await assert.rejects(slow, e => e.code === 'superseded', 'the superseded request rejects (it never resolves to a language it did not apply)');
  assert.equal(rt.lang, 'en_US');
});

test('set(): a later request that fails still supersedes the earlier one; both reject, the language stays', async () => {
  let rt;
  const pending = {};
  const { doc } = fakeDoc(s => { pending[s.src] = s; });
  rt = m.createI18n({ doc, files: { zh_CN: 'zh', de_DE: 'de', fr_FR: 'fr' } });
  rt.register('zh_CN', {});
  const first = rt.set('de_DE');
  await new Promise(r => setTimeout(r, 5));
  const second = rt.set('fr_FR');
  await new Promise(r => setTimeout(r, 5));
  pending.fr.onerror();
  await assert.rejects(second, e => e.code === 'load_failed');
  rt.register('de_DE', {}); pending.de.onload();
  await assert.rejects(first, e => e.code === 'superseded');
  assert.equal(rt.lang, 'zh_CN');
});

test('boot(): only preloads the navigator.languages guess (no switch, no store write); a store write of `lang` is followed', async () => {
  let rt;
  const scripts = [];
  const { doc } = fakeDoc(s => { scripts.push(s.src); rt.register(s.src, {}); s.onload(); });
  const store = fakeStore();
  const events = [];
  rt = m.createI18n({ doc, store, emit: e => events.push(e), files: { zh_CN: 'zh_CN', en_US: 'en_US', ru_RU: 'ru_RU', ko_KR: 'ko_KR' }, win: { navigator: { languages: ['ru-RU', 'en'] }, console, setTimeout } });
  rt.register('zh_CN', {});
  assert.equal(await rt.boot(), 'zh_CN', 'the page stays on its language until hello (architecture §3.5)');
  assert.deepEqual(scripts.sort(), ['en_US', 'ru_RU'], 'the guess and the en_US fallback were preloaded');
  assert.equal(store.get('lang'), undefined, 'the store is not pre-written: the first hello write always notifies');
  assert.deepEqual(events, []);
  store.set('lang', 'ru_RU'); // the rt client writing hello.lang into the store: preloaded, switches at once
  await new Promise(r => setTimeout(r, 10));
  assert.equal(rt.lang, 'ru_RU');
  store.set('lang', 'ko_KR'); // host:language
  await new Promise(r => setTimeout(r, 10));
  assert.equal(rt.lang, 'ko_KR');
  assert.deepEqual(events, ['i18n:change', 'i18n:change']);
});

test('boot() race (fix round 1, MUST-1): hello.lang equal to the applied language arrives while the guess is loading → the page stays on hello.lang', async () => {
  let rt;
  const pending = {};
  const { doc } = fakeDoc(s => { pending[s.src] = s; }); // language file loads are held by the test
  const store = fakeStore();
  const events = [];
  rt = m.createI18n({ doc, store, emit: (e, p) => events.push([e, p]), files: { zh_CN: 'zh_CN', en_US: 'en_US' }, win: { navigator: { languages: ['en-US'] }, console, setTimeout } });
  rt.register('zh_CN', {}); // statically preloaded source catalog
  const booting = rt.boot();
  await new Promise(r => setTimeout(r, 5));
  assert.ok(pending.en_US, 'en_US preload in flight');
  store.set('lang', 'zh_CN'); // hello.lang = zh_CN, before en_US has loaded
  rt.register('en_US', {}); pending.en_US.onload();
  await booting;
  await new Promise(r => setTimeout(r, 10));
  assert.equal(rt.lang, 'zh_CN');
  assert.equal(store.get('lang'), 'zh_CN');
  assert.equal(doc.documentElement.attrs.lang, undefined, '<html lang> untouched (index.html already says zh-CN)');
  assert.deepEqual(events, [], 'no i18n:change');
});

test('boot() race: a switch in flight is superseded by a later store write of the applied language', async () => {
  let rt;
  const pending = {};
  const { doc } = fakeDoc(s => { pending[s.src] = s; });
  const store = fakeStore();
  const events = [];
  rt = m.createI18n({ doc, store, emit: e => events.push(e), files: { zh_CN: 'zh_CN', en_US: 'en_US' }, win: { navigator: { languages: ['zh-CN'] }, console, setTimeout } });
  rt.register('zh_CN', {});
  await Promise.race([rt.boot(), new Promise(r => setTimeout(r, 5))]);
  store.set('lang', 'en_US'); // hello.lang = en_US: loading
  await new Promise(r => setTimeout(r, 5));
  store.set('lang', 'zh_CN'); // host:language = zh_CN before en_US arrives
  rt.register('en_US', {}); pending.en_US.onload();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(rt.lang, 'zh_CN');
  assert.equal(store.get('lang'), 'zh_CN');
  assert.deepEqual(events, []);
});

test('a store-driven switch that fails puts the store back to the applied language, so the same value can retry', async () => {
  let rt;
  let fail = true;
  const { doc } = fakeDoc(s => { if (fail) s.onerror(); else { rt.register('ja_JP', {}); s.onload(); } });
  const store = fakeStore();
  rt = m.createI18n({ doc, store, files: { zh_CN: 'zh_CN', ja_JP: 'ja_JP' }, win: { navigator: { languages: ['zh-CN'] }, console, setTimeout } });
  rt.register('zh_CN', {});
  await rt.boot();
  store.set('lang', 'ja_JP');
  await new Promise(r => setTimeout(r, 10));
  assert.equal(rt.lang, 'zh_CN');
  assert.equal(store.get('lang'), 'zh_CN', 'store names the applied language again');
  fail = false;
  store.set('lang', 'ja_JP');
  await new Promise(r => setTimeout(r, 10));
  assert.equal(rt.lang, 'ja_JP');
});

test('T.ti / relabel write text-bearing attributes only (title, placeholder, alt, label, value, data-tip, aria-*)', () => {
  const warns = [];
  const rt = m.createI18n({ dev: true, doc: { createDocumentFragment: () => ({}) }, win: { console: { warn: w => warns.push(w) }, setTimeout } });
  rt.register('zh_CN', { 'a.x': P('文字') });
  const el = () => { const a = {}; return { a, setAttribute(k, v) { a[k] = v; }, getAttribute: k => (k in a ? a[k] : null) }; };
  for (const attr of ['title', 'placeholder', 'aria-label', 'aria-description', 'data-tip', 'alt']) {
    const e = el(); rt.ti(e, 'a.x', null, { attr });
    assert.equal(e.a[attr], '文字', attr);
    assert.equal(e.a['data-i18n-attr'], `${attr}:a.x`);
  }
  for (const attr of ['onclick', 'href', 'src', 'srcdoc', 'style', 'action', 'formaction', 'aria-labelledby', 'class']) {
    const e = el(); rt.ti(e, 'javascript:alert(1)', null, { attr });
    assert.deepEqual(e.a, {}, `${attr} is not written and not registered`);
  }
  assert.ok(warns.some(w => w.includes('"onclick" is not translatable')));
});

test('language guess matches the shell (native_text.rs language(): primary language, else en_US)', () => {
  const vectors = [
    [['zh-CN'], 'zh_CN'], [['zh-TW'], 'zh_CN'], [['zh-HK'], 'zh_CN'], [['zh-Hant-TW'], 'zh_CN'],
    [['en-GB'], 'en_US'], [['de-AT'], 'de_DE'], [['es-MX'], 'es_ES'], [['fr-CA'], 'fr_FR'],
    [['ja'], 'ja_JP'], [['ko-KR'], 'ko_KR'], [['pt-PT'], 'pt_BR'], [['ru'], 'ru_RU'],
    [['it-IT', 'de-DE'], 'en_US'], [[], 'en_US'], [[''], 'en_US'],
  ];
  for (const [list, want] of vectors) assert.equal(m.guessLanguage(list), want, list.join(','));
  // the shell's table, read from its source: LANGID primary language id → locale
  const rs = readFileSync(join(REPO, 'shell', 'app', 'src', 'native_text.rs'), 'utf8');
  const arms = [...rs.matchAll(/0x([0-9a-f]{2})=>"([a-z]{2}_[A-Z]{2})"/g)].map(x => [parseInt(x[1], 16), x[2]]);
  const PRIMARY_LANGID = { 0x04: 'zh', 0x07: 'de', 0x0a: 'es', 0x0c: 'fr', 0x11: 'ja', 0x12: 'ko', 0x16: 'pt', 0x19: 'ru' };
  assert.equal(arms.length, 8);
  for (const [id, locale] of arms) assert.equal(m.guessLanguage([PRIMARY_LANGID[id]]), locale, `LANGID 0x${id.toString(16)}`);
  assert.match(rs, /_=>"en_US"/, 'everything else → en_US on both sides');
});

test('T.page relocalize contract: snapshotState → rerender → restoreState (router side is T-P4-04a)', () => {
  const calls = [];
  const page = {
    relocalize: 'rerender',
    draft: 'unsaved text', scroll: 120,
    snapshotState() { calls.push('snapshot'); return { draft: this.draft, scroll: this.scroll }; },
    restoreState(s) { calls.push('restore'); this.draft = s.draft; this.scroll = s.scroll; },
  };
  const rerender = () => { calls.push('rerender'); page.draft = ''; page.scroll = 0; };
  assert.equal(m.relabel.relocalizePage(page, rerender), true);
  assert.deepEqual(calls, ['snapshot', 'rerender', 'restore']);
  assert.equal(page.draft, 'unsaved text');
  assert.equal(page.scroll, 120);
  assert.equal(m.relabel.relocalizePage({ render() {} }, () => calls.push('x')), false, 'pages without relocalize are relabelled in place only');
  assert.equal(calls.includes('x'), false);
});

test('pseudo language: zh_CN base ×3 per CJK char +10 %, en base +40 %, placeholders and tags kept', () => {
  assert.equal(pseudo.PSEUDO.ZH_CHARS_PER_CJK, 3);
  assert.equal(pseudo.PSEUDO.ZH_EXTRA, 0.1);
  assert.equal(pseudo.PSEUDO.EN_EXTRA, 0.4);
  const env = m.icuEnv('en_US');
  const start = icu.format(pseudo.pseudoMessage(P('启动服务'), 'zh'), {}, env);
  const inner = start.slice(1, -1);
  assert.ok(start.startsWith('⟦') && start.endsWith('⟧'));
  assert.ok(inner.length >= 12, `「启动服务」→ ${start} (${inner.length})`);
  assert.equal(inner.length, 12 + 2, '12 letters + ceil(12 × 10 %) padding');
  assert.ok(!/[一-鿿]/.test(start), 'no CJK left');
  const src = P('按 <kbd>Ctrl K</kbd> 打开 {name}，共 {n, plural, other {# 个}} {mode, select, tun {TUN} other {系统}}');
  const ps = pseudo.pseudoMessage(src, 'zh');
  const a = icu.analyze(src), b = icu.analyze(ps);
  assert.deepEqual(Object.keys(b.args).sort(), Object.keys(a.args).sort());
  assert.deepEqual([...b.tags], [...a.tags]);
  assert.deepEqual(b.selects, a.selects);
  const out = icu.formatParts(ps, { name: 'NAME', n: 3, mode: 'tun' }, env);
  assert.ok(icu.partsToText(out).includes('NAME') && icu.partsToText(out).includes('3'));
  assert.ok(out.some(p => typeof p === 'object' && p.tag === 'kbd'));
  const en = icu.format(pseudo.pseudoMessage(P('Start service'), 'en'), {}, env);
  assert.equal(en.length - 2, 'Start service'.length + Math.ceil('Start service'.length * 0.4));
  const cat = pseudo.pseudoCatalog({ 'a.x': P('启动'), 'a.y': P('停止') }, { 'a.y': P('Stop') });
  assert.equal(icu.format(cat['a.y'], {}, env).length, 2 + 4 + 4, 'English base where it exists (4 letters + the +4 floor)');
  assert.equal(icu.format(cat['a.x'], {}, env).length, 2 + 10 + 1, 'two characters: 5 letters each + ceil(10 × 10 %)');
});

// T-P4-H (决定_第五波裁定 10-3): short-string floor. A message of at most two source characters
// counts five letters per character, and every pseudo text is at least four letters longer
// than its source (padding makes up what the letters do not). Fix round 1: "characters" are the
// translatable ones — CJK in a zh_CN base, every character in an English one.
test('pseudo language: short-string floor (≤ 2 characters → 5 letters each; always ≥ source + 4 letters)', () => {
  assert.equal(pseudo.PSEUDO.SHORT_MAX, 2);
  assert.equal(pseudo.PSEUDO.SHORT_PER_CHAR, 5);
  assert.equal(pseudo.PSEUDO.MIN_GROWTH, 4);
  const env = m.icuEnv('en_US');
  const inner = (src, base = 'zh') => { const s = icu.format(pseudo.pseudoMessage(P(src), base), {}, env); return [...s.slice(1, -1)]; };
  const letters = a => a.filter(ch => ch !== '~').length;
  // one and two CJK characters: 5 letters each, then the usual +10 % padding
  assert.equal(letters(inner('停')), 5);
  assert.equal(inner('停').length, 5 + 1);
  assert.equal(letters(inner('停止')), 10);
  assert.equal(inner('停止').length, 10 + 1);
  assert.ok(!/[一-鿿]/.test(inner('刷新').join('')), 'no CJK left');
  // three characters and more: back to 3 letters per CJK character (+10 %)
  assert.equal(inner('重启服').length, 9 + 1);
  assert.equal(inner('启动服务').length, 12 + 2);
  // short English bases: padded up to the floors
  assert.equal(inner('Go', 'en').length, 10);
  assert.equal(inner('Stop', 'en').length, 4 + 4, 'source + 4 beats +40 %');
  assert.equal(inner('Refresh', 'en').length, 7 + 4);
  assert.equal(inner('Start service', 'en').length, 13 + 6, '+40 % beats source + 4');
  for (const [src, base] of [['停', 'zh'], ['停止', 'zh'], ['刷新列表', 'zh'], ['Stop', 'en'], ['Retry now', 'en'], ['A', 'en']]) {
    const n = [...src].length;
    const out = inner(src, base).length;
    assert.ok(out >= n + 4, `${src}: ${out} ≥ ${n} + 4`);
    if (n <= 2) assert.ok(out >= 5 * n, `${src}: ${out} ≥ 5 × ${n}`);
  }
  // fix round 1: Latin text in a zh_CN message is a unit / protocol / product token every language
  // keeps — accented 1:1 with the +10 % padding, no floor (no false overflow on " ms", "KB", "/s")
  assert.equal(inner('OK').length, 2 + 1);
  assert.equal(inner('KB').length, 2 + 1);
  assert.equal(icu.format(pseudo.pseudoMessage(P('{n} ms'), 'zh'), { n: 42 }, env), '⟦42 ɱš~⟧');
  // only translatable literal text counts: arguments and tags are not source characters, and in a
  // zh_CN base only the CJK characters are; a select / plural is measured along its shortest path
  assert.equal(pseudo.sourceLength(P('{name}')), 0);
  assert.equal(pseudo.sourceLength(P('共 {n} 个')), 2);
  assert.equal(pseudo.sourceLength(P('<b>共</b> {n} 个 KB')), 2);
  assert.equal(pseudo.sourceLength(P('Stop now'), 'en'), 8);
  assert.equal(pseudo.sourceLength(P('{mode, select, tun {TUN} other {系统}}')), 0);
  assert.equal(pseudo.sourceLength(P('前{mode, select, tun {TUN} other {系统模式}}')), 1);
  const argOnly = icu.format(pseudo.pseudoMessage(P('{name}'), 'zh'), { name: 'X' }, env);
  assert.equal(argOnly, '⟦X⟧', 'a message with no literal text gets no padding');
  const withArg = pseudo.pseudoMessage(P('{n} 个'), 'zh');
  assert.equal(icu.format(withArg, { n: 3 }, env).length, 2 + 1 + 1 + 5 + 1, 'short: 「个」 is 1 character → 5 letters, + ceil(6 × 10 %)');
});

// T-P4-H fix round 1 (review S2): a select / plural is judged per branch, so a short state word
// chosen by a select gets the short-string floor even when its sibling branches are long.
test('pseudo language: select / plural branches get the short-string floor on their own', () => {
  const env = m.icuEnv('en_US');
  const fmt = (src, args) => [...icu.format(pseudo.pseudoMessage(P(src), 'zh'), args, env).slice(1, -1)];
  const letters = a => a.filter(ch => ch !== '~').length;
  const mode = '{mode, select, tun {TUN} other {系统}}';
  assert.equal(letters(fmt(mode, { mode: 'sys' })), 10, '「系统」 is a 2-character path → 5 letters each');
  assert.equal(fmt(mode, { mode: 'sys' }).length, 10 + 1);
  assert.equal(fmt(mode, { mode: 'tun' }).length, 3 + 1, 'the Latin branch: accented 1:1 + 10 %, no floor');
  const state = '{state, select, live {实时} other {未启动 · 启动服务后开始采集}}';
  assert.equal(fmt(state, { state: 'live' }).length, 10 + 1, 'the short branch is not diluted by the long one');
  const long = fmt(state, { state: 'x' });
  assert.equal(letters(long), 3 * 12 + 3, '12 CJK characters at 3 letters, " · " kept');
  assert.equal(long.length - letters(long), Math.ceil((3 * 12 + 3) * 0.1), 'the long branch pads +10 % of its own letters');
  // the text around the select is part of every branch path
  const around = '前{mode, select, tun {TUN} other {系统}}';
  assert.equal(letters(fmt(around, { mode: 'tun' })), 5 + 3, '「前」 + TUN is a 1-character path → 5 letters');
  assert.ok(fmt(around, { mode: 'sys' }).length >= 3 * 3 + 4, '「前系统」 is 3 characters: 3 letters each, ≥ source + 4');
  // plural with a single branch behaves like a plain short message; the padding sits in the branch
  const err = pseudo.pseudoMessage(P('{n, plural, other {错误}}'), 'zh');
  assert.equal(icu.format(err, { n: 2 }, env).length, 2 + 10 + 1);
  assert.equal(err[err.length - 1], '⟧');
  const branch = err[1][3].other;
  assert.ok(String(Array.isArray(branch) ? branch[branch.length - 1] : branch).endsWith('~'), 'padding at the end of the branch');
  // an empty branch with nothing around it stays empty
  assert.equal(icu.format(pseudo.pseudoMessage(P('{g}{cost, select, yes { · 短信按条计费} other {}}'), 'zh'), { g: 'G', cost: 'no' }, env), '⟦G⟧');
  // two or more top-level selects keep a single padding at the end (shortest path)
  const two = pseudo.pseudoMessage(P('{a, select, x {开} other {关闭}}{b, select, x {开} other {关}}'), 'zh');
  assert.equal(typeof two[two.length - 1], 'string');
  assert.ok(two[two.length - 1].endsWith('~⟧'));
});
