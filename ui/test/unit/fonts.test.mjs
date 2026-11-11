// T-P4-08a: self-hosted fonts (P4 architecture §8). The bundle carries the woff2 files and
// the OFL texts; css/fonts.css declares exactly what the fonts map (cmap); the per-language
// stacks follow §8; nothing in either target loads an external resource. The local zh voice
// subset (Tesseract Kai, tools/zh_voice_font.py) maps every CJK character of the zh_CN
// messages and carries no Reserved Font Name of its upstream in the names that identify it.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { UI_ROOT, buildTarget } from '../../build.mjs';
import {
  FONT_FACES, FONT_PACKAGES, fontCopies, fontSourcePath, licenseFile, licenseSourcePath, packageOf, syncFonts,
} from '../../build/fonts.mjs';
import { localeScript, readCatalogs } from '../../build/locales.mjs';
import {
  BLOCKS, checkFontsCss, cmapCodepoints, coverage, expectedRange, formatUnicodeRange, nameRecords, parseFontFaces, parseUnicodeRange, woff2Tables,
} from '../../tools/font_cmap.mjs';

const dec = new TextDecoder();
const built = {};
async function get(target) {
  built[target] ??= await buildTarget({ target });
  return built[target];
}
const text = (r, p) => dec.decode(r.files.get(p));
const named = (r, re) => [...r.files.keys()].filter(p => re.test(p)).sort();
const escape = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const stem = f => f.replace(/\.[^.]+$/, '');
const hex = c => c.toString(16).toUpperCase().padStart(4, '0');
const KAI = FONT_FACES.find(f => f.local && f.family === 'Tesseract Kai');

// ---- external resource references --------------------------------------------------
// A resource reference is something the browser fetches by itself: src/href/srcset/... in
// HTML, url()/@import in CSS, and the same constructs (or fetch/import/Worker/...) written
// inside JS strings. Link *text* (an <a href>, a placeholder, a log line) is not one.
const ABSOLUTE = /^\s*(?:[a-z][a-z0-9+.-]*:|\/\/|\\\\)/i; // any scheme, or protocol-relative
const REMOTE = '(?:https?:|wss?:|ftp:)?//';
const JS_RESOURCE = [
  new RegExp(`\\burl\\(\\s*\\\\?["'\`]?\\s*${REMOTE}`, 'i'),
  new RegExp(`@import\\s+(?:url\\(\\s*)?\\\\?["'\`]\\s*${REMOTE}`, 'i'),
  new RegExp(`\\b(?:fetch|import|importScripts|sendBeacon|new\\s+(?:Worker|SharedWorker|EventSource|WebSocket|Request))\\s*\\(\\s*["'\`]\\s*${REMOTE}`, 'i'),
  new RegExp(`<(?:link|script|img|iframe|frame|source|video|audio|track|embed|object|image|use|input)\\b[^>]*?\\s(?:src|href|srcset|data|poster|xlink:href)\\s*=\\s*\\\\?["'\`]?\\s*${REMOTE}`, 'i'),
  new RegExp(`\\.(?:src|srcset|href|data|poster)\\s*=\\s*["'\`]\\s*${REMOTE}`, 'i'),
  new RegExp(`setAttribute(?:NS)?\\([^)]*?["'\`](?:src|srcset|href|data|poster|xlink:href)["'\`]\\s*,\\s*["'\`]\\s*${REMOTE}`, 'i'),
];

function cssRefs(css) {
  const body = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const refs = [];
  for (const m of body.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)/gi)) refs.push(m[1] ?? m[2] ?? m[3]);
  for (const m of body.matchAll(/@import\s+(?:"([^"]*)"|'([^']*)')/gi)) refs.push(m[1] ?? m[2]);
  return refs;
}

function htmlRefs(html) {
  const body = html.replace(/<!--[\s\S]*?-->/g, '');
  const refs = [];
  for (const m of body.matchAll(/<([a-zA-Z][a-zA-Z0-9-]*)[^>]*>/g)) {
    const link = /^(a|area)$/i.test(m[1]); // <a href> / <area href> navigate, they fetch nothing
    for (const a of m[0].matchAll(/\s(src|href|srcset|data|poster|action|xlink:href|imagesrcset)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi)) {
      if (link && /^href$/i.test(a[1])) continue;
      const v = a[2] ?? a[3] ?? a[4];
      if (/srcset/i.test(a[1])) refs.push(...v.split(',').map(s => s.trim().split(/\s+/)[0]).filter(Boolean));
      else refs.push(v);
    }
  }
  for (const m of body.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)) refs.push(...cssRefs(m[1]));
  return refs;
}

// A language file (build/locales.mjs localeScript) is a fixed wrapper around one JSON catalog;
// the catalog's strings are message text (a hint may well say "URL(http:// …)"), shown as text
// and never fetched. (T-P5-04) In a file of exactly that shape the catalog's string literals are
// blanked before the script patterns run; the wrapper is still scanned. Any other script, or a
// language file that does not have the wrapper's shape, is scanned as it is.
const LOCALE_FILE = /^locale-[A-Za-z_]+\.[A-Z2-7]{8}\.js$/;
const LOCALE_BODY = /^(\(function\(w\)\{var T=w\.T=w\.T\|\|\{\},a=\[)([\s\S]*)(\];if\(T\.i18n&&T\.i18n\.register\)T\.i18n\.register\(a\[0\],a\[1\]\);else\(T\.__i18nq=T\.__i18nq\|\|\[\]\)\.push\(a\);\}\)\(window\);\n)$/;
function scriptCode(path, s) {
  const m = LOCALE_FILE.test(path.slice(path.lastIndexOf('/') + 1)) && LOCALE_BODY.exec(s);
  if (!m) return s;
  // the array must be pure data: [language, {catalog}] as JSON, nothing executable in between
  let a;
  try { a = JSON.parse(`[${m[2]}]`); } catch (e) { return s; }
  if (a.length !== 2 || typeof a[0] !== 'string' || !a[1] || typeof a[1] !== 'object' || Array.isArray(a[1])) return s;
  return m[1] + m[2].replace(/"(?:[^"\\\n]|\\.)*"/g, '""') + m[3];
}

// Problems over a whole bundle: Map(path → bytes). License texts are link text only.
function externalProblems(files) {
  const out = [];
  for (const [path, bytes] of files) {
    if (/\.(woff2?|png|ico)$/i.test(path) || /(^|\/)OFL-[^/]*\.txt$/.test(path)) continue;
    const s = dec.decode(bytes);
    for (const host of ['googleapis', 'gstatic']) if (s.toLowerCase().includes(host)) out.push(`${path}: mentions ${host}`);
    const refs = path.endsWith('.html') ? htmlRefs(s) : path.endsWith('.css') ? cssRefs(s) : [];
    for (const ref of refs) {
      if (/^\s*data:/i.test(ref) || /^\s*#/.test(ref)) continue;
      if (ABSOLUTE.test(ref)) out.push(`${path}: external reference ${ref}`);
      else if (!path.endsWith('.map')) {
        const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : '';
        const target = (dir + ref.replace(/^\.\//, '')).split(/[?#]/)[0];
        if (!files.has(target)) out.push(`${path}: reference ${ref} is not an output file`);
      }
    }
    if (/\.(js|mjs)$/.test(path)) { const code = scriptCode(path, s); for (const re of JS_RESOURCE) { const m = re.exec(code); if (m) out.push(`${path}: external resource in script: ${m[0].slice(0, 80)}`); } }
  }
  return out;
}

// ---- tests ---------------------------------------------------------------------------
test('both targets carry every woff2 the CSS names, byte-identical to the pinned package (a local font: its source), with hashed names', async () => {
  for (const target of ['app', 'design']) {
    const r = await get(target);
    const css = named(r, /^app\.[A-Z0-9]{8}\.css$/).map(p => text(r, p)).join('\n');
    const woff2 = named(r, /\.woff2$/);
    assert.equal(woff2.length, FONT_FACES.length, `${target}: one woff2 per @font-face`);
    for (const face of FONT_FACES) {
      const re = new RegExp(`^assets/${escape(stem(face.file))}\\.[A-Z0-9]{8}\\.woff2$`);
      const hits = woff2.filter(p => re.test(p));
      assert.equal(hits.length, 1, `${target}: ${face.file} emitted once with a hash`);
      assert.ok(Buffer.from(r.files.get(hits[0])).equals(readFileSync(fontSourcePath(UI_ROOT, face))), `${target}: ${hits[0]} = ${face.local ? 'ui/fonts source' : 'node_modules copy'}`);
      assert.ok(css.includes(`url("./${hits[0]}")`) || css.includes(`url(./${hits[0]})`), `${target}: app css references ${hits[0]}`);
      // the build also left the copy in ui/fonts/ (source of the url() in css/fonts.css; a
      // local font's file is that source itself)
      assert.ok(readFileSync(join(UI_ROOT, 'fonts', face.file)).equals(readFileSync(fontSourcePath(UI_ROOT, face))), `ui/fonts/${face.file}`);
    }
    const m = JSON.parse(text(r, 'build-manifest.json'));
    for (const p of woff2) assert.ok(m.files.some(f => f.path === p), `${target}: manifest lists ${p}`);
    // the bundled @font-face rules keep unicode-range and font-display
    const faces = parseFontFaces(css);
    assert.equal(faces.length, FONT_FACES.length, `${target}: bundled @font-face count`);
    for (const f of faces) {
      assert.equal(f.display, 'swap');
      assert.ok(f.codepoints.length > 0, `${target}: ${f.url} has a unicode-range`);
    }
  }
});

// What each family's license text must name: its copyright holders.
const LICENSE_MARKERS = {
  'instrument-sans': ['The Instrument Sans Project Authors'],
  'geist-mono': ['The Geist Project Authors'],
  'tesseract-kai': ['Copyright 2021-2026 LXGW', 'The Klee Project Authors'],
};

test('both targets carry the OFL text of each font family, with hashed names', async () => {
  assert.deepEqual(Object.keys(LICENSE_MARKERS).sort(), FONT_PACKAGES.map(p => p.id).sort(), 'a marker list per font package');
  for (const target of ['app', 'design']) {
    const r = await get(target);
    for (const p of FONT_PACKAGES) {
      const re = new RegExp(`^assets/${escape(stem(licenseFile(p)))}\\.[A-Z0-9]{8}\\.txt$`);
      const hits = named(r, re);
      assert.equal(hits.length, 1, `${target}: ${licenseFile(p)}`);
      const body = text(r, hits[0]);
      assert.ok(Buffer.from(r.files.get(hits[0])).equals(readFileSync(licenseSourcePath(UI_ROOT, p))), `${target}: ${hits[0]} = ${p.local ? 'ui/fonts source' : 'package LICENSE'}`);
      assert.match(body, /SIL OPEN FONT LICENSE Version 1\.1/);
      for (const marker of LICENSE_MARKERS[p.id]) assert.ok(body.includes(marker), `${target}: ${hits[0]} names ${marker}`);
      assert.ok(JSON.parse(text(r, 'build-manifest.json')).files.some(f => f.path === hits[0]));
      assert.ok(existsSync(join(UI_ROOT, 'fonts', licenseFile(p))), `ui/fonts/${licenseFile(p)}`);
    }
  }
});

test('woff2 reader: table stream offsets are right (head magic), cmap parsed', () => {
  for (const face of FONT_FACES) {
    const tables = woff2Tables(readFileSync(fontSourcePath(UI_ROOT, face)));
    assert.equal(tables.get('head').readUInt32BE(12), 0x5F0F3CF5, `${face.file}: head.magicNumber`);
    const cps = cmapCodepoints(readFileSync(fontSourcePath(UI_ROOT, face)));
    assert.ok(cps.length > 20, face.file);
    if (face.subset === 'latin') for (const c of 'Tesseract 0123456789') assert.ok(cps.includes(c.codePointAt(0)), `${face.file} maps ${c}`);
  }
  assert.deepEqual(parseUnicodeRange('U+0041-0043, U+0061,U+00E?'), [0x41, 0x42, 0x43, 0x61, ...Array.from({ length: 16 }, (_, i) => 0xE0 + i)]);
  assert.equal(formatUnicodeRange([0x61, 0x41, 0x42, 0x43, 0x2212]), 'U+0041-0043, U+0061, U+2212');
  assert.throws(() => parseUnicodeRange('U+0043-0041'));
});

test('css/fonts.css: unicode-range = cmap ∩ Fontsource subset (local face: its cmap), never beyond the cmap; latin + latin-ext only, plus the local zh-ui subset; swap', () => {
  assert.deepEqual(checkFontsCss(UI_ROOT), []);
  const faces = parseFontFaces(readFileSync(join(UI_ROOT, 'css', 'fonts.css'), 'utf8'));
  assert.equal(faces.length, FONT_FACES.length);
  for (const face of FONT_FACES) {
    if (face.local) assert.equal(face.subset, 'zh-ui', face.file);
    else assert.ok(['latin', 'latin-ext'].includes(face.subset), face.file);
    const f = faces.find(x => x.file === face.file);
    assert.ok(f, face.file);
    assert.equal(f.display, 'swap');
    assert.equal(f.style, 'normal');
    assert.equal(f.url, `../fonts/${face.file}`);
    assert.match(f.src, /format\("woff2"\)/);
    const cmap = new Set(cmapCodepoints(readFileSync(fontSourcePath(UI_ROOT, face))));
    assert.deepEqual(f.codepoints.filter(c => !cmap.has(c)), [], `${face.file}: declared beyond cmap`);
    assert.deepEqual(f.codepoints, expectedRange(UI_ROOT, face), `${face.file}: = cmap ∩ subset`);
    if (face.local) assert.deepEqual(f.codepoints, [...cmap].sort((a, b) => a - b), `${face.file}: = its whole cmap`);
  }
  // weights: Instrument Sans variable 400–700 (covers 400/500/600), Geist Mono 400 and 500,
  // the zh voice 400 only (--voice-weight)
  const weights = fam => faces.filter(f => f.family === fam).map(f => f.weight).sort();
  assert.deepEqual(weights('Instrument Sans'), ['400 700', '400 700']);
  assert.deepEqual(weights('Geist Mono'), ['400', '400', '500', '500']);
  assert.deepEqual(weights('Tesseract Kai'), ['400']);
  // faces of the same family and weight overlap only where the Fontsource latin and
  // latin-ext ranges themselves overlap (the combining marks U+0304, U+0308, U+0329)
  const subsetOverlap = (() => {
    const u = JSON.parse(readFileSync(join(UI_ROOT, 'node_modules', '@fontsource', 'geist-mono', 'unicode.json'), 'utf8'));
    const a = new Set(parseUnicodeRange(u.latin));
    return new Set(parseUnicodeRange(u['latin-ext']).filter(c => a.has(c)));
  })();
  for (const fam of ['Instrument Sans', 'Geist Mono']) {
    for (const w of new Set(weights(fam))) {
      const sets = faces.filter(f => f.family === fam && f.weight === w).map(f => f.codepoints);
      const seen = new Set();
      for (const s of sets) for (const c of s) { assert.ok(!seen.has(c) || subsetOverlap.has(c), `${fam} ${w}: U+${c.toString(16)} in two faces`); seen.add(c); }
    }
  }
});

// Rewrite one declaration inside the @font-face rule that names `file`.
function editFace(css, file, prop, fn) {
  const at = css.indexOf(`url("../fonts/${file}")`);
  assert.ok(at > 0, file);
  const start = css.lastIndexOf('@font-face', at);
  const end = css.indexOf('}', at);
  const rule = css.slice(start, end);
  const re = new RegExp(`(${prop}:\\s*)([^;]+);`);
  assert.match(rule, re, `${file} ${prop}`);
  return css.slice(0, start) + rule.replace(re, (_, k, v) => `${k}${fn(v)};`) + css.slice(end);
}

test('checkFontsCss reports a broken fonts.css (not vacuous): beyond the cmap, short of it, wrong weight, no swap', () => {
  const good = readFileSync(join(UI_ROOT, 'css', 'fonts.css'), 'utf8');
  assert.deepEqual(checkFontsCss(UI_ROOT, good), [], 'the real file passes when fed as text');
  const [isExt, isLatin, , , , gmLatin500] = FONT_FACES;
  assert.ok(!expectedRange(UI_ROOT, isExt).includes(0x400), 'premise: U+0400 is not in the font');
  const only = (problems, file) => problems.filter(p => p.includes(file));

  // one code point beyond the cmap (Cyrillic U+0400) → both range checks fire
  let p = checkFontsCss(UI_ROOT, editFace(good, isExt.file, 'unicode-range', v => `U+0400, ${v}`));
  assert.equal(p.length, 2, p.join('\n'));
  assert.match(only(p, isExt.file)[0], /declares 1 code points the font does not map \(U\+0400\)/);
  assert.match(only(p, isExt.file)[1], /unicode-range ≠ cmap ∩ latin-ext subset/);

  // one mapped code point missing ('A') → only the equality check fires
  const want = expectedRange(UI_ROOT, isLatin);
  assert.ok(want.includes(0x41));
  p = checkFontsCss(UI_ROOT, editFace(good, isLatin.file, 'unicode-range', () => formatUnicodeRange(want.filter(c => c !== 0x41))));
  assert.equal(p.length, 1, p.join('\n'));
  assert.match(p[0], new RegExp(`${escape(isLatin.file)}: unicode-range ≠ cmap ∩ latin subset`));

  // wrong weight on Geist Mono 500 latin
  p = checkFontsCss(UI_ROOT, editFace(good, gmLatin500.file, 'font-weight', () => '600'));
  assert.deepEqual(p, [`fonts.css ${gmLatin500.file}: font-weight 600 ≠ 500`]);

  // font-display other than swap
  p = checkFontsCss(UI_ROOT, editFace(good, isLatin.file, 'font-display', () => 'block'));
  assert.deepEqual(p, [`fonts.css ${isLatin.file}: font-display must be swap`]);

  // the local face: 'A' declared (Latin stays in the UI face), and one mapped code point dropped
  const kaiRange = expectedRange(UI_ROOT, KAI);
  assert.ok(!kaiRange.includes(0x41) && kaiRange.length > 1, 'premise: Tesseract Kai maps no A');
  p = checkFontsCss(UI_ROOT, editFace(good, KAI.file, 'unicode-range', v => `U+0041, ${v}`));
  assert.equal(p.length, 2, p.join('\n'));
  assert.match(p[0], /declares 1 code points the font does not map \(U\+0041\)/);
  assert.match(p[1], new RegExp(`${escape(KAI.file)}: unicode-range ≠ the cmap of the local font`));
  p = checkFontsCss(UI_ROOT, editFace(good, KAI.file, 'unicode-range', () => formatUnicodeRange(kaiRange.slice(1))));
  assert.deepEqual(p.map(x => x.replace(/ \(expected .*$/, '')), [`fonts.css ${KAI.file}: unicode-range ≠ the cmap of the local font`]);

  // a face dropped, and a face for a file the build does not ship
  const dropped = good.slice(0, good.lastIndexOf('/*'));
  assert.ok(checkFontsCss(UI_ROOT, dropped).some(x => x.includes(`has no @font-face for ${FONT_FACES[FONT_FACES.length - 1].file}`)));
  p = checkFontsCss(UI_ROOT, good.replaceAll(`../fonts/${gmLatin500.file}`, '../fonts/geist-mono-cyrillic-500-normal.woff2'));
  assert.ok(p.some(x => x.includes('names geist-mono-cyrillic-500-normal.woff2, which the build does not ship')), p.join('\n'));
});

// A minimal ui root: only the files syncFonts reads, copied (no link into the real
// node_modules); a local font's files are sources in its ui/fonts/.
function tempFontRoot() {
  const root = mkdtempSync(join(tmpdir(), 'tesseract-ui-fonts-'));
  const copy = (from, to) => { mkdirSync(dirname(to), { recursive: true }); copyFileSync(from, to); };
  for (const face of FONT_FACES) copy(fontSourcePath(UI_ROOT, face), fontSourcePath(root, face));
  for (const p of FONT_PACKAGES) copy(licenseSourcePath(UI_ROOT, p), licenseSourcePath(root, p));
  return root;
}

// Several processes filling an empty (and then a stale) ui/fonts/ at once: no failure, no
// temp file left behind, every copy byte-identical to the package (Windows rename-over-open).
// "Empty" is a fresh checkout: ui/fonts/ holds only the local font's source files.
test('syncFonts: concurrent first copies and refreshes succeed and leave no temp files', { timeout: 120000 }, async () => {
  const root = tempFontRoot();
  try {
    const mod = pathToFileURL(join(UI_ROOT, 'build', 'fonts.mjs')).href;
    const script = `import(${JSON.stringify(mod)}).then(m => { m.syncFonts(${JSON.stringify(root)}); })`
      + `.catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });`;
    const run = () => new Promise(res => {
      const c = spawn(process.execPath, ['-e', script], { stdio: ['ignore', 'ignore', 'pipe'] });
      let err = '';
      c.stderr.on('data', d => { err += d; });
      c.on('close', code => res({ code, err }));
    });
    const dir = join(root, 'fonts');
    const expected = fontCopies(root).map(c => [c.name, readFileSync(c.source), !!c.local]);
    assert.ok(expected.some(([, , local]) => local) && expected.some(([, , local]) => !local), 'premise: copied and local files');
    for (let round = 0; round < 10; round++) {
      if (round % 2 === 0) { // fresh clone
        rmSync(dir, { recursive: true, force: true });
        mkdirSync(dir);
        for (const [name, bytes, local] of expected) if (local) writeFileSync(join(dir, name), bytes);
      } else for (const [name, , local] of expected) if (!local) writeFileSync(join(dir, name), 'stale'); // package upgraded
      const results = await Promise.all(Array.from({ length: 8 }, run));
      for (const r of results) assert.equal(r.code, 0, `round ${round}: ${r.err}`);
      assert.deepEqual(readdirSync(dir).filter(n => n.endsWith('.tmp')), [], `round ${round}: temp files left`);
      for (const [name, bytes] of expected) assert.ok(readFileSync(join(dir, name)).equals(bytes), `round ${round}: ${name}`);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('syncFonts: a local font is read from ui/fonts/, never written; a missing local file is an error', () => {
  const root = tempFontRoot();
  try {
    const locals = fontCopies(root).filter(c => c.local);
    assert.deepEqual(locals.map(c => c.name).sort(), [KAI.file, licenseFile(packageOf(KAI))].sort());
    for (const c of locals) assert.equal(c.source, join(root, 'fonts', c.name), `${c.name}: its own source`);
    // whatever the local file holds is what the build ships; syncFonts does not touch it
    const lic = join(root, 'fonts', licenseFile(packageOf(KAI)));
    writeFileSync(lic, 'edited by hand');
    const out = syncFonts(root);
    assert.deepEqual(out.map(o => o.name), fontCopies(root).map(c => c.name));
    assert.equal(String(out.find(o => o.name === licenseFile(packageOf(KAI))).bytes), 'edited by hand');
    assert.equal(readFileSync(lic, 'utf8'), 'edited by hand');
    // a missing local file: a clear error, not a copy attempt
    rmSync(join(root, 'fonts', KAI.file));
    assert.throws(() => syncFonts(root), new RegExp(`${escape(KAI.file)} is missing; it is a source file of the local font "Tesseract Kai" \\(written by tools/zh_voice_font\\.py\\)`));
    assert.ok(!existsSync(join(root, 'fonts', KAI.file)));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('V11: the shipped subsets map no Cyrillic (U+0400–04FF) and no Greek (U+0370–03FF)', () => {
  const rows = coverage(UI_ROOT);
  assert.deepEqual(rows.map(r => r.family), ['Instrument Sans', 'Geist Mono', 'Tesseract Kai']);
  for (const r of rows) for (const b of BLOCKS) assert.equal(r.shippedBlocks[b.name], 0, `${r.family} ${b.name}`);
  // upstream: Instrument Sans has no Cyrillic at all; Geist Mono has a separate cyrillic subset (not shipped)
  assert.equal(rows[0].packageBlocks.Cyrillic, 0);
  assert.ok(rows[1].packageBlocks.Cyrillic > 0);
  assert.equal(rows[1].packageBlocks.Greek, 0);
  // the local font has no package to compare with; its row reports the shipped file only
  assert.equal(rows[2].local, true);
  assert.equal(rows[2].packageBlocks, null);
  assert.deepEqual(rows[2].names, ['Tesseract Kai']);
  assert.ok(rows[2].ideographs > 500 && rows[2].ideographs < rows[2].codepoints, `${rows[2].ideographs} ideographs of ${rows[2].codepoints}`);
});

// ---- the zh voice subset (tools/zh_voice_font.py) -------------------------------------
// The character classes of the subset: VOICE_RANGES and VOICE_EXTRA of tools/zh_voice_font.py
// (checked below). Latin, digits and the rest stay in the UI face.
const VOICE_RANGES = [[0x3000, 0x303F], [0x3400, 0x4DBF], [0x4E00, 0x9FFF], [0xFF00, 0xFFEF]];
const VOICE_EXTRA = [0x00B7, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026];
const inVoice = c => VOICE_EXTRA.includes(c) || VOICE_RANGES.some(([lo, hi]) => c >= lo && c <= hi);
// The OFL Reserved Font Names of LXGW WenKai GB Screen, and the name IDs that identify a font
// (family, subfamily, unique id, full, PostScript, typographic, compatible full, CID, WWS, variations prefix).
const RESERVED_FONT_NAMES = ['霞鹜', '霞鶩', '落霞孤鹜', '落霞孤鶩', 'LXGW'];
const IDENTIFYING_IDS = [1, 2, 3, 4, 6, 16, 17, 18, 20, 21, 22, 25];

test('zh voice: Tesseract Kai maps every CJK character of the zh_CN messages, and nothing outside those classes', () => {
  assert.ok(KAI, 'FONT_FACES has the local Tesseract Kai face');
  const cmap = new Set(cmapCodepoints(readFileSync(fontSourcePath(UI_ROOT, KAI))));
  const used = new Map(); // code point → first message id that uses it
  for (const [id, e] of Object.entries(readCatalogs(UI_ROOT).zh_CN || {})) {
    if (typeof e.text !== 'string') continue;
    for (const ch of e.text) { const c = ch.codePointAt(0); if (inVoice(c) && !used.has(c)) used.set(c, id); }
  }
  assert.ok(used.size > 500, `premise: the zh_CN messages use ${used.size} such characters`);
  const missing = [...used].filter(([c]) => !cmap.has(c)).map(([c, id]) => `U+${hex(c)} ${String.fromCodePoint(c)} (${id})`);
  assert.deepEqual(missing, [], `ui/fonts/${KAI.file} lacks them: run \`python -I ui/tools/zh_voice_font.py\``);
  assert.deepEqual([...cmap].filter(c => !inVoice(c)).map(c => `U+${hex(c)}`), [], 'mapped outside the voice classes');
});

test('zh voice: the character classes here are the ones tools/zh_voice_font.py uses', () => {
  const py = readFileSync(join(UI_ROOT, 'tools', 'zh_voice_font.py'), 'utf8');
  const ranges = /^VOICE_RANGES = \(\r?\n([\s\S]*?)^\)/m.exec(py);
  assert.ok(ranges, 'VOICE_RANGES in zh_voice_font.py');
  assert.deepEqual([...ranges[1].matchAll(/\(0x([0-9A-F]+), 0x([0-9A-F]+)\)/gi)].map(m => [parseInt(m[1], 16), parseInt(m[2], 16)]), VOICE_RANGES);
  const extra = /^VOICE_EXTRA = \(([^)]*)\)/m.exec(py);
  assert.ok(extra, 'VOICE_EXTRA in zh_voice_font.py');
  assert.deepEqual([...extra[1].matchAll(/0x([0-9A-F]+)/gi)].map(m => parseInt(m[1], 16)), VOICE_EXTRA);
  for (const rfn of RESERVED_FONT_NAMES) assert.ok(py.includes(`"${rfn}"`), `zh_voice_font.py checks ${rfn}`);
});

test('zh voice: Tesseract Kai is renamed (no Reserved Font Name identifies it), copyright and licence kept', () => {
  const names = nameRecords(readFileSync(fontSourcePath(UI_ROOT, KAI)));
  const get = id => names.filter(r => r.nameId === id).map(r => r.text);
  assert.deepEqual(get(1), ['Tesseract Kai']);
  assert.deepEqual(get(2), ['Regular']);
  assert.deepEqual(get(4), ['Tesseract Kai Regular']);
  assert.deepEqual(get(6), ['TesseractKai-Regular']);
  assert.match(get(3)[0] ?? '', /^Tesseract Kai Regular; /);
  for (const r of names.filter(x => IDENTIFYING_IDS.includes(x.nameId))) {
    const where = `name ${r.nameId} (platform ${r.platform}, language 0x${hex(r.language)})`;
    assert.ok(r.platform === 3 && r.language === 0x409, `${where}: identifying names are English (Windows) only`);
    for (const rfn of RESERVED_FONT_NAMES) assert.ok(!r.text.toLowerCase().includes(rfn.toLowerCase()), `${where} contains ${rfn}: ${r.text}`);
  }
  // copyright (0) and licence (13, 14) are the upstream's; the copyright lines are the shipped OFL text's
  const ofl = readFileSync(licenseSourcePath(UI_ROOT, packageOf(KAI)), 'utf8');
  assert.equal(get(0).length, 1);
  for (const line of get(0)[0].split('\n')) assert.ok(ofl.includes(line), `the OFL text has the copyright line ${line.slice(0, 60)}…`);
  assert.match(get(13)[0] ?? '', /SIL Open Font License, Version 1\.1/);
  assert.equal(get(14).length, 1);
});

// ---- per-language stacks (css/tokens.css) ---------------------------------------------
function stacks() {
  const css = readFileSync(join(UI_ROOT, 'css', 'tokens.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const blocks = {};
  for (const m of css.matchAll(/([^{}@]+?)\s*\{([^{}]*)\}/g)) {
    const sel = m[1].trim();
    const vars = {};
    for (const d of m[2].matchAll(/(--font-[a-z]+)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].split(',').map(s => s.trim().replace(/^"|"$/g, ''));
    if (Object.keys(vars).length) blocks[sel] = { ...(blocks[sel] || {}), ...vars };
  }
  return blocks;
}

test('font stacks per language: western default, zh, ja (Yu Gothic before any YaHei), ko, ru (system fonts only)', () => {
  const s = stacks();
  assert.deepEqual(Object.keys(s).sort(), [':root', ':root:lang(ja)', ':root:lang(ko)', ':root:lang(ru)', ':root:lang(zh)']);
  const resolved = sel => ({ ...s[':root'], ...(s[sel] || {}) });
  assert.deepEqual(s[':root']['--font-sans'], ['Instrument Sans', 'Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', 'sans-serif']);
  assert.deepEqual(s[':root:lang(zh)']['--font-sans'], ['Instrument Sans', 'Noto Sans SC', 'Microsoft YaHei UI', 'sans-serif']);
  assert.ok(s[':root:lang(zh)']['--font-voice'].includes(KAI.family), 'the zh voice names the bundled kai (css/fonts.css)');
  assert.deepEqual(s[':root:lang(ja)']['--font-sans'], ['Instrument Sans', 'Yu Gothic UI', 'Meiryo UI', 'Microsoft YaHei UI', 'sans-serif']);
  assert.deepEqual(s[':root:lang(ko)']['--font-sans'], ['Instrument Sans', 'Malgun Gothic', 'Microsoft YaHei UI', 'sans-serif']);
  assert.deepEqual(s[':root:lang(ru)']['--font-sans'], ['Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', 'sans-serif']);
  assert.deepEqual(s[':root:lang(ru)']['--font-mono'], ['Cascadia Mono', 'Consolas', 'Microsoft YaHei UI', 'monospace']);
  for (const sel of [':root', ':root:lang(zh)', ':root:lang(ja)', ':root:lang(ko)', ':root:lang(ru)']) {
    const r = resolved(sel);
    for (const v of ['--font-sans', '--font-mono', '--font-brand']) {
      const st = r[v];
      assert.ok(st && st.length, `${sel} ${v}`);
      assert.match(st[st.length - 1], /^(sans-serif|monospace)$/, `${sel} ${v} ends with a generic family`);
      // ja / ko: YaHei, if present, only after the language's own CJK fonts (architecture §8)
      const own = { ':root:lang(ja)': ['Yu Gothic UI', 'Meiryo UI'], ':root:lang(ko)': ['Malgun Gothic'] }[sel];
      if (own) {
        const ya = st.findIndex(f => /YaHei/i.test(f));
        for (const f of own) if (v !== '--font-brand') assert.ok(st.indexOf(f) !== -1 && (ya === -1 || st.indexOf(f) < ya), `${sel} ${v}: ${f} must precede YaHei`);
        if (v === '--font-brand') assert.equal(ya, -1, `${sel} brand names YaHei`);
      }
      if (sel === ':root:lang(ru)') assert.ok(!st.some(f => /Instrument Sans|Geist Mono/.test(f)), `ru ${v} uses a web font`);
      // western / ru: YaHei only as the last named font, for Chinese data text (决定_第四波裁定 08a-3); never in brand
      if (v === '--font-brand') assert.ok(!st.some(f => /YaHei/i.test(f)), `${sel} ${v} names YaHei`);
      else if (!['zh', 'ja', 'ko'].some(x => sel === `:root:lang(${x})`)) {
        const ya = st.findIndex(f => /YaHei/i.test(f));
        assert.equal(ya, st.length - 2, `${sel} ${v}: YaHei must be the last named font`);
      }
    }
  }
});

// ---- no external resources -------------------------------------------------------------
test('neither target references an external resource (googleapis, gstatic, http(s)://, //)', async () => {
  for (const target of ['app', 'design']) {
    const r = await get(target);
    assert.deepEqual(externalProblems(r.files), [], target);
    assert.ok(!/fonts\.googleapis|fonts\.gstatic|rel="preconnect"/.test(text(r, 'index.html')), `${target}: index.html`);
  }
  // the template: no Google host, and no absolute resource reference (link text and <a href> are fine)
  const template = readFileSync(join(UI_ROOT, 'src', 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  assert.ok(!/googleapis|gstatic/.test(template), 'template names a Google font host');
  assert.deepEqual(htmlRefs(template).filter(ref => ABSOLUTE.test(ref) && !/^\s*data:/i.test(ref)), [], 'template: external resource reference');
});

test('the external-reference scanner catches what it must and ignores link text', () => {
  const enc = new TextEncoder();
  const bundle = o => new Map(Object.entries(o).map(([k, v]) => [k, enc.encode(v)]));
  const probs = o => externalProblems(bundle(o));
  // must catch
  assert.equal(probs({ 'index.html': '<link rel="preconnect" href="https://fonts.example.net">' }).length, 1);
  assert.equal(probs({ 'index.html': '<script src="//cdn.example.net/x.js"></script>' }).length, 1);
  assert.equal(probs({ 'index.html': '<a href="#x"><img src="https://x.example/a.png"></a>' }).length, 1, 'img inside a link');
  assert.equal(probs({ 'index.html': '<abbr href="https://x.example/a"></abbr>' }).length, 1, 'only <a>/<area> href is link text');
  assert.equal(probs({ 'index.html': '<img srcset="a.png 1x, http://x.example/b.png 2x">', 'a.png': '' }).length, 1);
  assert.equal(probs({ 'a.css': '@font-face{src:url(https://x.example/f.woff2)}' }).length, 1);
  assert.equal(probs({ 'a.css': "@import 'http://x.example/a.css';" }).length, 1);
  assert.equal(probs({ 'a.css': '.x{background:url(missing.png)}' }).length, 1, 'relative ref to a missing file');
  assert.ok(probs({ 'a.css': '/* fonts.googleapis.com */' }).length >= 1, 'googleapis anywhere');
  assert.equal(probs({ 'a.js': 'fetch("https://x.example/a")' }).length, 1);
  assert.equal(probs({ 'a.js': 'el.innerHTML=`<img src="https://x.example/a.png">`' }).length, 1);
  assert.equal(probs({ 'a.js': 'img.src = "//x.example/a.png"' }).length, 1);
  assert.equal(probs({ 'a.js': 's.textContent="@font-face{src:url(\'https://x.example/f\')}"' }).length, 1);
  assert.equal(probs({ 'a.js': 'new FontFace("x","url(https://x.example/f.woff2)")' }).length, 1);
  // must ignore
  assert.deepEqual(probs({
    'index.html': '<a href="#top">x</a><a class="doc" href="https://example.org/doc">doc</a><area href="https://example.org/m"><link rel="stylesheet" href="app.css"><!-- <link href="https://x.example"> -->',
    'app.css': '.i{background:url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\'%3E%3C/svg%3E")} .g{fill:url(#grad)}',
    'a.js': 'const ph = "https://hooks.example.com/x"; T.html`<span class="mono">https://link.example.org/tg</span><a href="https://example.org">doc</a>`;',
    'assets/OFL-x.ABCDEFGH.txt': 'http://scripts.sil.org/OFL',
  }), []);
  // (T-P5-04) catalog text in a language file is message text, not a reference: the ko_KR hint
  // 「…구독 URL(http:// 또는 https://)」 (settings.bastion.src.sub.hint) and the like are ignored …
  const hint = { 'settings.bastion.src.sub.hint': '공개 노드 구독 URL(http:// 또는 https://)', 'x.y': 'fetch("https://x.example/a") <img src="https://x.example/a.png">', 'x.z': ['⟦', 'url(//x.example/f)', '⟧'] };
  assert.deepEqual(probs({ 'locale-ko_KR.ABCDEFGH.js': localeScript('ko_KR', hint) }), [], 'catalog text in a language file');
  // … but not in any other script (the same text in app code is still caught) …
  assert.equal(probs({ 'app.ABCDEFGH.js': 'const h = "공개 노드 구독 URL(http:// 또는 https://)";' }).length, 1, 'the same text in app code');
  assert.ok(probs({ 'locale-ko_KR.ABCDEFGH.txt.js': localeScript('ko_KR', hint) }).length >= 1, 'not a language file name');
  // … nor when a language file is not exactly the wrapper around a JSON catalog
  assert.equal(probs({ 'locale-ko_KR.ABCDEFGH.js': 'fetch("https://x.example/a")' }).length, 1, 'language file name, other content');
  assert.equal(probs({ 'locale-ko_KR.ABCDEFGH.js': localeScript('ko_KR', {}) + 'new Worker("https://x.example/w.js");' }).length, 1, 'code after the wrapper');
  assert.equal(probs({ 'locale-ko_KR.ABCDEFGH.js': localeScript('ko_KR', {}).replace('a=["ko_KR",{}]', 'a=["ko_KR",{},fetch("https://x.example/a")]') }).length, 1, 'code inside the array');
  // CSS that really references an http resource is still caught, wherever it sits
  assert.equal(probs({ 'app.ABCDEFGH.css': '.x{background:url(http://x.example/a.png)}' }).length, 1, 'CSS url(http://…)');
  assert.equal(probs({ 'app.ABCDEFGH.css': '.x{background:URL( "https://x.example/a.png" )}' }).length, 1, 'CSS URL( "https://…" )');
});
