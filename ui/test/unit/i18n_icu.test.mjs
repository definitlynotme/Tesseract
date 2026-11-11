// T-P4-03: ICU subset — parser, compiler, formatter (P4 architecture §3.2, §3.3).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadI18n } from '../fixtures/i18n/helpers.mjs';

const m = await loadI18n();
const { icu } = m;
const fmtIn = (lang, src, params) => icu.format(icu.parse(src), params, m.icuEnv(lang));

test('simple arguments, literal text and missing arguments', () => {
  assert.equal(fmtIn('zh_CN', '你好，{name}！', { name: 'Ann' }), '你好，Ann！');
  assert.equal(fmtIn('zh_CN', '无参数'), '无参数');
  assert.equal(icu.parse('plain'), 'plain', 'a plain message compiles to a string');
  const seen = [];
  const env = { ...m.icuEnv('en_US'), missing: n => { seen.push(n); return ''; } };
  assert.equal(icu.format(icu.parse('a {x} b'), {}, env), 'a  b');
  assert.deepEqual(seen, ['x']);
});

test('number, integer, percent', () => {
  assert.equal(fmtIn('en_US', '{n, number}', { n: 1234.5 }), '1,234.5');
  assert.equal(fmtIn('de_DE', '{n, number}', { n: 1234.5 }), '1.234,5');
  assert.equal(fmtIn('en_US', '{n, number, integer}', { n: 1234.56 }), '1,235');
  assert.equal(fmtIn('zh_CN', '{p, number, percent}', { p: 0.42 }), '42%');
  assert.match(fmtIn('fr_FR', '{p, number, percent}', { p: 0.42 }), /^42\s%$/, 'French: (narrow) no-break space before %');
});

test('date and time (24-hour)', () => {
  const t = Date.UTC(2026, 9, 7, 15, 4, 5);
  const local = new Date(t);
  const hh = String(local.getHours()).padStart(2, '0'), mm = String(local.getMinutes()).padStart(2, '0');
  assert.equal(fmtIn('en_US', '{t, time, short}', { t }), `${hh}:${mm}`);
  assert.equal(fmtIn('en_US', '{t, time}', { t }), `${hh}:${mm}`, 'time defaults to short');
  assert.match(fmtIn('en_US', '{t, date, short}', { t }), /^\d{1,2}\/\d{1,2}\/\d{2}$/);
  assert.match(fmtIn('de_DE', '{t, date, medium}', { t }), /^\d{2}\.\d{2}\.2026$/);
  assert.match(fmtIn('zh_CN', '{t, date}', { t }), /^2026年10月\d{1,2}日$/);
});

test('plural: zh, en, fr (0 and 1), ru one/few/many, ja, ko', () => {
  const zh = '{n, plural, other {# 个节点}}';
  for (const n of [0, 1, 2, 5]) assert.equal(fmtIn('zh_CN', zh, { n }), `${n} 个节点`);
  const en = '{n, plural, one {# node} other {# nodes}}';
  assert.equal(fmtIn('en_US', en, { n: 0 }), '0 nodes');
  assert.equal(fmtIn('en_US', en, { n: 1 }), '1 node');
  assert.equal(fmtIn('en_US', en, { n: 2 }), '2 nodes');
  const fr = '{n, plural, one {# nœud} many {# de nœuds} other {# nœuds}}';
  assert.equal(fmtIn('fr_FR', fr, { n: 0 }), '0 nœud', 'French: 0 is singular');
  assert.equal(fmtIn('fr_FR', fr, { n: 1 }), '1 nœud');
  assert.equal(fmtIn('fr_FR', fr, { n: 2 }), '2 nœuds');
  assert.match(fmtIn('fr_FR', fr, { n: 1000000 }), /^1\s000\s000 de nœuds$/, 'French many');
  const ru = '{n, plural, one {# узел} few {# узла} many {# узлов} other {# узла}}';
  assert.equal(fmtIn('ru_RU', ru, { n: 1 }), '1 узел');
  assert.equal(fmtIn('ru_RU', ru, { n: 21 }), '21 узел');
  assert.equal(fmtIn('ru_RU', ru, { n: 3 }), '3 узла');
  assert.equal(fmtIn('ru_RU', ru, { n: 5 }), '5 узлов');
  assert.equal(fmtIn('ru_RU', ru, { n: 11 }), '11 узлов');
  assert.equal(fmtIn('ru_RU', ru, { n: 1.5 }), '1,5 узла', 'fraction → other');
  assert.equal(fmtIn('ja_JP', '{n, plural, other {# 件}}', { n: 1 }), '1 件');
  assert.equal(fmtIn('ko_KR', '{n, plural, other {#개}}', { n: 1 }), '1개');
});

test('plural: exact =N first, offset, # inside nested text', () => {
  const src = '{n, plural, offset:1 =0 {nobody} =1 {only {who}} one {{who} and # other} other {{who} and # others}}';
  assert.equal(fmtIn('en_US', src, { n: 0, who: 'Ann' }), 'nobody');
  assert.equal(fmtIn('en_US', src, { n: 1, who: 'Ann' }), 'only Ann');
  assert.equal(fmtIn('en_US', src, { n: 2, who: 'Ann' }), 'Ann and 1 other');
  assert.equal(fmtIn('en_US', src, { n: 5, who: 'Ann' }), 'Ann and 4 others');
  assert.equal(fmtIn('en_US', '{n, plural, other {#}} {n, number}', { n: 1234 }), '1,234 1,234', '# is a formatted number');
  assert.equal(fmtIn('en_US', '# {n, plural, other {x}}', { n: 1 }), '# x', '# outside plural is literal');
});

test('select and nesting (≤ 3 levels)', () => {
  const src = '{mode, select, tun {TUN {n, plural, one {# rule} other {# rules}}} system {System} other {Mixed}}';
  assert.equal(fmtIn('en_US', src, { mode: 'tun', n: 1 }), 'TUN 1 rule');
  assert.equal(fmtIn('en_US', src, { mode: 'system' }), 'System');
  assert.equal(fmtIn('en_US', src, { mode: 'weird' }), 'Mixed');
  assert.equal(fmtIn('en_US', src, {}), 'Mixed', 'missing select value → other');
  const three = '{a, select, x {{b, select, y {{c, plural, other {deep #}}} other {b}}} other {a}}';
  assert.equal(fmtIn('en_US', three, { a: 'x', b: 'y', c: 3 }), 'deep 3');
});

test('quoting: doubled apostrophe, quoted braces, quoted #, quoted <, lone apostrophe', () => {
  assert.equal(fmtIn('en_US', "It''s {x}", { x: 'ok' }), "It's ok");
  assert.equal(fmtIn('en_US', "'{literal}' and '{'{x}'}'", { x: 1 }), '{literal} and {1}');
  assert.equal(fmtIn('en_US', "{n, plural, other {'#' is #}}", { n: 2 }), '# is 2');
  assert.equal(fmtIn('en_US', "use '<b>' tags", {}), 'use <b> tags');
  assert.equal(fmtIn('en_US', "l'avion {x}", { x: 1 }), "l'avion 1");
  assert.equal(fmtIn('en_US', 'a < b and 1<2', {}), 'a < b and 1<2', '< not followed by a letter is text');
});

test('unsupported syntax fails at compile time with line:col and file:id', () => {
  const err = (src, re) => assert.throws(() => icu.parse(src), e => e.name === 'IcuSyntaxError' && re.test(e.message), src);
  err('{n, selectordinal, other {#th}}', /"selectordinal" is not supported \(at 1:5\)/);
  err('{n, spellout}', /"spellout" is not supported/);
  err('{n, number, ::currency/USD}', /skeletons are not supported/);
  err('{n, number, currency}', /unsupported number style "currency"/);
  err('{d, date, yyyy-MM-dd}', /unsupported date style/);
  err('{t, time, medium}', /unsupported time style/);
  err('{n, plural, one {x}}', /plural needs an "other" case/);
  err('{n, plural, fewer {x} other {y}}', /unknown plural category "fewer"/);
  err('{s, select, a {x} a {y} other {z}}', /duplicate select case "a"/);
  err('{s, select, __proto__ {x} other {z}}', /"__proto__" is not allowed as a select case key \(at 1:13\)/);
  // other prototype-ish names are ordinary cases (lookups use hasOwnProperty)
  assert.equal(icu.format(icu.parse('{s, select, constructor {C} other {O}}'), { s: 'constructor' }, { plural: () => 'other' }), 'C');
  assert.equal(icu.format(icu.parse('{s, select, x {X} other {O}}'), { s: 'toString' }, { plural: () => 'other' }), 'O');
  err('{x',/expected "," but the message ended/);
  err('a } b', /unmatched "}" \(at 1:3\)/);
  err("'{oops", /unterminated quote \(at 1:1\)/);
  err('line one\n{n, foo}', /unknown argument type "foo" \(at 2:5\)/);
  err('{a, select, x {{b, select, y {{c, select, z {{d, select, other {}}} other {}}} other {}}} other {}}', /nested deeper than 3 levels/);
  assert.throws(() => icu.compile('{n, plural, one {x}}', 'src/i18n/locales/en_US/common.json:common.items'),
    e => e.message === 'src/i18n/locales/en_US/common.json:common.items: plural needs an "other" case (at 1:1)' && e.line === 1 && e.col === 1);
});

test('tags: whitelist only, no attributes, no self-closing, closed', () => {
  const parts = icu.formatParts(icu.parse('按 <kbd>Ctrl K</kbd> 或 <b>{x}</b>'), { x: '<i>' }, m.icuEnv('zh_CN'));
  assert.deepEqual(parts, ['按 ', { tag: 'kbd', kids: ['Ctrl K'] }, ' 或 ', { tag: 'b', kids: ['<i>'] }]);
  for (const t of ['b', 'em', 'code', 'kbd', 'mono', 'tier', 'link', 'x']) assert.doesNotThrow(() => icu.parse(`<${t}>a</${t}>`), t);
  const err = (src, re) => assert.throws(() => icu.parse(src), re, src);
  err('<script>alert(1)</script>', /unknown tag <script>/);
  err('<img src=x>', /unknown tag <img>/);
  err('<b class="x">a</b>', /tag <b> must not have attributes/);
  err('<b onclick=alert(1)>a</b>', /must not have attributes/);
  err('<x/>', /self-closing tag <x\/> is not supported/);
  err('<b>never closed', /unclosed tag <b>/);
  err('<b>a</em>', /unclosed tag <b>/);
  err('a</b>', /closing tag without an opening tag/);
  err('{n, plural, other {<b>#}}', /unclosed tag <b>/);
  assert.deepEqual(icu.analyze(icu.parse('<b>{a}</b> {n, plural, other {<tier>#</tier>}} {s, select, x {} other {}}')),
    { args: { a: new Set(['text']), n: new Set(['plural']), s: new Set(['select']) }, tags: new Set(['b', 'tier']), selects: { s: new Set(['x', 'other']) }, plurals: [{ name: 'n', keys: ['other'] }] });
});

test('partsToText drops tags and keeps their words (what T.t returns)', () => {
  const parts = icu.formatParts(icu.parse('显示 <b>{n, number}</b> / {total, number} 条'), { n: 3, total: 1200 }, m.icuEnv('zh_CN'));
  assert.equal(icu.partsToText(parts), '显示 3 / 1,200 条');
  const rt = m.createI18n({});
  rt.register('zh_CN', { 'logs.footer.shown': icu.parse('显示 <b>{n, number}</b> / {total, number} 条') });
  assert.equal(rt.t('logs.footer.shown', { n: 3, total: 1200 }), '显示 3 / 1,200 条');
});

test('mapText touches literal text only', () => {
  const msg = icu.parse('ab {x} <b>cd</b> {n, plural, other {# ef}}');
  const up = icu.mapText(msg, s => s.toUpperCase());
  assert.equal(icu.format(up, { x: 'q', n: 2 }, m.icuEnv('en_US')), 'AB q CD 2 EF');
});
