// T-P4-01: build.mjs (both targets, hashing, manifest, determinism, demo split, hooks).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { UI_ROOT, buildTarget, demoFindings, htmlProblems, parseArgs, renderHtml, writeOutput } from '../../build.mjs';
import { loadGates } from '../../tools/scan.mjs';

const dec = new TextDecoder();
const sha = b => createHash('sha256').update(b).digest('hex');
const built = {};
async function get(target) {
  built[target] ??= await buildTarget({ target });
  return built[target];
}
const text = (r, p) => dec.decode(r.files.get(p));
const named = (r, re) => [...r.files.keys()].filter(p => re.test(p));
const isStringArray = v => Array.isArray(v) && v.every(x => typeof x === 'string');
const isPlainObject = v => v !== null && typeof v === 'object' && !Array.isArray(v) && Object.getPrototypeOf(v) === Object.prototype;

// A minimal ui root in a temp directory: the given entry sources plus the template, CSS
// entries, features.json and the real demo marker list. Nothing is imported from
// node_modules, so buildTarget({root}) bundles it on its own.
function tempUiRoot({ app, design = app, css = '.btn{color:red}\n' }) {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-ui-root-'));
  mkdirSync(join(root, 'src', 'entry'), { recursive: true });
  mkdirSync(join(root, 'css'), { recursive: true });
  mkdirSync(join(root, 'tools'), { recursive: true });
  writeFileSync(join(root, 'src', 'entry', 'app.js'), app);
  writeFileSync(join(root, 'src', 'entry', 'design.js'), design);
  writeFileSync(join(root, 'src', 'index.html'), '<!doctype html><html><head><!-- @build:css --></head><body><!-- @build:js --></body></html>\n');
  writeFileSync(join(root, 'src', 'features.json'), '[]\n');
  writeFileSync(join(root, 'css', 'index.css'), css);
  writeFileSync(join(root, 'css', 'design.css'), '.demo-link{color:blue}\n');
  cpSync(join(UI_ROOT, 'tools', 'demo_markers.json'), join(root, 'tools', 'demo_markers.json'));
  return root;
}

test('both targets build, with content-hashed file names', async () => {
  const app = await get('app');
  const design = await get('design');
  assert.equal(named(app, /^app\.[A-Z0-9]{8}\.js$/).length, 1);
  assert.equal(named(app, /^app\.[A-Z0-9]{8}\.css$/).length, 1);
  assert.equal(named(design, /^design\.[A-Z0-9]{8}\.js$/).length, 1);
  assert.equal(named(design, /^app\.[A-Z0-9]{8}\.css$/).length, 1);
  assert.equal(named(design, /^design\.[A-Z0-9]{8}\.css$/).length, 1);
  assert.ok(app.files.has('index.html') && app.files.has('build-manifest.json'));
  assert.ok(design.files.has('index.html') && design.files.has('build-manifest.json'));
});

test('index.html references only hashed files: no inline script body, no style attribute or element', async () => {
  for (const target of ['app', 'design']) {
    const r = await get(target);
    const html = text(r, 'index.html');
    assert.deepEqual(htmlProblems(html), []);
    const refs = [...html.matchAll(/<(?:script src|link rel="stylesheet" href)="([^"]+)"/g)].map(m => m[1]).filter(u => !u.startsWith('https:'));
    assert.ok(refs.length >= 2);
    for (const ref of refs) assert.ok(r.files.has(ref), `${target}: ${ref} is an output file`);
    for (const ref of refs) assert.match(ref, /\.[A-Z0-9]{8}\.(js|css)$/);
    assert.ok(!html.includes('@build:'), 'template markers replaced');
  }
  assert.deepEqual(htmlProblems('<script>alert(1)</script>'), ['inline <script> body in index.html']);
  assert.deepEqual(htmlProblems('<div style="color:red"></div>'), ['style= attribute in index.html']);
  assert.deepEqual(htmlProblems('<img src="x.png" onerror="go()">'), ['inline on*= event handler attribute in index.html']);
  assert.deepEqual(htmlProblems('<button type="button" onClick=go()>x</button>'), ['inline on*= event handler attribute in index.html']);
  assert.deepEqual(htmlProblems('<a href="javascript:go()">x</a>'), ['javascript: URL in index.html']);
  assert.deepEqual(htmlProblems("<a href=' JavaScript:go()'>x</a>"), ['javascript: URL in index.html']);
  assert.deepEqual(htmlProblems('<a href="https://example.invalid/on=1" data-x="javascript">x</a><button class="one">y</button>'), []);
  assert.deepEqual(htmlProblems('<!-- <script>x</script> <p style="x" onclick="y"> -->'), [], 'comments ignored');
  assert.throws(() => renderHtml('<html></html>', { css: [], scripts: [] }), /@build:css/);
});

test('app: the notice card page is built next to index.html (its own bundle, the zh_CN language file), listed in the manifest', async () => {
  const app = await get('app');
  const html = text(app, 'notice.html');
  assert.deepEqual(htmlProblems(html), []);
  assert.ok(!html.includes('@build:'), 'template markers replaced');
  const refs = [...html.matchAll(/<(?:script src|link rel="stylesheet" href)="([^"]+)"/g)].map(m => m[1]);
  assert.equal(refs.filter(r => /^notice\.[A-Z0-9]{8}\.css$/.test(r)).length, 1);
  assert.equal(refs.filter(r => /^notice\.[A-Z0-9]{8}\.js$/.test(r)).length, 1);
  assert.ok(refs.some(r => /^locale-zh_CN\.[A-Z0-9]{8}\.js$/.test(r)), 'the source language is preloaded as in index.html');
  for (const ref of refs) assert.ok(app.files.has(ref), ref);
  const listed = JSON.parse(text(app, 'build-manifest.json')).files.map(f => f.path);
  for (const p of ['notice.html', ...refs]) assert.ok(listed.includes(p), `build-manifest.json lists ${p}`);
  assert.ok(!text(app, 'index.html').includes('notice.'), 'index.html does not load the notice bundle');
  // the width the shell sizes the window with (shell/app/src/notice.rs WIDTH_CSS)
  assert.match(text(app, refs.find(r => r.endsWith('.css'))), /--nc-width:\s*396px/);
  assert.ok(!(await get('design')).files.has('notice.html'), 'the design target has no notice page');
});

test('manifest: fields complete, sha256 and sizes correct, no build time', async () => {
  for (const target of ['app', 'design']) {
    const r = await get(target);
    const m = JSON.parse(text(r, 'build-manifest.json'));
    assert.equal(m.target, target);
    assert.match(m.source_digest, /^[0-9a-f]{64}$/);
    // Shapes only: later tickets fill locales (03), enable features (02a/05x) and make the
    // app bundle demo-free (07); the values are checked against their sources below.
    assert.ok(isPlainObject(m.locales), 'locales is a plain object');
    assert.equal(typeof m.demo_free, 'boolean');
    assert.ok(isStringArray(m.h_features), 'h_features is a string array');
    assert.deepEqual(m.h_features, JSON.parse(readFileSync(join(UI_ROOT, 'src', 'features.json'), 'utf8')), 'h_features = features.json');
    // demo_free is the demo gate's verdict on this very bundle; design is never demo-free.
    if (target === 'design') assert.equal(m.demo_free, false);
    else {
      assert.equal(m.demo_free, demoFindings(r.files).length === 0);
      if (loadGates().demo === 'enforce') assert.equal(m.demo_free, true, 'demo gate enforced: the app bundle must be demo-free');
    }
    if (!process.env.SOURCE_DATE_EPOCH) assert.ok(!('source_date' in m));
    const keys = [...r.files.keys()].filter(p => p !== 'build-manifest.json').sort();
    assert.deepEqual(m.files.map(f => f.path), keys);
    for (const f of m.files) {
      assert.equal(f.sha256, sha(r.files.get(f.path)), f.path);
      assert.equal(f.bytes, r.files.get(f.path).byteLength, f.path);
    }
    assert.ok(!/"(built_at|build_time|time|date)"/.test(text(r, 'build-manifest.json')));
  }
});

test('two builds of the same sources are byte-identical (manifest included)', async () => {
  for (const target of ['app', 'design']) {
    const a = await get(target);
    const b = await buildTarget({ target });
    assert.deepEqual([...a.files.keys()].sort(), [...b.files.keys()].sort());
    for (const [k, v] of a.files) assert.equal(Buffer.compare(Buffer.from(v), Buffer.from(b.files.get(k))), 0, `${target}/${k}`);
  }
});

test('written output matches the in-memory build; --clean only empties allowed directories', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'tesseract-ui-build-'));
  try {
    const r = await get('app');
    writeOutput(dir, r.files, { clean: true }); // empty directory: allowed
    writeFileSync(join(dir, 'stale.js'), 'old');
    writeOutput(dir, r.files, { clean: true }); // previous build (has build-manifest.json): allowed
    assert.ok(!existsSync(join(dir, 'stale.js')));
    assert.deepEqual(readdirSync(dir, { recursive: true, withFileTypes: true }).filter(e => e.isFile()).map(e => join(e.parentPath, e.name).slice(dir.length + 1).replaceAll('\\', '/')).sort(), [...r.files.keys()].sort());
    for (const [k, v] of r.files) assert.equal(sha(readFileSync(join(dir, k))), sha(v), k);
    // A non-empty directory that is not a previous build and not under ui/dist or shell/app/ui.
    const other = join(dir, 'other');
    mkdirSync(join(other, 'sub'), { recursive: true });
    writeFileSync(join(other, 'sub', 'keep.txt'), 'keep');
    assert.throws(() => writeOutput(other, r.files, { clean: true }), /refusing to clean/);
    assert.ok(existsSync(join(other, 'sub', 'keep.txt')), 'refused directory left intact');
  } finally { rmSync(dir, { recursive: true, force: true }); }
  for (const d of [UI_ROOT, join(UI_ROOT, '..'), join(UI_ROOT, 'dist'), join(UI_ROOT, 'src'), join(UI_ROOT, '..', 'shell'), join(UI_ROOT, '..', 'shell', 'app'), join(UI_ROOT, '..', '.build')]) {
    if (existsSync(d)) assert.throws(() => writeOutput(d, new Map(), { clean: true }), /refusing to clean/, d);
  }
});

test('__DEMO__ / __TARGET__ / __H_FEATURES__ are replaced by constants; demo blocks vanish from app (via buildTarget)', async () => {
  const root = tempUiRoot({ app: "if (__DEMO__) { globalThis.m = 'DEMO_ONLY_MARKER'; }\nglobalThis.t = __TARGET__; globalThis.f = __H_FEATURES__;\n" });
  try {
    writeFileSync(join(root, 'src', 'features.json'), '["B-1"]\n');
    const js = r => text(r, named(r, /\.js$/)[0]);
    const app = js(await buildTarget({ target: 'app', root }));
    const design = js(await buildTarget({ target: 'design', root }));
    // __H_FEATURES__ is a JSON string literal: '["B-1"]' or "[\"B-1\"]" depending on the quote esbuild picks
    const features = /'\["B-1"\]'|"\[\\"B-1\\"\]"/;
    assert.ok(!app.includes('DEMO_ONLY_MARKER') && !app.includes('__DEMO__') && app.includes('"app"') && features.test(app));
    assert.ok(design.includes('DEMO_ONLY_MARKER') && design.includes('"design"') && features.test(design));
  } finally { rmSync(root, { recursive: true, force: true }); }
  for (const target of ['app', 'design']) {
    const r = await get(target);
    for (const p of named(r, /\.js$/)) assert.ok(!/__(DEMO|TARGET|H_FEATURES|LOCALES)__/.test(text(r, p)), `${target}/${p}`);
  }
});

test('app bundle carries none of the design.css rules; design bundle does', async () => {
  const markers = JSON.parse(readFileSync(join(UI_ROOT, 'tools', 'demo_markers.json'), 'utf8'));
  const designCss = readFileSync(join(UI_ROOT, 'css', 'design.css'), 'utf8');
  const app = await get('app');
  const design = await get('design');
  const appCss = named(app, /\.css$/).map(p => text(app, p)).join('\n');
  const designOut = text(design, named(design, /^design\.[A-Z0-9]+\.css$/)[0]);
  // The demo classes the prototype stylesheets declared (.build/claude_ui_prototype/css,
  // not in the repo; ov-demo / logs-demo are element ids there, with no CSS rule): all moved.
  const moved = ['demo-link', 'set-demo', 'set-notes-demo', 'set-demo-tag'];
  for (const c of moved) assert.ok(markers.css_classes.includes(c), `demo_markers.json lists ${c}`);
  for (const c of markers.css_classes) {
    const re = new RegExp(`[.#]${c}(?![\\w-])`);
    assert.ok(!re.test(appCss), `app css has .${c} / #${c}`);
    if (!moved.includes(c)) continue;
    assert.ok(re.test(designCss), `design.css has .${c}`);
    assert.ok(re.test(designOut), `design bundle has .${c}`);
  }
  // No other stylesheet under ui/css still declares a demo class.
  for (const f of ['tokens', 'base', 'components', 'shell', 'pages/overview', 'pages/tiers', 'pages/logs', 'pages/settings']) {
    const css = readFileSync(join(UI_ROOT, 'css', `${f}.css`), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    for (const c of markers.css_classes) assert.ok(!new RegExp(`\\.${c}(?![\\w-])`).test(css), `${f}.css still has .${c}`);
  }
  assert.ok(!named(app, /^design\./).length);
});

test('app minified without source maps; design unminified with linked source maps', async () => {
  const app = await get('app');
  const design = await get('design');
  assert.equal(named(app, /\.map$/).length, 0);
  assert.ok(!/sourceMappingURL/.test(named(app, /\.js$/).map(p => text(app, p)).join('')));
  const js = named(design, /\.js$/)[0];
  assert.ok(design.files.has(js + '.map'));
  assert.match(text(design, js), /\/\/# sourceMappingURL=design\.[A-Z0-9]+\.js\.map\s*$/);
  assert.ok(app.files.get(named(app, /\.js$/)[0]).byteLength < 0.8 * design.files.get(js).byteLength, 'app JS is minified');
});

test('arguments: --h-features all only for design; unknown arguments rejected', () => {
  assert.throws(() => parseArgs(['--target', 'app', '--h-features', 'all']), /design/);
  assert.equal(parseArgs(['--target', 'design', '--h-features', 'all']).hFeatures, 'all');
  assert.throws(() => parseArgs(['--target', 'design', '--h-features', 'B-1']), /all/);
  assert.throws(() => parseArgs(['--target', 'x']), /target/);
  assert.throws(() => parseArgs(['--target', 'app', '--nope']), /unknown/);
  const o = parseArgs(['--target', 'design', '--out', 'x', '--clean', '--pseudo', '--i18n-trace']);
  assert.ok(o.clean && o.pseudo && o.i18nTrace && o.out === 'x');
  assert.ok(parseArgs(['--check']).check);
});

test('hook files (architecture §1.1.1) and package.json scripts all exist', () => {
  const files = [
    'src/entry/app.js', 'src/entry/design.js', 'src/index.html', 'src/features.json',
    'src/boot/data-app.js', 'src/boot/data-design.js', 'src/boot/rt-app.js', 'src/boot/rt-design.js',
    'src/boot/i18n.js', 'src/boot/shellc.js', 'src/boot/design.js', 'src/boot/README.md',
    'build/locales.mjs', 'build/fonts.mjs', 'css/index.css', 'css/design.css',
    'tools/check.mjs', 'tools/scan.mjs', 'tools/scan_gates.json', 'tools/demo_markers.json', 'tools/inject_allow.json',
    'test/render/fixture_clock.js', 'package-lock.json',
  ];
  for (const f of files) assert.ok(existsSync(join(UI_ROOT, f)), f);
  const pkg = JSON.parse(readFileSync(join(UI_ROOT, 'package.json'), 'utf8'));
  for (const s of ['build', 'build:app', 'build:design', 'check', 'test', 'test:render', 'test:e2e', 'test:perf', 'test:a11y']) assert.ok(pkg.scripts[s], s);
  for (const d of ['esbuild', 'acorn', 'acorn-walk', 'playwright-core', '@fontsource-variable/instrument-sans', '@fontsource/geist-mono']) {
    assert.match(pkg.devDependencies[d], /^\d+\.\d+\.\d+$/, `${d} pinned exactly`);
  }
  assert.equal(pkg.devDependencies['playwright-core'], '1.63.0');
  // Shapes only: later tickets flip their own gate to enforce (02a features, 03 cjk,
  // 07 demo/inject, 08b style) and append pages to features_pages (05x).
  const gates = JSON.parse(readFileSync(join(UI_ROOT, 'tools', 'scan_gates.json'), 'utf8'));
  assert.deepEqual(Object.keys(gates).sort(), ['cjk', 'demo', 'features', 'features_pages', 'inject', 'style']);
  for (const k of ['cjk', 'demo', 'inject', 'style', 'features']) assert.ok(['report', 'enforce'].includes(gates[k]), `scan_gates.${k}`);
  assert.ok(isStringArray(gates.features_pages), 'features_pages is a string array');
  assert.ok(isStringArray(JSON.parse(readFileSync(join(UI_ROOT, 'src', 'features.json'), 'utf8'))), 'features.json is a string array');
});

test('entries keep the prototype script order (core → icons → i18n → data → rt → shellc → pages → app)', () => {
  const order = f => [...readFileSync(join(UI_ROOT, 'src', 'entry', f), 'utf8').matchAll(/^import '([^']+)';$/gm)].map(m => m[1]);
  const common = ['../core/core.js', '../core/icons.js', '../boot/i18n.js', null, null, '../boot/shellc.js',
    '../pages/overview.js', '../pages/tiers.js', '../pages/logs.js', '../pages/settings.js', '../core/app.js'];
  const expect = (data, rt) => common.map((x, i) => x ?? (i === 3 ? data : rt));
  assert.deepEqual(order('app.js'), expect('../boot/data-app.js', '../boot/rt-app.js'));
  assert.deepEqual(order('design.js'), [...expect('../boot/data-design.js', '../boot/rt-design.js'), '../boot/design.js']);
});

test('demo_free is computed: true for a marker-free app bundle, false with a marker or for design', async () => {
  const root = tempUiRoot({ app: 'globalThis.ok = 1;\n', design: 'globalThis.ok = 1;\n' });
  try {
    const app = await buildTarget({ target: 'app', root });
    assert.deepEqual(demoFindings(app.files, root), []);
    assert.equal(app.manifest.demo_free, true);
    assert.equal(JSON.parse(text(app, 'build-manifest.json')).demo_free, true);
    assert.equal((await buildTarget({ target: 'design', root })).manifest.demo_free, false, 'design is never demo-free');
    writeFileSync(join(root, 'src', 'entry', 'app.js'), 'globalThis.x = window.demo;\n');
    assert.equal((await buildTarget({ target: 'app', root })).manifest.demo_free, false, 'JS marker');
    writeFileSync(join(root, 'src', 'entry', 'app.js'), 'globalThis.ok = 1;\n');
    writeFileSync(join(root, 'css', 'index.css'), '#ov-demo{color:red}\n');
    assert.equal((await buildTarget({ target: 'app', root })).manifest.demo_free, false, 'CSS id marker');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('source_digest covers package-lock.json', async () => {
  const root = tempUiRoot({ app: 'globalThis.ok = 1;\n' });
  try {
    writeFileSync(join(root, 'package-lock.json'), '{"lockfileVersion":3,"packages":{"node_modules/esbuild":{"version":"0.28.2"}}}\n');
    const a = (await buildTarget({ target: 'app', root })).manifest.source_digest;
    writeFileSync(join(root, 'package-lock.json'), '{"lockfileVersion":3,"packages":{"node_modules/esbuild":{"version":"0.28.3"}}}\n');
    assert.notEqual((await buildTarget({ target: 'app', root })).manifest.source_digest, a);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
