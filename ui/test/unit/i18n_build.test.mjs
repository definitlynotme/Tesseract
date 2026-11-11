// T-P4-03: build/locales.mjs — language files, __LOCALES__, the preloaded source catalog,
// --pseudo, compile errors with file:id (architecture §1.2, §3.6, §3.8).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import { UI_ROOT, buildTarget } from '../../build.mjs';
import prepare, { LANGS, readCatalogs, compileCatalogs, localeScript } from '../../build/locales.mjs';

const dec = new TextDecoder();
const plain = x => JSON.parse(JSON.stringify(x)); // objects from a vm context have foreign prototypes
const text = (r, p) => dec.decode(r.files.get(p));
let app, design;
const getApp = async () => (app ??= await buildTarget({ target: 'app' }));
const getDesign = async () => (design ??= await buildTarget({ target: 'design', pseudo: true }));

test('every language gets locale-<lang>.<HASH>.js, listed in the manifest and in __LOCALES__', async () => {
  const r = await getApp();
  const m = r.manifest;
  assert.deepEqual(Object.keys(m.locales).sort(), [...LANGS].sort());
  for (const [lang, file] of Object.entries(m.locales)) {
    assert.match(file, new RegExp(`^locale-${lang}\\.[A-Z2-7]{8}\\.js$`));
    assert.ok(r.files.has(file), file);
    assert.ok(m.files.some(f => f.path === file), `${file} in manifest.files`);
  }
  const main = [...r.files.keys()].find(p => /^app\.[A-Z0-9]{8}\.js$/.test(p));
  for (const file of Object.values(m.locales)) assert.ok(text(r, main).includes(file), `__LOCALES__ names ${file}`);
  assert.ok(!('pseudo' in m.locales), 'no pseudo language in a plain app build');
});

test('index.html preloads only the source catalog, before the main bundle', async () => {
  const r = await getApp();
  const scripts = [...text(r, 'index.html').matchAll(/<script src="([^"]+)"/g)].map(x => x[1]);
  assert.equal(scripts.length, 2);
  assert.equal(scripts[0], r.manifest.locales.zh_CN);
  assert.match(scripts[1], /^app\.[A-Z0-9]{8}\.js$/);
});

test('a language file is ASCII-only and registers (or queues) its compiled catalog', async () => {
  const r = await getApp();
  const body = text(r, r.manifest.locales.zh_CN);
  assert.ok(/^[\x20-\x7e\n]*$/.test(body), 'ASCII only (encoding-proof under file:// and WebView2)');
  const queued = { T: {} };
  vm.runInNewContext(body, { window: queued });
  const [[lang, cat]] = queued.T.__i18nq;
  assert.equal(lang, 'zh_CN');
  assert.equal(cat['common.action.save'], '保存');
  assert.ok(cat['errors.start.listener_conflict']);
  const registered = [];
  vm.runInNewContext(body, { window: { T: { i18n: { register: (l, c) => registered.push([l, Object.keys(c).length]) } } } });
  assert.deepEqual(registered, [['zh_CN', Object.keys(cat).length]]);
  // (T-P5-04) since P5 every other language is written in full: each file queues a catalog with
  // exactly the zh_CN ids (no id missing, none extra)
  const ids = Object.keys(cat).sort();
  const others = LANGS.filter(l => l !== 'zh_CN');
  assert.equal(others.length, 8);
  for (const l of others) {
    const w = {};
    vm.runInNewContext(text(r, r.manifest.locales[l]), { window: w });
    const [[got, c]] = w.T.__i18nq;
    assert.equal(got, l);
    assert.deepEqual(Object.keys(c).sort(), ids, `${l}: the full catalog (${Object.keys(c).length} of ${ids.length} ids)`);
  }
});

test('a language with an empty catalog still builds, loads and registers (the runtime falls back)', async () => {
  // a made-up ui root: zh_CN with two messages, de_DE a directory whose only file has no entries
  const root = mkdtempSync(join(tmpdir(), 'tesseract-ui-empty-'));
  try {
    const loc = join(root, 'src', 'i18n', 'locales');
    mkdirSync(join(loc, 'zh_CN'), { recursive: true });
    mkdirSync(join(loc, 'de_DE'), { recursive: true });
    writeFileSync(join(loc, 'zh_CN', 'common.json'), JSON.stringify({ 'common.a': { m: '甲', _desc: 'x' }, 'common.b': { m: '乙', _desc: 'x' } }));
    writeFileSync(join(loc, 'de_DE', 'common.json'), '{}');
    const p = await prepare({ target: 'app', uiRoot: root });
    assert.deepEqual(Object.keys(p.locales).sort(), ['de_DE', 'zh_CN']);
    const file = p.files.find(f => f.path === p.locales.de_DE);
    assert.ok(file, 'locale-de_DE is emitted');
    const q = { T: {} };
    vm.runInNewContext(file.contents, { window: q });
    assert.deepEqual(plain(q.T.__i18nq), [['de_DE', {}]], 'an empty language still loads (and falls back)');
    const registered = [];
    vm.runInNewContext(file.contents, { window: { T: { i18n: { register: (l, c) => registered.push([l, Object.keys(c).length]) } } } });
    assert.deepEqual(registered, [['de_DE', 0]]);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('--pseudo adds locale-pseudo to the design target only (and to app with --dev)', async () => {
  const d = await getDesign();
  assert.ok(d.manifest.locales.pseudo);
  const q = { T: {} };
  vm.runInNewContext(text(d, d.manifest.locales.pseudo), { window: q });
  const cat = q.T.__i18nq[0][1];
  assert.deepEqual(plain(cat['common.action.save'].slice(0, 1)), ['⟦']);
  // (T-P5-04) the language list's own names are never pseudo-localised (their _desc: untranslated
  // in every catalog); exactly these nine ids, the row's other messages stay pseudo
  const kept = Object.keys(cat).filter(id => !JSON.stringify(cat[id]).includes('⟦')).sort();
  assert.deepEqual(kept, ['de_de', 'en_us', 'es_es', 'fr_fr', 'ja_jp', 'ko_kr', 'pt_br', 'ru_ru', 'zh_cn'].map(k => `settings.app.lang.name.${k}`));
  assert.equal(cat['settings.app.lang.name.zh_cn'], '简体中文');
  assert.equal(cat['settings.app.lang.name.en_us'], 'English');
  assert.deepEqual(plain(cat['settings.app.lang.title'].slice(0, 1)), ['⟦']);
  const appPseudo = await buildTarget({ target: 'app', pseudo: true });
  assert.ok(!appPseudo.manifest.locales.pseudo, 'release app: never');
  const dev = await prepare({ target: 'app', uiRoot: UI_ROOT, pseudo: true, dev: true });
  assert.ok(dev.locales.pseudo, 'app --dev: yes');
  assert.equal(dev.define.__I18N_DEV__, 'true');
  const rel = await prepare({ target: 'app', uiRoot: UI_ROOT, i18nTrace: true });
  assert.equal(rel.define.__I18N_DEV__, 'false');
  assert.equal(rel.define.__I18N_TRACE__, 'true');
});

test('an ICU error fails the build with file:id; missing/empty/orphan translations are left out', async () => {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-ui-loc-'));
  try {
    const loc = join(root, 'src', 'i18n', 'locales');
    mkdirSync(join(loc, 'zh_CN'), { recursive: true });
    mkdirSync(join(loc, 'en_US'), { recursive: true });
    writeFileSync(join(loc, 'zh_CN', 'common.json'), JSON.stringify({ 'common.a': { m: '甲', _desc: 'x' }, 'common.b': { m: '乙', _desc: 'x' } }));
    writeFileSync(join(loc, 'en_US', 'common.json'), JSON.stringify({ 'common.a': 'A', 'common.b': '  ', 'common.gone': 'G' }));
    const ok = compileCatalogs(readCatalogs(root));
    assert.deepEqual({ ...ok.en_US }, { 'common.a': 'A' });
    assert.equal(Object.getPrototypeOf(ok.en_US), null, 'compiled catalogs have no prototype');
    // reserved ids would act on an object literal's prototype in the language script
    writeFileSync(join(loc, 'zh_CN', 'logs.json'), '{"__proto__": {"m": "x", "_desc": "x"}}');
    assert.throws(() => compileCatalogs(readCatalogs(root)), /src\/i18n\/locales\/zh_CN\/logs\.json:__proto__: reserved message id/);
    rmSync(join(loc, 'zh_CN', 'logs.json'));
    writeFileSync(join(loc, 'en_US', 'common.json'), JSON.stringify({ 'common.a': '{n, plural, one {x}}' }));
    assert.throws(() => compileCatalogs(readCatalogs(root)), /src\/i18n\/locales\/en_US\/common\.json:common\.a: plural needs an "other" case/);
    // through buildTarget, too
    for (const d of ['entry']) mkdirSync(join(root, 'src', d), { recursive: true });
    mkdirSync(join(root, 'css'), { recursive: true });
    writeFileSync(join(root, 'src', 'entry', 'app.js'), 'globalThis.x = __LOCALES__;\n');
    writeFileSync(join(root, 'src', 'index.html'), '<!doctype html><html><head><!-- @build:css --></head><body><!-- @build:js --></body></html>\n');
    writeFileSync(join(root, 'src', 'features.json'), '[]\n');
    writeFileSync(join(root, 'css', 'index.css'), 'a{color:red}\n');
    mkdirSync(join(root, 'tools'), { recursive: true });
    cpSync(join(UI_ROOT, 'tools', 'demo_markers.json'), join(root, 'tools', 'demo_markers.json'));
    await assert.rejects(buildTarget({ target: 'app', root }), /common\.json:common\.a/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('a ui root without catalogs builds with an empty __LOCALES__ (plugin is a no-op)', async () => {
  const r = await prepare({ target: 'app', uiRoot: join(tmpdir(), 'no-such-ui-root') });
  assert.deepEqual(r.files, []);
  assert.deepEqual(r.scripts, []);
  assert.equal(r.define.__LOCALES__, JSON.stringify('{}'), 'a JSON string literal (no define comment in unminified bundles)');
});

test('localeScript output is deterministic and escapes every non-ASCII character', () => {
  const a = localeScript('zh_CN', { 'x.y': ['⟦', '中', '~⟧'], 'x.z': 'a b' });
  assert.equal(a, localeScript('zh_CN', { 'x.y': ['⟦', '中', '~⟧'], 'x.z': 'a b' }));
  assert.ok(/^[\x20-\x7e\n]*$/.test(a));
  assert.ok(a.includes('\\u4e2d') && a.includes('\\u00a0'));
});
