// Build plugin hook: self-hosted fonts (owner: T-P4-08a; P4 architecture §1.2, §8).
// Interface: see src/boot/README.md ("构建插件接口").
//
// Nothing is downloaded. Two kinds of font, both served from ui/fonts/:
// - Fontsource fonts come from the pinned devDependencies (package-lock.json). prepare()
//   copies the woff2 files that css/fonts.css uses, and each family's OFL text, from
//   node_modules into ui/fonts/ (only when the bytes differ).
// - A local font (pkg: null, local: true) has no package: its woff2 and OFL text are source
//   files in ui/fonts/, which prepare() reads and never writes. The one local font is
//   "Tesseract Kai", the zh voice (css/tokens.css): a renamed subset of LXGW WenKai GB Screen
//   cut by tools/zh_voice_font.py to the CJK characters of the zh_CN catalogs (re-run it
//   when they gain new ones; test/unit/fonts.test.mjs fails until then).
// esbuild emits the woff2 files through loader 'file' (assets/<name>.<hash>.woff2, the url()
// in the CSS bundle is rewritten to it) and prepare() adds the OFL texts as
// assets/OFL-<id>.<hash>.txt. A ui root without css/fonts.css (the temp roots in
// test/unit/build.test.mjs) gets the no-op result.
//
// ui/fonts/ is refreshed by EVERY call of prepare(), i.e. also by `build.mjs --check`
// ("nothing written" there means: no output directory) and by the unit tests that call
// buildTarget(). That is the only write into the source tree; it is a no-op once the
// copies match node_modules, and concurrent builds are safe (see syncFonts()).
//
// Of the Fontsource fonts only the latin and latin-ext subsets are shipped (architecture
// §8): Instrument Sans as one variable file per subset (wght 400–700, covers 400/500/600),
// Geist Mono 400 and 500. The unicode-range of every @font-face in css/fonts.css is the
// font's real cmap intersected with the Fontsource subset range (a local font: its whole
// cmap); tools/font_cmap.mjs --report and test/unit/fonts.test.mjs check that.
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { threadId } from 'node:worker_threads';

export const FONT_PACKAGES = [
  { id: 'instrument-sans', family: 'Instrument Sans', pkg: '@fontsource-variable/instrument-sans', license: 'OFL-1.1' },
  { id: 'geist-mono', family: 'Geist Mono', pkg: '@fontsource/geist-mono', license: 'OFL-1.1' },
  // local: ui/fonts/tesseract-kai-zh.woff2 and ui/fonts/OFL-tesseract-kai.txt are the sources
  { id: 'tesseract-kai', family: 'Tesseract Kai', pkg: null, local: true, license: 'OFL-1.1',
    upstream: 'LXGW WenKai GB Screen', version: '1.530', url: 'https://github.com/lxgw/LxgwWenKai-Screen', tool: 'tools/zh_voice_font.py' },
];

// One entry per @font-face in css/fonts.css (same order). A local face belongs to the local
// package of the same family.
export const FONT_FACES = [
  { pkg: '@fontsource-variable/instrument-sans', family: 'Instrument Sans', subset: 'latin-ext', weight: '400 700', file: 'instrument-sans-latin-ext-wght-normal.woff2' },
  { pkg: '@fontsource-variable/instrument-sans', family: 'Instrument Sans', subset: 'latin', weight: '400 700', file: 'instrument-sans-latin-wght-normal.woff2' },
  { pkg: '@fontsource/geist-mono', family: 'Geist Mono', subset: 'latin-ext', weight: '400', file: 'geist-mono-latin-ext-400-normal.woff2' },
  { pkg: '@fontsource/geist-mono', family: 'Geist Mono', subset: 'latin', weight: '400', file: 'geist-mono-latin-400-normal.woff2' },
  { pkg: '@fontsource/geist-mono', family: 'Geist Mono', subset: 'latin-ext', weight: '500', file: 'geist-mono-latin-ext-500-normal.woff2' },
  { pkg: '@fontsource/geist-mono', family: 'Geist Mono', subset: 'latin', weight: '500', file: 'geist-mono-latin-500-normal.woff2' },
  { local: true, family: 'Tesseract Kai', subset: 'zh-ui', weight: '400', file: 'tesseract-kai-zh.woff2' },
];

export const licenseFile = p => `OFL-${p.id}.txt`;
export const packageDir = (uiRoot, pkg) => join(uiRoot, 'node_modules', ...pkg.split('/'));
export function packageOf(face) {
  const p = FONT_PACKAGES.find(x => (face.local ? x.local && x.family === face.family : x.pkg === face.pkg));
  if (!p) throw new Error(`build/fonts.mjs: no FONT_PACKAGES entry for ${face.file}`);
  return p;
}
// A Fontsource file comes from its package; a local font's file is its own source in ui/fonts/.
export const fontSourcePath = (uiRoot, face) => (face.local ? join(uiRoot, 'fonts', face.file) : join(packageDir(uiRoot, face.pkg), 'files', face.file));
export const licenseSourcePath = (uiRoot, p) => (p.local ? join(uiRoot, 'fonts', licenseFile(p)) : join(packageDir(uiRoot, p.pkg), 'LICENSE'));

// Every file that belongs in ui/fonts/: [{name, source}], plus {local, family, tool} for a
// local font's files (source = the file itself).
export function fontCopies(uiRoot) {
  const entry = (name, source, p) => (p.local ? { name, source, local: true, family: p.family, tool: p.tool } : { name, source });
  return [
    ...FONT_FACES.map(f => entry(f.file, fontSourcePath(uiRoot, f), packageOf(f))),
    ...FONT_PACKAGES.map(p => entry(licenseFile(p), licenseSourcePath(uiRoot, p), p)),
  ];
}

const sleepMs = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

// true when `path` exists and holds exactly `bytes` (a file being replaced or locked by
// another process reads as "not yet").
function holds(path, bytes) {
  try { return bytes.equals(readFileSync(path)); } catch { return false; }
}

// Copy node_modules → ui/fonts/, writing a file only when its bytes differ. Each writer
// uses its own temp file plus rename, so nobody reads a half-written font. On Windows the
// rename fails (EPERM/EBUSY/EACCES) while another process has the target open (a
// concurrent build comparing or bundling it, or replacing it with the same bytes); then
// the copy is accepted if the target already holds the right bytes, otherwise the rename
// is retried for up to ~3 s. The temp file is always removed. A local font's files are
// only read: there is nothing to copy them from, so a missing one is an error.
export function syncFonts(uiRoot) {
  const dir = join(uiRoot, 'fonts');
  mkdirSync(dir, { recursive: true });
  const out = [];
  for (const { name, source, local, family, tool } of fontCopies(uiRoot)) {
    if (local) {
      if (!existsSync(source)) {
        throw new Error(`build/fonts.mjs: ${source} is missing; it is a source file of the local font "${family}" (written by ${tool}), not a copy from node_modules`);
      }
      out.push({ name, bytes: readFileSync(source) });
      continue;
    }
    if (!existsSync(source)) {
      throw new Error(`build/fonts.mjs: ${source} is missing; run \`npm --prefix ui ci --ignore-scripts\` (the font packages are pinned devDependencies)`);
    }
    const bytes = readFileSync(source);
    const dest = join(dir, name);
    if (!holds(dest, bytes)) {
      const tmp = `${dest}.${process.pid}-${threadId}.tmp`;
      try {
        writeFileSync(tmp, bytes);
        for (let attempt = 0; ; attempt++) {
          try { renameSync(tmp, dest); break; } catch (e) {
            if (holds(dest, bytes)) break;
            if (attempt >= 40 || !['EPERM', 'EBUSY', 'EACCES'].includes(e && e.code)) throw e;
            sleepMs(Math.min(10 + 5 * attempt, 80));
          }
        }
      } finally {
        rmSync(tmp, { force: true });
      }
    }
    out.push({ name, bytes });
  }
  return out;
}

const hash8 = buf => createHash('sha256').update(buf).digest('hex').slice(0, 8).toUpperCase();

export default async function prepare(ctx) {
  const uiRoot = ctx.uiRoot;
  if (!existsSync(join(uiRoot, 'css', 'fonts.css'))) return { plugins: [], files: [], loader: {} };
  const copied = syncFonts(uiRoot);
  const files = copied
    .filter(c => c.name.endsWith('.txt'))
    .map(c => ({ path: `assets/${c.name.replace(/\.txt$/, '')}.${hash8(c.bytes)}.txt`, contents: new Uint8Array(c.bytes) }));
  return { plugins: [], files, loader: { '.woff2': 'file' } };
}
