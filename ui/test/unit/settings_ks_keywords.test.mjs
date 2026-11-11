// T-P5-14 (requirement 1): the kill switch's keywords (settings.conn.ks.kw.*) can be typed into the
// "other addresses" field in all nine languages, as shown and in the forms an IME or a copy gives:
// full-width Latin (IME full-width mode), half-width katakana (ja IME F8), NFD (an accent or a
// Hangul syllable split into combining marks, e.g. copied from macOS). The page compares both sides
// after normalize('NFKC') and toLowerCase() (ui/src/pages/settings.js ksKw).
//
// Pure: the page's own declarations (KW_IDS, kwShow, kwFold, ksKw, and the token field's split) are
// taken from settings.js by acorn and run with each language's catalog as t(), so the test follows
// the code, not a copy of it. The browser half (typing into the real field, the chip it adds) is the
// render case ks_keyword_variants (ui/test/render/cases/inputs_a11y.mjs).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as acorn from 'acorn';
import * as walk from 'acorn-walk';

const UI = new URL('../../', import.meta.url);
const read = rel => readFileSync(new URL(rel, UI), 'utf8');
const LANGS9 = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];
const KEYS = { local_subnet: 'LocalSubnet', default_gateway: 'DefaultGateway' };
const SRC = read('src/pages/settings.js');

/* the source of the named `const` declarators (the first of each name) and of tokens()'s split */
function pageCode() {
  const ast = acorn.parse(SRC, { ecmaVersion: 'latest', sourceType: 'module' });
  const want = ['KW_IDS', 'kwShow', 'kwFold', 'ksKw'], got = {};
  let split = null;
  walk.ancestor(ast, {
    VariableDeclarator(n, _, anc) {
      const name = n.id && n.id.name;
      if (want.includes(name) && !got[name]) got[name] = SRC.slice(n.start, n.end);
      if (name === 'split' && !split && anc.some(a => a.type === 'FunctionDeclaration' && a.id && a.id.name === 'tokens')) split = SRC.slice(n.start, n.end);
    },
  });
  for (const k of want) assert.ok(got[k], `settings.js declares ${k}`);
  assert.ok(split, 'settings.js tokens() declares split');
  return `const ${want.map(k => got[k]).join(', ')}, ${split};`;
}
const CODE = pageCode();
const catalog = lang => {
  const raw = JSON.parse(read(`src/i18n/locales/${lang}/settings.json`));
  return id => { const v = raw[id]; return typeof v === 'string' ? v : v && v.m; };
};
function pageWith(t) {
  // eslint-disable-next-line no-new-func
  return { ...new Function('t', `${CODE}\nreturn { KW_IDS, kwShow, ksKw, split };`)(t), word: id => t(`settings.conn.ks.kw.${id}`) };
}
function pageIn(lang) {
  const cat = catalog(lang);
  return pageWith(id => { const v = cat(id); if (typeof v !== 'string') throw new Error(`${lang}: no ${id}`); return v; });
}

/* the variants of a word */
const fullWidth = s => s.replace(/[!-~]/g, c => String.fromCharCode(c.charCodeAt(0) + 0xfee0));
// full-width katakana → half-width (U+FF61-FF9F; a voiced / semi-voiced mark becomes ﾞ / ﾟ)
const HALF = new Map();
for (let c = 0xff61; c <= 0xff9f; c++) { const h = String.fromCharCode(c); HALF.set(h.normalize('NFKC'), h); }
const halfKana = s => [...s].map(ch => {
  const d = ch.normalize('NFD'), base = d[0], mark = d.slice(1);
  const h = HALF.get(base);
  if (!h) return ch;
  return h + (mark === '゙' ? 'ﾞ' : mark === '゚' ? 'ﾟ' : mark);
}).join('');
function variants(w) {
  const out = [['as shown', w], ['full-width', fullWidth(w)], ['half-width katakana', halfKana(w)], ['NFD', w.normalize('NFD')],
    ['full-width NFD', fullWidth(w).normalize('NFD')], ['upper case', w.toUpperCase()]];
  const seen = new Set();
  return out.filter(([, v]) => !seen.has(v) && seen.add(v));
}

test('the variant helpers make what an IME or a copy gives', () => {
  assert.equal(fullWidth('LocalSubnet'), 'ＬｏｃａｌＳｕｂｎｅｔ');
  assert.equal(halfKana('ローカルサブネット'), 'ﾛｰｶﾙｻﾌﾞﾈｯﾄ'); // ローカルサブネット → ﾛｰｶﾙｻﾌﾞﾈｯﾄ
  assert.notEqual('Sousé'.normalize('NFD'), 'Sousé');
});

for (const lang of LANGS9) {
  test(`${lang}: each settings.conn.ks.kw.* word and its full-width / half-width / NFD forms add the keyword`, () => {
    const p = pageIn(lang);
    assert.deepEqual(Object.keys(p.KW_IDS).sort(), Object.values(KEYS).sort(), 'the two keywords');
    let tried = 0;
    for (const [id, key] of Object.entries(KEYS)) {
      const w = p.word(id);
      assert.equal(p.kwShow(key), w, `${id}: shown as the catalog word`);
      for (const [how, v] of variants(w)) {
        const parts = p.split(v);
        assert.deepEqual(parts, [v], `${id} ${how} "${v}": one entry for the token field (no separator inside)`);
        assert.equal(p.ksKw(parts[0]), key, `${id} ${how} "${v}" (${[...v].map(c => c.codePointAt(0).toString(16)).join(' ')}): the ${key} keyword`);
        tried++;
      }
      // the English name in the same forms still works in every language
      for (const [how, v] of variants(key)) assert.equal(p.ksKw(v), key, `${key} ${how}: the keyword`);
    }
    assert.ok(tried >= 2);
    // still an exact match: a part of the word, or the word with more, is not the keyword
    const w = p.word('local_subnet');
    for (const v of [w.slice(0, -1), `${w}x`, 'Local', 'subnet']) assert.equal(p.ksKw(v), null, `"${v}" is not a keyword`);
    for (const k of ['DNS', 'dhcp', 'ＷＩＮＳ']) assert.ok(p.ksKw(k), `${k}: the fixed English keyword`);
  });
}

test('the cases T-P5-12 found are covered: ja half-width katakana, full-width Latin, fr NFD', () => {
  const ja = pageIn('ja_JP'), en = pageIn('en_US'), fr = pageIn('fr_FR');
  assert.equal(ja.ksKw('ﾛｰｶﾙｻﾌﾞﾈｯﾄ'), 'LocalSubnet');
  assert.equal(en.ksKw('ＬｏｃａｌＳｕｂｎｅｔ'), 'LocalSubnet');
  const nfd = fr.word('local_subnet').normalize('NFD');
  assert.ok(nfd.includes('́'), 'the fr word has an accent to split');
  assert.equal(fr.ksKw(nfd), 'LocalSubnet');
});

// The nine catalogs' words are NFC and half-width already, so the tests above cannot see whether the
// shown-word side (kwFold(kwShow(k))) is folded too. A catalog that holds the word in NFD, full-width
// or half-width katakana must still match the plain word typed in.
test('the shown-word side is folded too: a catalog word in NFD / full-width / half-width katakana', () => {
  const words = {
    'settings.conn.ks.kw.local_subnet': 'SousRéseauLocal'.normalize('NFD'),
    'settings.conn.ks.kw.default_gateway': 'ﾃﾞﾌｫﾙﾄｹﾞｰﾄｳｪｲ',
  };
  const nfd = pageWith(id => words[id]);
  assert.notEqual(nfd.word('local_subnet'), 'SousRéseauLocal', 'the injected word is NFD');
  assert.equal(nfd.ksKw('SousRéseauLocal'), 'LocalSubnet', 'NFC typed, NFD in the catalog');
  assert.equal(nfd.ksKw('sousréseaulocal'), 'LocalSubnet');
  assert.equal(nfd.ksKw('デフォルトゲートウェイ'), 'DefaultGateway', 'full-width typed, half-width katakana in the catalog');
  const wide = pageWith(id => (id === 'settings.conn.ks.kw.local_subnet' ? 'ＬＡＮ－Ｓｕｂｎｅｔｚ' : 'Standardgateway'));
  assert.equal(wide.ksKw('lan-subnetz'), 'LocalSubnet', 'half-width typed, full-width in the catalog');
  assert.equal(wide.ksKw('LAN-Subnetz'), 'LocalSubnet');
  assert.equal(wide.ksKw('ＬＡＮ－Ｓｕｂｎｅｔｚ'), 'LocalSubnet');
});
