// Shared helpers for ui/test/unit/i18n_*.test.mjs (T-P4-03).
//
// The i18n modules are browser ES modules in a package without "type": "module"; importing
// them by path makes Node warn on every run. Tests load them through esbuild (the same
// bundler as the product) into one in-memory ES module, so every test sees one instance of
// t.js + intl.js + icu.js, exactly like the page.
import * as esbuild from 'esbuild';
import { cpSync, existsSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const HERE = dirname(fileURLToPath(import.meta.url));
export const UI = resolve(HERE, '..', '..', '..');
export const REPO = resolve(UI, '..');
export const I18N_SRC = join(UI, 'src', 'i18n');
export const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';

/** Bundle the i18n modules (or `contents`) to an ES module and import it. */
export async function loadI18n({ define = {}, contents } = {}) {
  const entry = contents || [
    "export * from './t.js';",
    "export { fmt, bindRuntime, clearCache, icuEnv, bcp47 } from './intl.js';",
    "export * as icu from './icu.js';",
    "export * as pseudo from './pseudo.js';",
    "export * as relabel from './relabel.js';",
  ].join('\n');
  const r = await esbuild.build({
    stdin: { contents: entry, resolveDir: I18N_SRC, sourcefile: 'i18n-test-entry.js' },
    bundle: true, format: 'esm', platform: 'neutral', write: false, logLevel: 'silent',
    define: { __I18N_DEV__: 'false', __I18N_TRACE__: 'false', __LOCALES__: '{}', ...define },
  });
  return import('data:text/javascript;base64,' + Buffer.from(r.outputFiles[0].contents).toString('base64'));
}

/** Bundle an IIFE for the browser (e.g. core.js + the i18n runtime). */
export async function bundleIife(contents, define = {}) {
  const r = await esbuild.build({
    stdin: { contents, resolveDir: join(UI, 'src', 'boot'), sourcefile: 'dom-test-entry.js' },
    bundle: true, format: 'iife', target: 'chrome120', write: false, logLevel: 'silent', charset: 'utf8',
    define: { __I18N_DEV__: 'true', __I18N_TRACE__: 'false', __LOCALES__: '{}', __DEMO__: 'false', __TARGET__: '"app"', __H_FEATURES__: '[]', ...define },
  });
  return r.outputFiles[0].text;
}

/** Materialise a gate fixture: base + overlay (case.json {extends, expect, patch_meta?}). */
export function materialize(name) {
  const fx = join(HERE);
  const dir = mkdtempSync(join(tmpdir(), `tesseract-i18n-${name}-`));
  const caseDir = join(fx, name);
  const spec = existsSync(join(caseDir, 'case.json')) ? JSON.parse(readFileSync(join(caseDir, 'case.json'), 'utf8')) : { extends: null, expect: null };
  cpSync(join(fx, spec.extends || name), dir, { recursive: true });
  if (spec.extends) {
    const copy = (src, rel = '') => {
      for (const n of readdirSync(join(src, rel), { withFileTypes: true })) {
        const r = rel ? `${rel}/${n.name}` : n.name;
        if (r === 'case.json') continue;
        if (n.isDirectory()) copy(src, r); else cpSync(join(src, r), join(dir, r));
      }
    };
    copy(caseDir);
  }
  if (spec.patch_meta) {
    const p = join(dir, 'src', 'i18n', 'locales', spec.patch_meta.lang, '_meta.json');
    const meta = JSON.parse(readFileSync(p, 'utf8'));
    meta[spec.patch_meta.id] = { src_hash: spec.patch_meta.src_hash };
    writeFileSync(p, JSON.stringify(meta, null, 2) + '\n');
  }
  const ext = f => join(dir, 'external', f);
  return {
    dir, expect: spec.expect,
    opts: { root: dir, backendKeys: ext('backend_keys.json'), pyCatalog: ext('py_catalog.json'), nativeText: ext('native_text.rs'), truncation: ext('truncation.json'),
      // the fixture's truncation summaries carry this digest in coverage.source_digest (T-P4-12 fix 1)
      sourceDigest: 'fixture-digest' },
  };
}

export const chromeAvailable = () => existsSync(CHROME);
