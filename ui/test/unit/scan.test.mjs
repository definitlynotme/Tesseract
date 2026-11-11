// T-P4-01: scan.mjs rules (cjk, demo, inject, style, features), pending markers, allow list.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  UI_ROOT, applyAllow, checkFeatures, findFeatureCalls, isPending, lineDigest, parseFeatureRegistry, runRule,
  scanCjkCss, scanCjkHtml, scanCjkJs, scanDemoText, scanInjectJs, scanStyleHtml, scanStyleJs, main,
} from '../../tools/scan.mjs';

test('cjk: string, template and regex literals hit; comments and ASCII do not', () => {
  assert.equal(scanCjkJs("const a = '中文';").length, 1);
  assert.equal(scanCjkJs('const a = `共 ${n} 个`;')[0].kind, 'template');
  assert.equal(scanCjkJs('const r = /演示/;')[0].kind, 'regex');
  assert.equal(scanCjkJs("const p = '：';").length, 1, 'full-width punctuation');
  assert.equal(scanCjkJs("const p = '、';").length, 1, 'CJK punctuation');
  assert.equal(scanCjkJs("// 注释里的中文\n/* 块注释 中文 */\nconst a = 'ascii';").length, 0);
  assert.equal(scanCjkJs("const a = 'café — ok';").length, 0);
});

test('cjk: index.html text nodes and visible attributes; CSS content values only', () => {
  const html = '<!doctype html><!-- 注释 --><title>总览</title><button aria-label="搜索">x</button><script>var a="中"</script><p>ok</p>';
  const h = scanCjkHtml(html);
  assert.deepEqual(h.map(f => f.kind).sort(), ['attr', 'text']);
  assert.equal(scanCjkHtml('<p>plain</p>').length, 0);
  assert.equal(scanCjkCss('.a::before { content: "演示"; }').length, 1);
  assert.equal(scanCjkCss('/* 中文注释 */ .a { font-family: "微软雅黑"; }').length, 0, 'only content: is checked');
  assert.equal(scanCjkCss('.a::before { content: "x"; }').length, 0);
});

test('cjk: @i18n-pending first-line markers in all three comment forms', () => {
  assert.ok(isPending("// @i18n-pending\nconst a = '中';", 'js'));
  assert.ok(isPending('/* @i18n-pending */\nconst a = 1;', 'js'));
  assert.ok(isPending('<!-- @i18n-pending -->\n<!doctype html>', 'html'));
  assert.ok(isPending('/* @i18n-pending */\n.a{}', 'css'));
  assert.ok(!isPending("const a = 1;\n// @i18n-pending", 'js'), 'only the first line counts');
  assert.ok(!isPending('// @i18n-pending', 'css'), 'JS form does not apply to CSS');
  const root = mkdtempSync(join(tmpdir(), 'tesseract-scan-'));
  try {
    mkdirSync(join(root, 'src', 'pages'), { recursive: true });
    mkdirSync(join(root, 'src', 'design'), { recursive: true });
    mkdirSync(join(root, 'css'), { recursive: true });
    writeFileSync(join(root, 'src', 'pages', 'a.js'), "// @i18n-pending\nconst a = '中';\n");
    writeFileSync(join(root, 'src', 'pages', 'b.js'), "const b = '中';\n");
    writeFileSync(join(root, 'src', 'design', 'c.js'), "const c = '中';\n");
    writeFileSync(join(root, 'src', 'index.html'), '<!-- @i18n-pending -->\n<p>中</p>\n');
    writeFileSync(join(root, 'css', 'x.css'), '/* @i18n-pending */\n.a::before{content:"中"}\n');
    writeFileSync(join(root, 'css', 'design.css'), '.d::before{content:"演示"}\n');
    const r = runRule('cjk', { root });
    assert.deepEqual(r.findings.map(f => f.file), ['src/pages/b.js']);
    assert.equal(r.pending.length, 3);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('cjk: src/rt and src/shellc ignore @i18n-pending, and the marker itself is a finding', () => {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-scan-'));
  try {
    for (const d of ['rt', 'shellc', 'pages']) mkdirSync(join(root, 'src', d), { recursive: true });
    writeFileSync(join(root, 'src', 'rt', 'a.js'), "// @i18n-pending\nconst a = '中';\n");
    writeFileSync(join(root, 'src', 'shellc', 'b.js'), '/* @i18n-pending */\nconst b = 1;\n');
    writeFileSync(join(root, 'src', 'pages', 'c.js'), "// @i18n-pending\nconst c = '中';\n");
    const r = runRule('cjk', { root });
    assert.deepEqual(r.findings.map(f => `${f.file}:${f.kind}`).sort(),
      ['src/rt/a.js:pending-marker', 'src/rt/a.js:string', 'src/shellc/b.js:pending-marker']);
    assert.deepEqual(r.pending.map(f => f.file), ['src/pages/c.js']);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('demo: markers hit in JS and CSS bundles, class tokens match whole names only', () => {
  const markers = { text: ['T.sim', 'sim:'], regex: ['[A-Za-z_$][\\w$]*\\.sim\\b'], css_classes: ['demo-link', 'set-demo'], message_ids: [] };
  const js = scanDemoText('e.sim.scenario("x");on("sim:tick");el.className="demo-link";', 'app.X.js', markers);
  assert.deepEqual(js.map(f => f.marker).sort(), ['/[A-Za-z_$][\\w$]*\\.sim\\b/', 'demo-link', 'sim:'].sort());
  assert.equal(scanDemoText('e.simple();x.className="set-demo-tag"', 'app.X.js', markers).length, 0);
  const css = scanDemoText('.a{}.demo-link:hover{}.set-demo[aria-pressed]{}.set-demo-tag{}', 'app.X.css', markers);
  assert.deepEqual(css.map(f => f.marker).sort(), ['.demo-link', '.set-demo']);
  assert.equal(scanDemoText('.btn{}', 'app.X.css', markers).length, 0);
  // ov-demo / logs-demo are element ids: #id and [id=…] selectors count too.
  const ids = { css_classes: ['ov-demo', 'logs-demo', 'set-demo'] };
  assert.equal(scanDemoText('#ov-demo{}', 'app.X.css', ids).length, 1);
  assert.equal(scanDemoText('[id="logs-demo"]{}', 'app.X.css', ids).length, 1);
  assert.equal(scanDemoText("[class~='set-demo']{}", 'app.X.css', ids).length, 1);
  assert.equal(scanDemoText('#ov-demox{}[id="logs-demo-2"]{}.set-demo-tag{}', 'app.X.css', ids).length, 0);
});

test('demo: rule scans only the files build-manifest.json lists; enforce fails when it cannot run', () => {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-scan-'));
  const log = console.log;
  try {
    mkdirSync(join(root, 'tools'), { recursive: true });
    cpSync(join(UI_ROOT, 'tools', 'demo_markers.json'), join(root, 'tools', 'demo_markers.json'));
    const dist = join(root, 'dist', 'app');
    assert.match(runRule('demo', { root }).skipped, /no bundle/);
    mkdirSync(dist, { recursive: true });
    writeFileSync(join(dist, 'app.A.js'), 'window.demo={};');
    writeFileSync(join(dist, 'app.A.css'), '.btn{}');
    assert.match(runRule('demo', { root }).skipped, /no build-manifest/);
    const manifest = (target, files) => writeFileSync(join(dist, 'build-manifest.json'), JSON.stringify({ target, files: files.map(path => ({ path, sha256: '', bytes: 0 })) }));
    manifest('design', ['app.A.js', 'app.A.css']);
    assert.match(runRule('demo', { root }).skipped, /expected "app"/);
    manifest('app', ['app.A.js', 'app.A.css', 'index.html']);
    assert.equal(runRule('demo', { root }).skipped, null);
    assert.equal(runRule('demo', { root }).findings.length, 1);
    // A stale hashed file that the manifest does not list is not scanned.
    writeFileSync(join(dist, 'app.B.js'), 'window.demo={};');
    writeFileSync(join(dist, 'app.A.js'), 'var x=1;');
    assert.equal(runRule('demo', { root }).findings.length, 0);
    manifest('app', ['app.C.js']);
    assert.match(runRule('demo', { root }).skipped, /missing: app\.C\.js/);
    console.log = () => {};
    assert.equal(main(['--rule', 'demo', '--root', root, '--mode', 'report']), 0, 'report: skipped is not a failure');
    assert.equal(main(['--rule', 'demo', '--root', root, '--mode', 'enforce']), 1, 'enforce: listed file missing');
    assert.equal(main(['--rule', 'demo', '--root', root, '--mode', 'enforce', '--dist', join(root, 'nonexistent')]), 1, 'enforce: no bundle');
    manifest('app', ['app.A.js', 'app.A.css']);
    assert.equal(main(['--rule', 'demo', '--root', root, '--mode', 'enforce']), 0, 'enforce: clean bundle passes');
  } finally { console.log = log; rmSync(root, { recursive: true, force: true }); }
});

test('inject rule 1: innerHTML / outerHTML / insertAdjacentHTML / document.write need plain literals', () => {
  assert.equal(scanInjectJs('el.innerHTML = x;')[0].check, 'html-sink');
  assert.equal(scanInjectJs('el.outerHTML = `<b>${x}</b>`;').length, 1);
  assert.equal(scanInjectJs("el.innerHTML = '';").length, 0);
  assert.equal(scanInjectJs('el.innerHTML = `<i></i>`;').length, 0);
  assert.equal(scanInjectJs("el.insertAdjacentHTML('beforeend', s);").length, 1);
  assert.equal(scanInjectJs("el.insertAdjacentHTML('beforeend', '<br>');").length, 0);
  assert.equal(scanInjectJs('document.write(s);').length, 1);
});

test('inject rule 2: helper calls must not take html, nor interpolated content/head/message', () => {
  assert.equal(scanInjectJs("T.h('div', {html: s});")[0].check, 'helper-html');
  assert.equal(scanInjectJs('M.swapText(el, s, {html: true});')[0].check, 'helper-html');
  assert.equal(scanInjectJs('swapText(el, s, {html: true});').length, 1);
  assert.equal(scanInjectJs('T.notify({message: `x ${n}`});')[0].check, 'helper-string');
  assert.equal(scanInjectJs("T.drawer({head: 'a' + b});").length, 1);
  assert.equal(scanInjectJs("T.popover(el, {content: node});").length, 0);
  assert.equal(scanInjectJs("T.notify({message: 'fixed'});").length, 0);
  assert.equal(scanInjectJs("other.h('div', {html: s});").length, 0, 'only T.* helpers');
  // T.popover(anchor, content): an interpolated positional content string is reported too.
  assert.equal(scanInjectJs('T.ui.popover(btn, `<b>${name}</b>`);')[0].check, 'helper-string');
  assert.equal(scanInjectJs("T.popover(btn, '<b>' + name + '</b>');").length, 1);
  assert.equal(scanInjectJs("T.ui.popover(btn, node);").length, 0);
  assert.equal(scanInjectJs("T.ui.popover(btn, '<b>fixed</b>');").length, 0);
});

test('inject rule 3: T.raw only with literals or icon calls', () => {
  assert.equal(scanInjectJs('T.raw(s);')[0].check, 'raw');
  assert.equal(scanInjectJs("T.raw('<b></b>');").length, 0);
  assert.equal(scanInjectJs("T.raw(T.icon('x'));").length, 0);
  // (T-P4-07 fix round 1: I counts as an icon only when this file binds it to T.icon)
  assert.equal(scanInjectJs("const I = (n, s) => T.icon(n, s); T.raw(I('x'));").length, 0);
  assert.equal(scanInjectJs("T.raw(I('x'));")[0].check, 'raw');
});

test('inject rule 4 (+ on* attributes): eval, Function, string timers, setAttribute("on…")', () => {
  assert.equal(scanInjectJs('eval(s);')[0].check, 'eval');
  assert.equal(scanInjectJs("new Function('return 1');")[0].check, 'eval');
  assert.equal(scanInjectJs("setTimeout('go()', 10);").length, 1);
  assert.equal(scanInjectJs('setTimeout(() => go(), 10);').length, 0);
  assert.equal(scanInjectJs("el.setAttribute('onclick', s);")[0].check, 'on-attr');
  assert.equal(scanInjectJs("el.setAttribute('class', s);").length, 0);
  // Through a global object.
  for (const src of ['window.eval(s);', "globalThis.Function('x')();", "new window.Function('x');", "self['eval'](s);"]) {
    assert.equal(scanInjectJs(src).length, 1, src);
  }
  assert.equal(scanInjectJs('obj.eval(s); new obj.Function();').length, 0, 'other objects are not globals');
});

test('inject allow list: matches by file + line digest; a changed line voids the exception', () => {
  const src = { 'src/a.js': ['const x = 1;', 'el.innerHTML = trusted;'] };
  const lineOf = (f, n) => src[f][n - 1];
  const findings = scanInjectJs(src['src/a.js'].join('\n'), 'src/a.js');
  const entry = { file: 'src/a.js', check: 'html-sink', line_sha256: lineDigest('el.innerHTML = trusted;'), reason: 'test' };
  assert.equal(applyAllow(findings, [entry], lineOf).kept.length, 0);
  assert.equal(applyAllow(findings, [entry], lineOf).allowed.length, 1);
  src['src/a.js'][1] = 'el.innerHTML = trusted + other;';
  const changed = scanInjectJs(src['src/a.js'].join('\n'), 'src/a.js');
  assert.equal(applyAllow(changed, [entry], lineOf).kept.length, 1, 'digest mismatch → not allowed');
  assert.equal(applyAllow(findings, [{ ...entry, file: 'src/b.js' }], (f, n) => src['src/a.js'][n - 1]).kept.length, 1);
  assert.equal(applyAllow(findings, [{ ...entry, check: 'raw' }], lineOf).kept.length, 1, 'check must match when given');
});

test('inject allow list: one entry covers one line; identical lines need count (or their own entries)', () => {
  const lines = ['el.innerHTML = trusted;', 'x();', 'el.innerHTML = trusted;', 'el.innerHTML = trusted;'];
  const lineOf = (f, n) => lines[n - 1];
  const findings = scanInjectJs(lines.join('\n'), 'src/a.js');
  assert.equal(findings.length, 3);
  const entry = { file: 'src/a.js', check: 'html-sink', line_sha256: lineDigest('el.innerHTML = trusted;'), reason: 'test' };
  const one = applyAllow(findings, [entry], lineOf);
  assert.equal(one.allowed.length, 1);
  assert.deepEqual(one.kept.map(f => f.line).sort(), [3, 4]);
  assert.equal(applyAllow(findings, [{ ...entry, count: 2 }], lineOf).kept.length, 1);
  assert.equal(applyAllow(findings, [{ ...entry, count: 3 }], lineOf).kept.length, 0);
  assert.equal(applyAllow(findings, [entry, entry, entry].map(e => ({ ...e })), lineOf).kept.length, 0, 'one entry per line');
  // Two findings on the same line are one line: a single entry covers both.
  const same = scanInjectJs('a.innerHTML = x; b.innerHTML = y;', 'src/b.js');
  const e2 = { file: 'src/b.js', line_sha256: lineDigest('a.innerHTML = x; b.innerHTML = y;'), reason: 'test' };
  assert.equal(applyAllow(same, [e2], () => 'a.innerHTML = x; b.innerHTML = y;').kept.length, 0);
});

test('style: style="…" in strings and templates, setAttribute("style"), style in index.html', () => {
  assert.equal(scanStyleJs('const t = `<i style="width:${w}px"></i>`;').length, 1);
  assert.equal(scanStyleJs("const t = '<i style=\"x\"></i>';").length, 1);
  assert.equal(scanStyleJs("el.setAttribute('style', s);")[0].check, 'set-style');
  assert.equal(scanStyleJs("el.style.setProperty('--w', w);").length, 0);
  assert.equal(scanStyleJs("const t = `<i class=\"a\"></i>`;").length, 0);
  assert.equal(scanStyleHtml('<div style="x"></div>').length, 1);
  assert.equal(scanStyleHtml('<div class="x"></div><!-- <p style="x"> -->').length, 0);
});

test('features: registry names, call sites and per-page coverage', () => {
  const registry = parseFeatureRegistry("T.rt.registry = [{name: 'B-31', b_item: 'B-31', page: 'tiers', controls: []}, {name: 'B-05', page: 'settings'}];");
  assert.deepEqual(registry, [{ name: 'B-31', page: 'tiers' }, { name: 'B-05', page: 'settings' }]);
  const calls = findFeatureCalls("if (T.rt.feature('B-31')) {}\nif (T.rt.feature(name)) {}\nif (T.rt.feature('B-99')) {}", 'src/pages/tiers.js');
  assert.deepEqual(calls.map(c => c.name), ['B-31', null, 'B-99']);
  const out = checkFeatures({ registry, enabled: ['B-31', 'B-77'], calls, pages: ['settings'] });
  assert.deepEqual(out.map(f => f.check).sort(), ['call-unregistered', 'dynamic-name', 'enabled-unregistered', 'unreferenced']);
  assert.equal(checkFeatures({ registry, enabled: ['B-31'], calls: calls.slice(0, 1), pages: ['tiers'] }).length, 0);
});

test('CLI: report mode never fails; enforce fails on findings; mode comes from scan_gates.json', () => {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-scan-'));
  const log = console.log;
  try {
    mkdirSync(join(root, 'src'), { recursive: true });
    mkdirSync(join(root, 'tools'), { recursive: true });
    writeFileSync(join(root, 'src', 'a.js'), 'el.innerHTML = x;\n');
    writeFileSync(join(root, 'tools', 'scan_gates.json'), JSON.stringify({ inject: 'report' }));
    console.log = () => {};
    assert.equal(main(['--rule', 'inject', '--root', root]), 0);
    assert.equal(main(['--rule', 'inject', '--root', root, '--mode', 'enforce']), 1);
    writeFileSync(join(root, 'tools', 'scan_gates.json'), JSON.stringify({ inject: 'enforce' }));
    assert.equal(main(['--rule', 'inject', '--root', root]), 1);
    assert.equal(main(['--rule', 'style', '--root', root, '--mode', 'enforce']), 0);
    assert.equal(main(['--rule', 'bogus', '--root', root]), 2);
  } finally { console.log = log; rmSync(root, { recursive: true, force: true }); }
});
