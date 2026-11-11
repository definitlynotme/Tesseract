// T-P4-07: the core API after the injection close-out (P4 architecture §7) and the hard gates.
//
// Node only (no browser):
//   - the inject lint follows the helpers through the names they are bound to (决定_第三波裁定
//     T-P4-01 item 6): `const {h} = T; h(…, {html})`, `const {h: mk} = T`, `const h = T.h`,
//     `const U = T.ui; U.popover(a, \`…${x}\`)`, nested patterns, `const R = T.raw`, an html key
//     nested in an argument (`Object.assign({html: true}, o)`); a name bound to something else is
//     not a helper;
//   - tools/inject_allow.json keeps only the T.html and T.icon implementations, every entry with
//     a reason and in use; the real tree has no finding left;
//   - the demo gate's source and import parts (T.sim / T.D / sim: / window.demo / scenario( /
//     @i18n-pending and the demo class tokens in ui/src and ui/css outside design/, design.css;
//     the app entry's imports reaching design/), and the features gate's `in-demo` check;
//   - gate self-test on a temporary copy of ui/ (src, css, tools; node_modules linked): the copy
//     passes; each planted violation fails the gate that `npm run check` runs for it — an
//     interpolated innerHTML (scan.mjs inject), a T.sim residue and an app CSS .demo-link
//     (build.mjs --check: the in-memory app bundle's demo gate), an unregistered
//     T.rt.feature('B-99') (scan.mjs features).
// Fix round 1 adds: T.html in any call form, setHTML(Unsafe) / parseHTMLUnsafe, Object.assign /
// Reflect.set to innerHTML, doc.write through a document alias, T.h on* / srcdoc, T.raw(I(…)) only
// for an I bound to T.icon; the demo gate decoding \uXXXX (a demo word or a deleted message id in
// the zh_CN catalog fails the locale chunk), design-only hook exemptions, require(); features per
// page (no_control, call-other-page); the check steps as child processes on the copy; the
// malicious fixtures through validate_ui_fixtures.py.
// In the browser (local Chrome, the design target built in memory, fixture mode, no network;
// skipped when Chrome is not installed): T.h({html}) and swapText({html:true}) throw; a string
// given to swapText, T.ui.popover, T.drawer / T.sheet content and head, the T.ui builders
// (status, tech, dots, tierTag, callout, empty, menu, confirm) is text; nothing runs, no <img>.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync, appendFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { CHROME, UI, chromeAvailable } from '../fixtures/i18n/helpers.mjs';
import { buildTarget, check, demoFindings, writeOutput } from '../../build.mjs';
import {
  UI_ROOT, checkFeatures, featurePageOf, findFeatureCalls, lineDigest, loadDemoMarkers, readFeatureRegistry, runRule,
  scanAppImports, scanDemoSources, scanDemoText, scanInjectJs, summarize,
} from '../../tools/scan.mjs';

const checks = src => scanInjectJs(src).map(f => f.check);

/* ------------------------------------------------------------------ lint: aliases */
test('inject lint: helpers reached through destructured and assigned names', () => {
  assert.deepEqual(checks('const { h } = T; h("div", { html: x });'), ['helper-html']);
  assert.deepEqual(checks('const { h: mk } = T; mk("p", { html: "<b>" });'), ['helper-html']);
  assert.deepEqual(checks('const h = T.h; h("p", { html: s });'), ['helper-html']);
  assert.deepEqual(checks('const T = window.T; const { h } = T; h("p", { html: s });'), ['helper-html']);
  assert.deepEqual(checks('const { h } = window.T; h("p", { html: s });'), ['helper-html']);
  assert.deepEqual(checks('const U = T.ui; U.popover(a, `<b>${x}</b>`);'), ['helper-string']);
  assert.deepEqual(checks('const { ui: { popover } } = window.T; popover(a, `<b>${x}</b>`);'), ['helper-string']);
  assert.deepEqual(checks('const { drawer } = T; drawer({ content: `<p>${x}</p>` });'), ['helper-string']);
  assert.deepEqual(checks('let n; n = T.notify; n({ message: "a" + x });'), ['helper-string']);
  assert.deepEqual(checks('const R = T.raw; R(x);'), ['raw']);
  assert.deepEqual(checks('const { raw } = T; raw(y);'), ['raw']);
  assert.deepEqual(checks('const I = n => T.icon(n); const { raw } = T; raw("<b>ok</b>"); raw(T.icon("x")); raw(I("x"));'), []);
  assert.deepEqual(checks('const M = T.motion; const sw = M.swapText; sw(el, x, Object.assign({ html: true }, o));'), ['helper-html']);
  assert.deepEqual(checks('M.swapText(el, x, { html: true });'), ['helper-html']);
  // an alias of an alias
  assert.deepEqual(checks('const A = T; const B = A.ui; const { popover: p } = B; p(a, `${x}`);'), ['helper-string']);
  // a name bound to something that is not T is not a helper
  assert.deepEqual(checks('const { h } = other; h("p", { html: s }); const raw = String.raw; raw`x${y}`;'), []);
  // an html key inside a function argument (a callback's own objects) is not the helper's
  assert.deepEqual(checks('T.drawer({ content: box => { make({ html: 1 }); } });'), []);
});

test('inject lint: the other sinks', () => {
  assert.deepEqual(checks('el.innerHTML = `<b>${x}</b>`;'), ['html-sink']);
  assert.deepEqual(checks('el.innerHTML += x;'), ['html-sink']);
  assert.deepEqual(checks('el.innerHTML = "<span></span>";'), []);
  assert.deepEqual(checks('range.createContextualFragment(s); new DOMParser().parseFromString(s, "text/html");'), ['html-sink', 'html-sink']);
  assert.deepEqual(checks('frame.srcdoc = s; frame.setAttribute("srcdoc", s);'), ['html-sink', 'on-attr']);
  assert.deepEqual(checks('el.setAttribute("onclick", s);'), ['on-attr']);
  assert.deepEqual(checks('setTimeout("x()", 1); window.eval(s); new Function(s);'), ['eval', 'eval', 'eval']);
  // T-P4-07 fix round 1: the HTML-parsing APIs, writes through Object.assign / Reflect.set, and
  // document.write through a name bound to document (core.js has `const doc = document`)
  assert.deepEqual(checks('el.setHTMLUnsafe(x); el.setHTML(x); Document.parseHTMLUnsafe(x);'), ['html-sink', 'html-sink', 'html-sink']);
  assert.deepEqual(checks('el.setHTMLUnsafe("<b></b>");'), []);
  assert.deepEqual(checks('Object.assign(el, { innerHTML: x }); Object.assign(el, { outerHTML: `<i>${x}</i>` }); Reflect.set(el, "innerHTML", x);'), ['html-sink', 'html-sink', 'html-sink']);
  assert.deepEqual(checks('Object.assign(el, { innerHTML: "" }, { className: x }); Reflect.set(el, "textContent", x);'), []);
  assert.deepEqual(checks('const doc = document; doc.write(x); window.document.writeln(x); document.write(x);'), ['html-sink', 'html-sink', 'html-sink']);
  assert.deepEqual(checks('const d2 = other; d2.write(x); stream.write(x);'), [], 'write on something that is not document');
});

test('inject lint: T.html is a template tag only (any call form is an html-sink)', () => {
  assert.deepEqual(checks('const n = T.html`<b>${x}</b>`;'), []);
  for (const code of [
    'T.html([s]);',
    'T.html.call(null, [s]);',
    'T.html.apply(null, [[s]]);',
    'const f = T.html.bind(null);',
    'Reflect.apply(T.html, null, [[s]]);',
    'rows.map(T.html);',
    'const { html } = T; html([s]);',
    'const H = window.T.html; H.call(0, [s]);',
    'new T.html([s]);',
  ]) assert.deepEqual(checks(code), ['html-sink'], code);
});

test('inject lint: T.h on* / srcdoc attributes; T.raw(I(…)) only when I is bound to T.icon', () => {
  assert.deepEqual(checks("T.h('a', { onclick: s });"), ['on-attr']);
  assert.deepEqual(checks("const { h } = T; h('a', { onClick: handler });"), ['on-attr'], 'a value that is not a function literal');
  assert.deepEqual(checks("T.h('iframe', { srcdoc: s }); T.h('iframe', { srcdoc: '<b></b>' });"), ['html-sink', 'html-sink']);
  assert.deepEqual(checks("T.h('a', { onClick: () => go(), onkeydown: function (e) {}, on: { click: f } });"), [], 'function literals and the on map');
  assert.deepEqual(checks('T.raw(I(x));'), ['raw'], 'an I that is not bound to T.icon');
  assert.deepEqual(checks('const I = (n, s, c) => T.icon(n, s || 16, c); T.raw(I(x));'), []);
  assert.deepEqual(checks('const I = T.icon; T.raw(I(x)); function J(n) { return T.icon(n); } T.raw(J(n));'), ['raw'], 'a function declaration is not followed');
  assert.deepEqual(checks('const I = n => other(n); T.raw(I(x));'), ['raw']);
});

/* ------------------------------------------------------------------ allow list, real tree */
test('inject_allow.json: only the T.html and T.icon implementations, each with a reason and in use; no finding left', () => {
  const allow = JSON.parse(readFileSync(join(UI_ROOT, 'tools', 'inject_allow.json'), 'utf8')).entries;
  assert.deepEqual(allow.map(a => `${a.file}:${a.check}`).sort(), ['src/core/core.js:html-sink', 'src/core/icons.js:raw']);
  for (const a of allow) {
    assert.ok(typeof a.reason === 'string' && a.reason.length > 40, `${a.file}: a reason`);
    assert.match(a.line_sha256, /^[0-9a-f]{16}$/);
    const lines = readFileSync(join(UI_ROOT, a.file), 'utf8').split('\n');
    assert.ok(lines.some(l => lineDigest(l) === a.line_sha256), `${a.file}: the allowed line is still there`);
  }
  const r = runRule('inject');
  assert.equal(r.findings.length, 0, JSON.stringify(r.findings));
  assert.equal(r.allowed.length, allow.length, 'every entry is in use');
  // the T.html one is the template's innerHTML, the T.icon one mints the icon's Raw
  assert.match(readFileSync(join(UI_ROOT, 'src', 'core', 'core.js'), 'utf8'), /const t = doc\.createElement\('template'\);\n\s*t\.innerHTML = out\.trim\(\);/);
});

test('the real tree: demo (sources, imports), features and inject gates are clean', () => {
  assert.deepEqual(scanDemoSources(UI_ROOT), []);
  assert.deepEqual(scanAppImports(UI_ROOT), []);
  assert.deepEqual(runRule('features').findings, []);
  const gates = JSON.parse(readFileSync(join(UI_ROOT, 'tools', 'scan_gates.json'), 'utf8'));
  assert.equal(gates.demo, 'enforce');
  assert.equal(gates.inject, 'enforce');
  assert.equal(gates.features, 'enforce');
  for (const p of ['overview', 'tiers', 'logs', 'settings', 'shell']) assert.ok(gates.features_pages.includes(p), `features_pages: ${p}`);
  for (const f of ['alias.js', 'legacy_sim.js']) assert.equal(existsSync(join(UI_ROOT, 'src', 'rt', f)), false, `src/rt/${f} is deleted`);
});

/* ------------------------------------------------------------------ demo: sources and imports */
function miniRoot() {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-demo-src-'));
  for (const d of ['src/entry', 'src/pages', 'src/design', 'src/rt', 'src/i18n/locales/zh_CN', 'css', 'tools']) mkdirSync(join(root, ...d.split('/')), { recursive: true });
  cpSync(join(UI_ROOT, 'tools', 'demo_markers.json'), join(root, 'tools', 'demo_markers.json'));
  writeFileSync(join(root, 'src', 'entry', 'app.js'), "import '../pages/a.js';\n");
  writeFileSync(join(root, 'src', 'pages', 'a.js'), 'export const a = 1;\n');
  writeFileSync(join(root, 'src', 'design', 'demo.js'), 'T.sim.scenario("x"); window.demo = {}; // sim: @i18n-pending\n');
  writeFileSync(join(root, 'src', 'i18n', 'locales', 'zh_CN', 'x.js'), 'T.sim;\n');
  writeFileSync(join(root, 'css', 'design.css'), '.demo-link{color:red}\n');
  writeFileSync(join(root, 'css', 'app.css'), '/* .demo-link is in design.css */ .btn{}\n');
  return root;
}

test('demo gate, sources part: markers outside design/ and design.css; CSS comments are not selectors', () => {
  const root = miniRoot();
  try {
    assert.deepEqual(scanDemoSources(root), [], 'design/, locales and design.css are exempt; a CSS comment is not a selector');
    const page = join(root, 'src', 'pages', 'a.js');
    const cases = [
      ['if (__DEMO__) T.sim.start();', 'T.sim'],
      ['T.on("sim:log", f);', 'sim:'],
      ['window.demo = 1;', 'window.demo'],
      ['x.scenario("fallback");', 'scenario('],
      ['// @i18n-pending\n', '@i18n-pending'],
      ['const n = T.D.nodes;', '/\\bT\\.D\\b/'],
      ['el.className = "demo-link";', 'demo-link'],
    ];
    for (const [code, marker] of cases) {
      writeFileSync(page, code + '\n');
      const f = scanDemoSources(root);
      assert.deepEqual(f.map(x => [x.file, x.marker, x.check]), [['src/pages/a.js', marker, 'source']], code);
    }
    writeFileSync(page, 'T.data.tier("vanguard"); T.Data; const simulated = 1;\n');
    assert.deepEqual(scanDemoSources(root), [], 'T.data, T.Data and words that only contain "sim" are fine');
    writeFileSync(join(root, 'css', 'app.css'), '.btn{} #ov-demo{}\n');
    assert.deepEqual(scanDemoSources(root).map(x => [x.file, x.marker]), [['css/app.css', '.ov-demo']]);
    writeFileSync(join(root, 'css', 'app.css'), '/* @i18n-pending */\n.btn{}\n');
    assert.deepEqual(scanDemoSources(root).map(x => [x.file, x.marker]), [['css/app.css', '@i18n-pending']]);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('demo gate, imports part: the app entry reaches nothing under src/design/ (static, re-export, dynamic)', () => {
  const root = miniRoot();
  try {
    assert.deepEqual(scanAppImports(root), []);
    const page = join(root, 'src', 'pages', 'a.js');
    for (const code of ["import '../design/demo.js';", "export { x } from '../design/demo.js';", "export * from '../design/demo.js';", "import('../design/demo.js');"]) {
      writeFileSync(page, code + '\n');
      assert.deepEqual(scanAppImports(root).map(f => [f.file, f.marker, f.check]), [['src/pages/a.js', 'imports src/design/demo.js', 'app-import']], code);
    }
    // a file only the design entry imports does not count
    writeFileSync(page, 'export const a = 1;\n');
    writeFileSync(join(root, 'src', 'entry', 'design.js'), "import '../design/demo.js';\n");
    assert.deepEqual(scanAppImports(root), []);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('demo gate: JS is matched after decoding \\uXXXX / \\\\uXXXX / \\u{…} (locale chunks carry the catalog as a string in a string)', () => {
  const markers = loadDemoMarkers(UI_ROOT);
  const esc = s => [...s].map(c => '\\u' + c.codePointAt(0).toString(16).padStart(4, '0')).join('');
  const plain = scanDemoText('x = "原型演示"', 'a.js', markers).map(f => f.marker);
  assert.ok(plain.includes('原型演示'));
  for (const text of [`x = "${esc('原型演示')}"`, `x = JSON.parse("{\\"k\\":\\"${esc('原型演示').replace(/\\/g, '\\\\')}\\"}")`, 'x = "\\u{539f}\\u{578b}\\u{6f14}\\u{793a}"']) {
    assert.ok(scanDemoText(text, 'locale-zh_CN.X.js', markers).some(f => f.marker === '原型演示'), text);
  }
  // the deleted message-id prefixes, escaped or not
  assert.deepEqual(scanDemoText(`{"overview.demo.tip":1,"${esc('shell.demo.group')}":2}`, 'l.js', markers).map(f => f.marker).sort(), ['overview.demo.', 'shell.demo.']);
  // a decoded line break stays escaped: line numbers are those of the file
  assert.equal(scanDemoText('a\\u000a\\u000ab\n"原型演示"', 'l.js', markers).find(f => f.marker === '原型演示').line, 2);
  // CSS is not decoded
  assert.deepEqual(scanDemoText('.a{content:"\\539f"}', 'a.css', markers), []);
});

test('demo gate, sources part: design-only hook files are exempt by name, with a reason, and only while the app entry does not reach them', () => {
  const root = miniRoot();
  try {
    mkdirSync(join(root, 'src', 'boot'), { recursive: true });
    const hook = join(root, 'src', 'boot', 'rt-design.js');
    writeFileSync(hook, "const sim = 1; T.on('sim:log', f); T.sim.start();\n");
    const mp = join(root, 'tools', 'demo_markers.json');
    const m = JSON.parse(readFileSync(mp, 'utf8'));
    m.source.design_only = [];
    writeFileSync(mp, JSON.stringify(m));
    assert.deepEqual([...new Set(scanDemoSources(root).map(f => f.file))], ['src/boot/rt-design.js'], 'not exempt without an entry');
    m.source.design_only = [{ file: 'src/boot/rt-design.js', reason: 'design entry only' }];
    writeFileSync(mp, JSON.stringify(m));
    assert.deepEqual(scanDemoSources(root), []);
    // reached from the app entry → a finding, whatever the exemption says
    writeFileSync(join(root, 'src', 'pages', 'a.js'), "import '../boot/rt-design.js';\n");
    assert.deepEqual(scanDemoSources(root).map(f => [f.file, f.marker]), [['src/boot/rt-design.js', 'design_only file reached from the app entry']]);
    writeFileSync(join(root, 'src', 'pages', 'a.js'), 'export const a = 1;\n');
    m.source.design_only = [{ file: 'src/boot/rt-design.js', reason: ' ' }];
    writeFileSync(mp, JSON.stringify(m));
    assert.deepEqual(scanDemoSources(root).map(f => f.marker), ['design_only entry without a reason']);
  } finally { rmSync(root, { recursive: true, force: true }); }
  // the real list: each entry has a reason and is outside the app's import graph (checked above
  // on the real tree by the clean scanDemoSources)
  const real = loadDemoMarkers(UI_ROOT).source.design_only;
  assert.deepEqual(real.map(e => e.file).sort(), ['src/boot/data-design.js', 'src/boot/design.js', 'src/boot/rt-design.js', 'src/entry/design.js']);
  for (const e of real) assert.ok(e.reason.length > 30, e.file);
});

test('demo gate, imports part: a literal require() counts like an import', () => {
  const root = miniRoot();
  try {
    writeFileSync(join(root, 'src', 'pages', 'a.js'), "const d = require('../design/demo.js');\n");
    assert.deepEqual(scanAppImports(root).map(f => [f.file, f.marker]), [['src/pages/a.js', 'imports src/design/demo.js']]);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('features gate: references are checked per page; no_control exempts an entry; a call on the wrong page is a finding', () => {
  assert.equal(featurePageOf('src/pages/tiers.js'), 'tiers');
  assert.equal(featurePageOf('src/pages/settings/alerts_form.js'), 'settings');
  assert.equal(featurePageOf('src/rt/project_overview.js'), 'overview');
  assert.equal(featurePageOf('src/shellc/quit.js'), 'shell');
  assert.equal(featurePageOf('src/core/app.js'), 'shell');
  assert.equal(featurePageOf('src/core/core.js'), null);
  const reg = [{ name: 'B-01', page: 'overview' }, { name: 'B-01', page: 'tiers' }, { name: 'B-07', page: 'tiers', no_control: 'implements the fallback directly' }, { name: 'B-50', page: 'shell' }];
  const calls = (file, src) => findFeatureCalls(src, file);
  const run = cs => checkFeatures({ registry: reg, enabled: [], calls: cs, pages: ['overview', 'tiers', 'shell'] }).map(f => [f.check, f.text]);
  // B-01 used on the overview only: the tiers entry is unreferenced (a same-name call elsewhere does not count)
  assert.deepEqual(run([...calls('src/pages/overview.js', "T.rt.feature('B-01')"), ...calls('src/shellc/x.js', "T.rt.feature('B-50')")]), [['unreferenced', 'B-01 (tiers)']]);
  assert.deepEqual(run([...calls('src/pages/overview.js', "T.rt.feature('B-01')"), ...calls('src/pages/tiers/a.js', "T.rt.feature('B-01')"), ...calls('src/core/app.js', "T.rt.feature('B-50')")]), []);
  // the shell's entries are checked against shellc/** and core/app.js
  assert.deepEqual(run([...calls('src/pages/overview.js', "T.rt.feature('B-01')"), ...calls('src/pages/tiers.js', "T.rt.feature('B-01')")]), [['unreferenced', 'B-50 (shell)']]);
  // a call in tiers.js of a name registered for the shell only
  assert.deepEqual(run([...calls('src/pages/overview.js', "T.rt.feature('B-01')"), ...calls('src/pages/tiers.js', "T.rt.feature('B-01'); T.rt.feature('B-50')"), ...calls('src/core/app.js', "T.rt.feature('B-50')")]),
    [['call-other-page', 'B-50 is registered for shell, not tiers']]);
  // the real registry: the two entries T-P4-05b accepted without gated code carry their reason,
  // and so do the settings entries whose controls the 10-30 alert redesign removed, B-28 (the
  // compatibility rules' 恢复默认, gone with the purged stage on 11-01) and B-24 (the 数据目录 row,
  // gone with the 11-04 settings cleanup)
  const ex = readFeatureRegistry(UI_ROOT).filter(r => r.no_control).map(r => `${r.name}/${r.page}`).sort();
  assert.deepEqual(ex, ['B-01/tiers', 'B-07/tiers', 'B-23/settings', 'B-24/settings', 'B-28/settings', 'B-31/settings', 'B-32/settings',
    'B-33/settings', 'B-34/settings', 'B-35/settings', 'B-36/settings']);
});

test('features gate: a B item under __DEMO__ is a finding (then-branch / && only)', () => {
  const reg = [{ name: 'B-22', page: 'logs' }];
  const run = src => checkFeatures({ registry: reg, enabled: [], calls: findFeatureCalls(src, 'x.js'), pages: [] }).map(f => f.check);
  assert.deepEqual(run("if (__DEMO__) { if (T.rt.feature('B-22')) a(); }"), ['in-demo']);
  assert.deepEqual(run("const c = __DEMO__ && T.rt.feature('B-22');"), ['in-demo']);
  assert.deepEqual(run("const c = __DEMO__ ? T.rt.feature('B-22') : 0;"), ['in-demo']);
  assert.deepEqual(run("if (__DEMO__) a(); else if (T.rt.feature('B-22')) b();"), []);
  assert.deepEqual(run("if (T.rt.feature('B-22')) a();"), []);
  assert.deepEqual(run("if (T.rt.feature('B-99')) a();"), ['call-unregistered']);
});

/* ------------------------------------------------------------------ gate self-test on a copy */
function copyUi() {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-gates-'));
  for (const d of ['src', 'css', 'tools', 'build', 'fonts', 'build.mjs', 'package.json']) cpSync(join(UI_ROOT, d), join(root, d), { recursive: true });
  symlinkSync(join(UI_ROOT, 'node_modules'), join(root, 'node_modules'), 'junction');
  return root;
}
// The first two steps of `npm run check` (tools/check.mjs), run as child processes on the copy:
// build.mjs --check (the in-memory app bundle's demo gate) and scan.mjs on the source rules. The
// remaining steps (gen_rt_spec, validate_ui_fixtures, i18n_check) read the repository outside
// ui/, so a copy cannot run them; the exit status of these two is what fails `npm run check`.
const CHECK_STEPS = {
  build: root => [join(root, 'build.mjs'), '--check'],
  scan: root => [join(root, 'tools', 'scan.mjs'), '--rule', 'cjk,inject,style,features'],
};
const runStep = (root, step) => spawnSync(process.execPath, CHECK_STEPS[step](root), { cwd: root, encoding: 'utf8' }).status;
const editFile = (root, rel, fn) => { const p = join(root, ...rel.split('/')); writeFileSync(p, fn(readFileSync(p, 'utf8'))); };
const OVERVIEW = 'src/pages/overview.js';
const plant = code => s => s.replace("  let deckExt = null;", `  let deckExt = null;\n  window.__planted = ${code};`);

test('gate self-test: each planted violation fails its gate; the clean copy passes', async () => {
  const root = copyUi();
  const pristine = readFileSync(join(root, ...OVERVIEW.split('/')), 'utf8');
  const reset = () => writeFileSync(join(root, ...OVERVIEW.split('/')), pristine);
  const appDemo = async () => demoFindings((await buildTarget({ target: 'app', root })).files, root);
  try {
    assert.ok(pristine.includes('  let deckExt = null;'), 'the planting point exists');
    // the copy is clean
    assert.equal(summarize(runRule('inject', { root })).count, 0);
    assert.equal(summarize(runRule('features', { root })).count, 0);
    assert.deepEqual(await appDemo(), []);
    assert.equal(runStep(root, 'scan'), 0, 'check step scan.mjs passes on the clean copy');
    assert.equal(runStep(root, 'build'), 0, 'check step build.mjs --check passes on the clean copy');
    // 1. an interpolated innerHTML → scan.mjs inject (enforce)
    editFile(root, OVERVIEW, plant('el => { el.innerHTML = `<b>${location.hash}</b>`; }'));
    assert.deepEqual(runRule('inject', { root }).findings.map(f => [f.file, f.check]), [[OVERVIEW, 'html-sink']]);
    assert.equal(runStep(root, 'scan'), 1, 'npm run check: scan.mjs exits 1');
    reset();
    // 1b. T.html called as a plain function (fix round 1)
    editFile(root, OVERVIEW, plant('() => T.html([location.hash])'));
    assert.deepEqual(runRule('inject', { root }).findings.map(f => [f.file, f.check, f.text]), [[OVERVIEW, 'html-sink', 'T.html(<not a tagged template>)']]);
    reset();
    // 2. a T.sim residue → the app bundle's demo gate (build.mjs --check) and the sources part
    editFile(root, OVERVIEW, plant('() => T.sim.start()'));
    const d2 = await appDemo();
    assert.ok(d2.some(f => /^app\.[A-Z0-9]+\.js$/.test(f.file)), `bundle finding: ${JSON.stringify(d2)}`);
    assert.ok(d2.some(f => f.file === OVERVIEW && f.marker === 'T.sim'), 'source finding');
    // the full in-memory gate of build.mjs --check fails on it (demo is enforce)
    const problems = await check({ root });
    assert.ok(problems.includes('app: build-manifest.demo_free is false') && problems.some(p => /^app: demo markers in bundle/.test(p)), JSON.stringify(problems));
    assert.equal(runStep(root, 'build'), 1, 'npm run check: build.mjs --check exits 1');
    reset();
    // 3. .demo-link in an app style sheet → the app bundle's CSS
    appendFileSync(join(root, 'css', 'components.css'), '\n.demo-link { color: red; }\n');
    const d3 = await appDemo();
    assert.ok(d3.some(f => /^app\.[A-Z0-9]+\.css$/.test(f.file) && f.marker === '.demo-link'), `css bundle finding: ${JSON.stringify(d3)}`);
    cpSync(join(UI_ROOT, 'css', 'components.css'), join(root, 'css', 'components.css'));
    // 4. an unregistered feature name → scan.mjs features (enforce)
    editFile(root, OVERVIEW, plant("() => T.rt.feature('B-99')"));
    assert.deepEqual(runRule('features', { root }).findings.map(f => [f.check, f.text]), [['call-unregistered', 'B-99']]);
    reset();
    // 5. a demo word or a deleted message id in the zh_CN catalog → the locale chunk of the app
    //    bundle (escaped there: the gate decodes \uXXXX first; fix round 1)
    const cat = join(root, 'src', 'i18n', 'locales', 'zh_CN', 'overview.json');
    const catPristine = readFileSync(cat, 'utf8');
    const cj = JSON.parse(catPristine);
    writeFileSync(cat, JSON.stringify({ 'overview.demo.fallback': { m: '原型演示', _desc: 'planted' }, ...cj }, null, 2) + '\n');
    const d5 = await appDemo();
    const loc = d5.filter(f => /^locale-zh_CN\.[A-Z0-9]+\.js$/.test(f.file)).map(f => f.marker).sort();
    assert.deepEqual(loc, ['overview.demo.', '原型演示'], JSON.stringify(d5));
    writeFileSync(cat, catPristine);
    assert.deepEqual(await appDemo(), [], 'clean again');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

/* ------------------------------------------------------------------ the malicious fixtures follow the contract */
// `npm run check` validates ui/test/fixtures/scenarios only (check.mjs passes no path); the
// malicious set is validated here so a contract change that leaves it behind fails the unit run
// (T-P4-07 fix round 1; whether check.mjs should name the directory is for Claude to rule).
const REPO = resolve(UI_ROOT, '..');
const PY = process.env.TESSERACT_UI_PYTHON || [join(REPO, '.venv', 'Scripts', 'python.exe'), join(REPO, '.venv', 'bin', 'python')].find(p => existsSync(p));
test('the malicious fixtures pass validate_ui_fixtures.py (bridge_protocol + runtime invariants)', { skip: !PY && 'no .venv python' }, () => {
  const r = spawnSync(PY, ['-B', join(UI_ROOT, 'tools', 'validate_ui_fixtures.py'), join(UI_ROOT, 'test', 'fixtures', 'malicious')],
    { cwd: REPO, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /OK: 10 file\(s\), \d+ frame\(s\), 0 problem\(s\)/);
});

/* ------------------------------------------------------------------ browser: the API itself */
const skip = !chromeAvailable() && 'local Chrome not found';
let dir, browser, designUrl;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-inject-api-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  designUrl = pathToFileURL(join(dir, 'design', 'index.html')).href;
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

test('the closed-out API: html options throw, strings are text, nothing runs', { skip }, async () => {
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
  await context.route(/^(?!file:)/, r => r.abort());
  const scenario = readFileSync(join(UI, 'test', 'fixtures', 'scenarios', 'rt_connect_replay.json'), 'utf8');
  await context.addInitScript(json => { window.__TESSERACT_FIXTURE__ = json; }, scenario);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(designUrl + '#overview');
  await page.waitForFunction(() => window.T && T.i18n && T.store.get('lang') === 'zh_CN' && T.router.current);
  const P = '<img src="x" onerror="window.__pwned=1"><script>window.__pwned=2</script>{n, plural, one {#} other {x}} \'q\' #';
  const r = await page.evaluate(async P => {
    const out = {};
    const threw = fn => { try { fn(); return 'no throw'; } catch (e) { return e instanceof TypeError ? 'TypeError' : String(e); } };
    out.h = threw(() => T.h('div', { html: P }));
    out.hNull = threw(() => T.h('div', { html: null }));
    // T-P4-07 fix round 1: T.html is a tag only; T.h refuses string handlers and srcdoc
    out.htmlCalls = [() => T.html([P]), () => T.html.call(null, [P]), () => Reflect.apply(T.html, null, [Object.assign([P], { raw: [P] })]),
      () => T.html(Object.assign([P], { raw: [P] }))].map(threw);
    out.hOn = [() => T.h('a', { onclick: 'window.__pwned=7' }), () => T.h('a', { onClick: 'window.__pwned=7' }), () => T.h('iframe', { srcdoc: P })].map(threw);
    let clicked = 0;
    const btn = T.h('button', { onclick: () => { clicked++; } });
    btn.click();
    out.hOnFn = [clicked, btn.hasAttribute('onclick')];
    // an ICU argument value is never parsed as ICU or markup: the {code} of a real message
    out.icu = T.t('shell.sidecar.unrecoverable.body', { code: P });
    const el = document.createElement('span'); document.body.append(el);
    out.swapHtml = threw(() => T.motion.swapText(el, P, { html: true }));
    T.motion.swapText(el, P, { instant: true });
    out.swapText = el.textContent;
    T.motion.swapText(el, T.html`<b>${P}</b>`, { instant: true });
    out.swapNode = el.querySelector('b') ? el.querySelector('b').textContent : null;
    const pop = T.ui.popover(document.querySelector('#cmd-open'), P);
    out.pop = pop.el.textContent; pop.close(null);
    const dr = T.drawer({ title: P, content: P, head: P, modal: false });
    out.drawer = [dr.body.textContent, dr.head.textContent, dr.el.querySelector('.ovl-title').textContent]; dr.close();
    const sh = T.sheet({ title: 'x', content: P, actions: [{ label: P }] });
    out.sheet = [sh.body.textContent, sh.foot.textContent]; sh.close();
    out.status = T.ui.status('ok', { label: P }).textContent;
    const tf = T.html`${T.ui.tech('at ' + P + ' 127.0.0.1:7890')}`;
    out.techText = tf.textContent;
    out.techSpan = [...tf.querySelectorAll('.tech')].map(n => n.textContent).join('|');
    out.dots = T.ui.dots(2, 1, { label: P }).getAttribute('aria-label');
    out.callout = T.ui.callout({ title: P, message: P, actions: [{ label: P }] }).textContent;
    out.empty = T.ui.empty({ title: P, text: P }).textContent;
    const t0 = T.t;
    T.t = (id, p) => (id === 'core.tier_tag.aria' ? P : t0(id, p));
    const tag = T.ui.tierTag('vanguard', { link: true });
    T.t = t0;
    out.tierTag = [tag.textContent, tag.getAttribute('aria-label')];
    const m = T.ui.menu(document.querySelector('#cmd-open'), [{ group: P }, { label: P, hint: P }]);
    out.menu = m.el.textContent; m.close();
    const c = T.confirm({ title: P, message: P, confirmLabel: P, cancelLabel: P });
    await new Promise(res => setTimeout(res, 30));
    const dlg = document.querySelector('.dlg');
    out.confirm = dlg ? dlg.textContent : null;
    if (dlg) dlg.querySelector('[data-a="no"]').click();
    await c;
    await new Promise(res => setTimeout(res, 300));
    out.imgs = document.querySelectorAll('img[src="x"], [onerror], [onload], script:not([src])').length;
    out.pwned = window.__pwned === undefined ? null : window.__pwned;
    return out;
  }, P);
  assert.equal(r.h, 'TypeError', 'T.h({html}) throws');
  assert.equal(r.hNull, 'no throw', 'html: null is skipped like any empty attribute');
  assert.deepEqual(r.htmlCalls, ['TypeError', 'TypeError', 'TypeError', 'TypeError'], 'T.html called as a function throws (the strings array of a tag is frozen)');
  assert.deepEqual(r.hOn, ['TypeError', 'TypeError', 'TypeError'], 'T.h: string on* handlers and srcdoc throw');
  assert.deepEqual(r.hOnFn, [1, false], 'T.h: a lowercase on* function is a listener, not an attribute');
  assert.ok(r.icu.includes(P), `the ICU argument shows literally: ${r.icu}`);
  assert.equal(r.swapHtml, 'TypeError', 'swapText({html:true}) throws');
  assert.equal(r.swapText, P, 'swapText: a string is text');
  assert.equal(r.swapNode, P, 'swapText: a Node goes in as it is');
  assert.equal(r.pop, P, 'popover: string content is text');
  assert.deepEqual(r.drawer, [P, P, P], 'drawer: content, head and title are text');
  assert.deepEqual(r.sheet, [P, P], 'sheet: content and action labels are text');
  assert.equal(r.status, P);
  assert.equal(r.techText, `at ${P} 127.0.0.1:7890`, 'tech: the text unchanged');
  assert.ok(String(r.techSpan).includes('127.0.0.1:7890'), `tech: the address in its own span: ${r.techSpan}`);
  assert.equal(r.dots, P);
  assert.equal(r.callout, P + P + P);
  assert.equal(r.empty, P + P);
  assert.equal(r.tierTag[0], 'Vanguard', 'tierTag: the name as text (mapping_core U5; F-03: no role word)');
  assert.equal(r.tierTag[1], P, 'tierTag: the aria-label carries the message as text');
  assert.equal(r.menu, P + P + P, 'menu: group, label and hint as text');
  assert.ok(r.confirm && r.confirm.includes(P), 'confirm: title / message / labels as text');
  assert.equal(r.imgs, 0, 'no element came out of the strings');
  assert.equal(r.pwned, null, 'nothing ran');
  assert.deepEqual(errors, []);
  await context.close();
});
