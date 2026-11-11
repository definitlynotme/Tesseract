// Tesseract UI build (T-P4-01; P4 architecture §1.2).
//
//   node build.mjs --target app|design [--out <dir>] [--clean] [--check]
//                  [--pseudo] [--i18n-trace] [--h-features all] [--dev]
//
// --out      output directory (default ui/dist/<target>), relative to the current directory
// --clean    empty the output directory first; only an empty directory, a previous build
//            (build-manifest.json) or a directory under ui/dist/** or shell/app/ui
// --check    build both targets in memory (nothing written) and run the bundle gates
//            (demo markers, inline script/style, manifest, byte-identical rebuild); it
//            also refreshes the ui/fonts/ copies from node_modules (T-P4-08a)
// --pseudo / --i18n-trace / --dev   passed to build/locales.mjs (reserved; no-op in T-P4-01)
// --h-features all   design only: treat every registered feature name as enabled
//
// Same sources → byte-identical output, including build-manifest.json (no build time is
// recorded; `source_date` only appears when SOURCE_DATE_EPOCH is set).
import * as esbuild from 'esbuild';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import prepareLocales from './build/locales.mjs';
import prepareFonts from './build/fonts.mjs';
import { loadDemoMarkers, loadGates, readFeatureRegistry, scanDemoFiles, summarize } from './tools/scan.mjs';

export const UI_ROOT = dirname(fileURLToPath(import.meta.url));
export const TARGETS = ['app', 'design'];
const REPO_ROOT = resolve(UI_ROOT, '..');
const posix = p => p.split(sep).join('/');
const sha256 = buf => createHash('sha256').update(buf).digest('hex');
const enc = new TextEncoder();
const dec = new TextDecoder();

export function parseArgs(argv) {
  const o = { target: null, out: null, clean: false, check: false, pseudo: false, i18nTrace: false, hFeatures: null, dev: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--target') o.target = argv[++i];
    else if (a === '--out') o.out = argv[++i];
    else if (a === '--clean') o.clean = true;
    else if (a === '--check') o.check = true;
    else if (a === '--pseudo') o.pseudo = true;
    else if (a === '--i18n-trace') o.i18nTrace = true;
    else if (a === '--dev') o.dev = true;
    else if (a === '--h-features') o.hFeatures = argv[++i];
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!o.check && !TARGETS.includes(o.target)) throw new Error('--target app|design is required');
  if (o.check && o.target && !TARGETS.includes(o.target)) throw new Error('--target must be app|design');
  if (o.hFeatures !== null && o.hFeatures !== 'all') throw new Error('--h-features only accepts "all"');
  if (o.hFeatures === 'all' && o.target === 'app') throw new Error('--h-features all is only valid for the design target');
  return o;
}

function readJson(p) { return JSON.parse(readFileSync(p, 'utf8').replace(/^﻿/, '')); }

// ENABLED_H_FEATURES (architecture §5): features.json, or every registered name with --h-features all.
export function hFeaturesFor(target, hFeatures, root = UI_ROOT) {
  if (hFeatures === 'all') {
    if (target !== 'design') throw new Error('--h-features all is only valid for the design target');
    return [...new Set(readFeatureRegistry(root).map(r => r.name))].sort();
  }
  const list = readJson(join(root, 'src', 'features.json'));
  if (!Array.isArray(list) || list.some(x => typeof x !== 'string')) throw new Error('src/features.json must be an array of strings');
  return list;
}

export function defineFor(target, hFeatures = []) {
  return {
    __DEMO__: target === 'design' ? 'true' : 'false',
    __TARGET__: JSON.stringify(target),
    // A JSON *string* literal (like __LOCALES__, build/locales.mjs): an array define would make
    // esbuild write a `// <define:__H_FEATURES__>` comment into the unminified design bundle.
    // Read with parseFeatureDefine (src/rt/features.js).
    __H_FEATURES__: JSON.stringify(JSON.stringify(hFeatures)),
    __LOCALES__: '{}',
  };
}

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out); else out.push(p);
  }
  return out;
}

// Content digest of everything that shapes the bundle: ui/src (incl. the language
// catalogs under src/i18n), ui/css, build.mjs, build/* and package-lock.json (the
// pinned esbuild version changes the output too). Paths are relative and sorted, so
// the digest is independent of checkout location.
export function sourceDigest(root = UI_ROOT) {
  const files = [...walk(join(root, 'src')), ...walk(join(root, 'css')), ...walk(join(root, 'build')),
    ...[join(root, 'build.mjs'), join(root, 'package-lock.json')].filter(p => existsSync(p))]
    .map(p => posix(relative(root, p))).sort();
  const h = createHash('sha256');
  for (const f of files) h.update(f + '\0' + sha256(readFileSync(join(root, f))) + '\n');
  return h.digest('hex');
}

export function renderHtml(template, { css, scripts }, name = 'src/index.html') {
  const cssTags = css.map(f => `<link rel="stylesheet" href="${f}">`).join('\n');
  const jsTags = scripts.map(f => `<script src="${f}"></script>`).join('\n');
  for (const marker of ['<!-- @build:css -->', '<!-- @build:js -->']) {
    if (template.split(marker).length !== 2) throw new Error(`${name} must contain ${marker} exactly once`);
  }
  return template.replace('<!-- @build:css -->', cssTags).replace('<!-- @build:js -->', jsTags);
}

// The notice card window's page (10-30 notice redesign; shell/app/src/notice.rs loads
// notice.html): built for the app target only, next to index.html, with its own bundle
// (notice.<hash>.js / notice.<hash>.css), the same language files and fonts. A ui root without
// src/notice/ (the temp roots of the unit tests) builds without it.
export const NOTICE_DIR = join('src', 'notice');
export const hasNotice = (target, root = UI_ROOT) => target === 'app' && existsSync(join(root, NOTICE_DIR, 'notice.html'));

// Build one target in memory. Returns {files: Map(path → Uint8Array), manifest}.
export async function buildTarget({ target, pseudo = false, i18nTrace = false, hFeatures = null, dev = false, root = UI_ROOT } = {}) {
  if (!TARGETS.includes(target)) throw new Error(`unknown target: ${target}`);
  const features = hFeaturesFor(target, hFeatures, root);
  const ctx = { target, uiRoot: root, pseudo, i18nTrace, dev };
  const loc = (await prepareLocales(ctx)) || {};
  const fonts = (await prepareFonts(ctx)) || {};
  const define = { ...defineFor(target, features), ...(loc.define || {}), ...(fonts.define || {}) };
  const design = target === 'design';
  const notice = hasNotice(target, root);
  const entryPoints = [
    { in: join(root, 'src', 'entry', `${target}.js`), out: target },
    { in: join(root, 'css', 'index.css'), out: 'app' },
    ...(design ? [{ in: join(root, 'css', 'design.css'), out: 'design' }] : []),
    ...(notice ? [{ in: join(root, NOTICE_DIR, 'notice.js'), out: 'notice' }, { in: join(root, NOTICE_DIR, 'notice.css'), out: 'notice' }] : []),
  ];
  const result = await esbuild.build({
    absWorkingDir: root,
    entryPoints,
    bundle: true,
    write: false,
    outdir: join(root, 'dist', target),
    format: 'iife',
    target: 'chrome120',
    charset: 'utf8',
    legalComments: 'eof',
    minify: !design,
    sourcemap: design ? 'linked' : false,
    entryNames: '[name].[hash]',
    assetNames: 'assets/[name].[hash]',
    define,
    loader: { ...(fonts.loader || {}), ...(loc.loader || {}) },
    plugins: [...(loc.plugins || []), ...(fonts.plugins || [])],
    logLevel: 'warning',
    metafile: false,
  });
  const outDir = join(root, 'dist', target);
  const files = new Map();
  for (const f of result.outputFiles) files.set(posix(relative(outDir, f.path)), f.contents);
  for (const f of [...(loc.files || []), ...(fonts.files || [])]) {
    files.set(f.path, typeof f.contents === 'string' ? enc.encode(f.contents) : f.contents);
  }
  const names = [...files.keys()];
  const pick = re => names.filter(n => re.test(n)).sort();
  const css = [...pick(/^app\.[A-Z0-9]+\.css$/), ...(design ? pick(/^design\.[A-Z0-9]+\.css$/) : [])];
  const main = pick(new RegExp(`^${target}\\.[A-Z0-9]+\\.js$`));
  if (css.length !== (design ? 2 : 1) || main.length !== 1) throw new Error(`unexpected esbuild outputs: ${names.join(', ')}`);
  const template = readFileSync(join(root, 'src', 'index.html'), 'utf8');
  files.set('index.html', enc.encode(renderHtml(template, { css, scripts: [...(loc.scripts || []), ...main] })));
  if (notice) {
    const ncss = pick(/^notice\.[A-Z0-9]+\.css$/), njs = pick(/^notice\.[A-Z0-9]+\.js$/);
    if (ncss.length !== 1 || njs.length !== 1) throw new Error(`unexpected esbuild outputs for the notice page: ${names.join(', ')}`);
    const page = readFileSync(join(root, NOTICE_DIR, 'notice.html'), 'utf8');
    files.set('notice.html', enc.encode(renderHtml(page, { css: ncss, scripts: [...(loc.scripts || []), ...njs] }, 'src/notice/notice.html')));
  }
  const manifest = {
    target,
    ...(process.env.SOURCE_DATE_EPOCH ? { source_date: Number(process.env.SOURCE_DATE_EPOCH) } : {}),
    source_digest: sourceDigest(root),
    files: [...files.keys()].sort().map(path => ({ path, sha256: sha256(files.get(path)), bytes: files.get(path).byteLength })),
    locales: loc.locales || {},
    demo_free: target === 'app' && demoFindings(files, root).length === 0,
    h_features: features,
  };
  files.set('build-manifest.json', enc.encode(JSON.stringify(manifest, null, 2) + '\n'));
  return { files, manifest };
}

// Demo-marker findings over the top-level *.js / *.css of an in-memory bundle (the files
// the app target embeds; architecture §6). demo_free is computed from this, so it is true
// exactly when the bundle passes the demo gate. Without tools/demo_markers.json nothing
// can be certified: a single synthetic finding keeps demo_free false.
export function demoFindings(files, root = UI_ROOT) {
  if (!existsSync(join(root, 'tools', 'demo_markers.json'))) return [{ rule: 'demo', file: 'tools/demo_markers.json', line: 1, col: 1, marker: '<missing marker list>', count: 1 }];
  const top = [...files.keys()].filter(p => !p.includes('/') && /\.(js|css)$/.test(p)).sort();
  return scanDemoFiles(top.map(p => ({ path: p, text: dec.decode(files.get(p)) })), loadDemoMarkers(root));
}

const inside = (parent, child) => { const r = relative(parent, child); return r === '' || (!r.startsWith('..') && !isAbsolute(r)); };

// --clean is an allow list: it only empties a directory that is empty, that holds a
// previous build (build-manifest.json at its top), or that lies under ui/dist/** or
// shell/app/ui. On top of that it never touches a directory that is or contains the
// repo, the ui root or its sources, or that looks like a source tree.
export function assertCleanable(dir) {
  const d = resolve(dir);
  const protectedDirs = [REPO_ROOT, UI_ROOT, join(UI_ROOT, 'src'), join(UI_ROOT, 'css'), join(UI_ROOT, 'tools'), join(UI_ROOT, 'test'), join(UI_ROOT, 'dist'), resolve('/')];
  for (const p of protectedDirs) {
    if (inside(d, p)) throw new Error(`refusing to clean ${d}: it is or contains ${p}`);
  }
  if (existsSync(join(d, '.git')) || existsSync(join(d, 'package.json'))) throw new Error(`refusing to clean ${d}: looks like a source tree`);
  const allowed = readdirSync(d).length === 0
    || existsSync(join(d, 'build-manifest.json'))
    || inside(join(UI_ROOT, 'dist'), d)
    || inside(join(REPO_ROOT, 'shell', 'app', 'ui'), d);
  if (!allowed) throw new Error(`refusing to clean ${d}: not empty, no build-manifest.json, and not under ui/dist or shell/app/ui`);
}

export function writeOutput(dir, files, { clean = false } = {}) {
  const d = resolve(dir);
  if (clean && existsSync(d)) { assertCleanable(d); rmSync(d, { recursive: true, force: true }); }
  for (const path of [...files.keys()].sort()) {
    const p = join(d, ...path.split('/'));
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, files.get(path));
  }
}

// ---- --check: bundle gates over in-memory output ------------------------------------
// Everything a CSP without 'unsafe-inline' blocks (architecture §9): inline script bodies,
// inline event handler attributes, javascript: URLs, style attributes and elements.
// HTML comments are ignored (they never execute or apply).
const INLINE_SCRIPT = /<script\b(?![^>]*\ssrc\s*=)[^>]*>\s*\S/i;
const EVENT_ATTR = /<[a-zA-Z][^>]*\son[a-z]+\s*=/i;
const JS_URL = /<[a-zA-Z][^>]*\s(?:href|src|action|formaction|xlink:href|data)\s*=\s*["']?\s*javascript:/i;
const STYLE_ATTR = /<[a-zA-Z][^>]*\sstyle\s*=/i;
const STYLE_ELEMENT = /<style\b/i;

export function htmlProblems(html) {
  const s = String(html).replace(/<!--[\s\S]*?-->/g, '');
  const out = [];
  if (INLINE_SCRIPT.test(s)) out.push('inline <script> body in index.html');
  if (EVENT_ATTR.test(s)) out.push('inline on*= event handler attribute in index.html');
  if (JS_URL.test(s)) out.push('javascript: URL in index.html');
  if (STYLE_ATTR.test(s)) out.push('style= attribute in index.html');
  if (STYLE_ELEMENT.test(s)) out.push('<style> element in index.html');
  return out;
}

function sameFiles(a, b) {
  if (a.size !== b.size) return false;
  for (const [k, v] of a) { const w = b.get(k); if (!w || Buffer.compare(Buffer.from(v), Buffer.from(w)) !== 0) return false; }
  return true;
}

export async function check(opts = {}) {
  const root = opts.root || UI_ROOT;
  const gates = loadGates(root);
  const problems = [];
  const lines = [];
  for (const target of TARGETS) {
    const a = await buildTarget({ ...opts, target, hFeatures: null, root });
    const b = await buildTarget({ ...opts, target, hFeatures: null, root });
    if (!sameFiles(a.files, b.files)) problems.push(`${target}: two builds of the same sources differ`);
    problems.push(...htmlProblems(dec.decode(a.files.get('index.html'))).map(p => `${target}: ${p}`));
    if (a.files.has('notice.html')) problems.push(...htmlProblems(dec.decode(a.files.get('notice.html'))).map(p => `${target}: notice.html: ${p.replace('index.html', 'notice.html')}`));
    for (const f of a.manifest.files) {
      if (sha256(a.files.get(f.path)) !== f.sha256) problems.push(`${target}: manifest sha256 mismatch for ${f.path}`);
    }
    const total = [...a.files.values()].reduce((n, v) => n + v.byteLength, 0);
    lines.push(`[build] ${target}: ${a.files.size} files, ${total} bytes, deterministic=${sameFiles(a.files, b.files)}`);
    if (target === 'app') {
      const mode = gates.demo || 'report';
      const findings = demoFindings(a.files, root);
      const sum = summarize({ findings });
      lines.push(`[scan] demo (${mode}): ${sum.count}, demo_free=${a.manifest.demo_free}`);
      for (const f of findings) lines.push(`  ${f.file}: ${f.marker} × ${f.count}`);
      if (mode === 'enforce') {
        if (sum.count) problems.push(`app: demo markers in bundle (${sum.count})`);
        if (!a.manifest.demo_free) problems.push('app: build-manifest.demo_free is false');
      }
    }
  }
  for (const l of lines) console.log(l);
  return problems;
}

export async function main(argv = process.argv.slice(2)) {
  let o;
  try { o = parseArgs(argv); } catch (e) { console.error(`build.mjs: ${e.message}`); return 2; }
  try {
    if (o.check) {
      const problems = await check({ pseudo: o.pseudo, i18nTrace: o.i18nTrace, dev: o.dev });
      for (const p of problems) console.error(`[build --check] ${p}`);
      return problems.length ? 1 : 0;
    }
    const { files, manifest } = await buildTarget({ target: o.target, pseudo: o.pseudo, i18nTrace: o.i18nTrace, hFeatures: o.hFeatures, dev: o.dev });
    const out = o.out ? resolve(o.out) : join(UI_ROOT, 'dist', o.target);
    writeOutput(out, files, { clean: o.clean });
    const bytes = manifest.files.reduce((n, f) => n + f.bytes, 0);
    console.log(`[build] ${o.target} → ${out} (${files.size} files, ${bytes} bytes + manifest)`);
    return 0;
  } catch (e) {
    if (e && Array.isArray(e.errors)) for (const m of e.errors) console.error(`build.mjs: ${m.text}${m.location ? ` (${m.location.file}:${m.location.line})` : ''}`);
    else console.error(`build.mjs: ${e && e.stack || e}`);
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
