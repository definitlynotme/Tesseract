// Build plugin hook: language catalogs (owner: T-P4-03; P4 architecture §1.2, §3.1, §3.6, §3.8).
// Interface: see src/boot/README.md ("构建插件接口").
//
// Reads src/i18n/locales/<lang>/<namespace>.json, compiles every message with the ICU subset
// (src/i18n/icu.js; a syntax error fails the build with `file:id`), and emits one classic
// script per language:
//
//   locale-<lang>.<HASH>.js   (function(w){…register("<lang>", {id: compiled})…})(window)
//
// Non-ASCII is \u-escaped, so the file is encoding-proof (file://, WebView2). Missing or empty
// translations and ids the source catalog no longer has are left out (the runtime falls back
// en_US → zh_CN). The source language (zh_CN) is the terminal fallback of every language, so
// index.html preloads it with a static <script src> before the main bundle; every other
// language is loaded on demand. Defines: __LOCALES__ (a JSON *string* of {lang: file}: a string define is
// inlined, an object define would leave a // <define:…> comment in unminified bundles), __I18N_DEV__ (design
// target or --dev), __I18N_TRACE__ (--i18n-trace). --pseudo adds locale-pseudo (design, or app
// with --dev; src/i18n/pseudo.js).
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// src/i18n/*.js are browser ES modules in a package without "type": "module" (package.json
// belongs to T-P4-01). Importing them by path makes Node print a MODULE_TYPELESS_PACKAGE_JSON
// warning on every build; loading the (import-free) source as a data: URL module does not.
const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'i18n');
export const importSource = file => import('data:text/javascript;base64,' + Buffer.from(readFileSync(file)).toString('base64'));
const { compile, parse, analyze, BUILTIN_TAGS, SLOT_TAGS } = await importSource(join(SRC, 'icu.js'));
const { pseudoCatalog, PSEUDO } = await importSource(join(SRC, 'pseudo.js'));
export { compile, parse, analyze, BUILTIN_TAGS, SLOT_TAGS, pseudoCatalog, PSEUDO };

export const SOURCE = 'zh_CN';
export const LANGS = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
function hash8(text) {
  const bytes = createHash('sha256').update(text).digest();
  let bits = 0, value = 0, out = '';
  for (const b of bytes) {
    value = ((value & 0xff) << 8) | b; bits += 8; // fewer than 5 bits are left over each round
    while (bits >= 5 && out.length < 8) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; }
    if (out.length >= 8) break;
  }
  return out;
}
const asciiJson = obj => JSON.stringify(obj).replace(/[^ -~]/g, c => '\\' + 'u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
const stripBom = s => (s.charCodeAt(0) === 0xfeff ? s.slice(1) : s);
const readJson = p => JSON.parse(stripBom(readFileSync(p, 'utf8')));

/** Raw catalogs: {lang: {id: {text, file}}} — zh_CN text is entry.m; others are strings. */
export function readCatalogs(uiRoot) {
  const dir = join(uiRoot, 'src', 'i18n', 'locales');
  const out = {};
  if (!existsSync(dir)) return out;
  for (const lang of readdirSync(dir).sort()) {
    const ldir = join(dir, lang);
    if (!statSync(ldir).isDirectory()) continue;
    const entries = Object.create(null); // ids are data: "__proto__" must stay an own key
    for (const name of readdirSync(ldir).filter(n => n.endsWith('.json') && !n.startsWith('_')).sort()) {
      const file = `src/i18n/locales/${lang}/${name}`;
      const data = readJson(join(ldir, name));
      for (const [id, v] of Object.entries(data)) {
        const text = lang === SOURCE ? (v && typeof v === 'object' ? v.m : undefined) : v;
        entries[id] = { text, file };
      }
    }
    out[lang] = entries;
  }
  return out;
}

// Ids that would act on the prototype chain of an object literal or a plain object (the
// language script embeds each catalog as a literal: {"__proto__": …} would set its prototype).
export const RESERVED_IDS = Object.freeze(['__proto__', 'constructor', 'prototype']);

/** Compile all catalogs: {lang: {id: compiled}}. Throws on the first ICU error (file:id). */
export function compileCatalogs(raw) {
  const out = {};
  const source = raw[SOURCE] || {};
  for (const [lang, entries] of Object.entries(raw)) {
    const cat = Object.create(null);
    for (const id of Object.keys(entries).sort()) {
      const { text, file } = entries[id];
      if (RESERVED_IDS.includes(id)) throw new Error(`${file}:${id}: reserved message id`);
      if (lang !== SOURCE && (!Object.prototype.hasOwnProperty.call(source, id) || typeof text !== 'string' || !text.trim())) continue;
      if (typeof text !== 'string') throw new Error(`${file}:${id}: message must be a string`);
      cat[id] = compile(text, `${file}:${id}`);
    }
    out[lang] = cat;
  }
  return out;
}

export function localeScript(lang, catalog) {
  return `(function(w){var T=w.T=w.T||{},a=[${JSON.stringify(lang)},${asciiJson(catalog)}];` +
    `if(T.i18n&&T.i18n.register)T.i18n.register(a[0],a[1]);else(T.__i18nq=T.__i18nq||[]).push(a);})(window);\n`;
}

export default async function prepare(ctx) {
  const { target, uiRoot, pseudo = false, i18nTrace = false, dev = false } = ctx;
  const devBuild = target === 'design' || dev;
  const define = {
    __LOCALES__: JSON.stringify('{}'),
    __I18N_DEV__: devBuild ? 'true' : 'false',
    __I18N_TRACE__: i18nTrace ? 'true' : 'false',
  };
  const raw = readCatalogs(uiRoot);
  if (!raw[SOURCE]) return { define, plugins: [], files: [], scripts: [], locales: {} };
  const compiled = compileCatalogs(raw);
  if (pseudo && devBuild) compiled.pseudo = pseudoCatalog(compiled[SOURCE], compiled.en_US || {});
  const files = [], locales = {};
  for (const lang of Object.keys(compiled).sort()) {
    const body = localeScript(lang, compiled[lang]);
    const path = `locale-${lang}.${hash8(body)}.js`;
    files.push({ path, contents: body });
    locales[lang] = path;
  }
  define.__LOCALES__ = JSON.stringify(JSON.stringify(locales));
  return { define, plugins: [], files, scripts: [locales[SOURCE]], locales };
}
