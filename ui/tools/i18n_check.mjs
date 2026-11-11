// i18n gate (T-P4-03; P4 architecture §3.9, critique B6, B7, C7, C8, F2, F3, F5, A4).
//
//   node ui/tools/i18n_check.mjs [--level p4|p7] [--report <out.json>] [--root <ui dir>] [--repo <dir>]
//        [--backend-keys <json>] [--py-catalog <json>] [--native-text <native_text.rs>]
//        [--truncation <json>] [--source-digest <hex>] [--quiet]
//
//   --source-digest: the digest a truncation summary must have rendered (default: build.mjs
//   sourceDigest of --root, i.e. the current source)
//
// Every check's level (hard | report) for p4 and p7 comes from <root>/tools/i18n_gates.json,
// never from this script; a check missing there is hard. A hard check with findings, or a
// hard check that could not run, fails the gate (exit 1). Report checks only print counts.
//
// Checks:
//   json            catalog files parse; zh_CN entries are {m, _desc}; other languages are flat
//                   {id: text}; _meta.json is {id: {src_hash, …}}; no id defined twice per language
//   id_format       ids are dotted lower-case segments and start with their file's namespace
//   icu             ICU subset syntax and tag whitelist, every language
//   args            per id, every language has zh_CN's argument names, argument kinds
//                   (value / select / datetime), tag set and select branches
//   plural          every plural covers the language's CLDR categories (Intl.PluralRules)
//   desc            every zh_CN entry has a non-empty _desc
//   frozen          src/i18n/frozen.json {id: src_hash} matches zh_CN (§3.1 freeze)
//   backend_keys    every backend message_key (tools/backend_keys.py) is in zh_CN; no dynamic
//                   key site left unresolved
//   catalog         every catalog.* label id / choice / notice (bridge_catalog) is in zh_CN
//   reason.<ns>     every reason_code of a vocabulary has <ns>.<code> in zh_CN
//                   (common.skipped, tiers.reason, alerts.test_reason, overview.exit_reason,
//                   alerts.onboarding_reason, shell.host_reason)
//   orphans         ids other languages have but zh_CN does not
//   missing         missing or empty translations, and _meta src_hash ≠ hash(zh_CN m)
//   glossary        glossary.json format; dnt terms verbatim; tr[lang] used where zh has the term
//                   (exceptions: tools/i18n_allow.json, each with a reason)
//   truncation      T-P4-10 render matrix `--langs all` truncation summary (allow list as above);
//                   found by itself, only a complete one (coverage.complete=true) of the current
//                   source (coverage.source_digest = build.mjs sourceDigest) counts
//   three_catalogs  Python ui_i18n (py_catalog_dump.py) and shell native_text.rs: same keys and
//                   placeholders in all nine languages
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync, mkdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { importSource, RESERVED_IDS } from '../build/locales.mjs';
import { sourceDigest } from '../build.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_ROOT = resolve(HERE, '..');
const DEFAULT_REPO = resolve(DEFAULT_ROOT, '..');
const icu = await importSource(join(DEFAULT_ROOT, 'src', 'i18n', 'icu.js'));

export const SOURCE = 'zh_CN';
export const LANGS = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];
const BCP47 = { zh_CN: 'zh-CN', en_US: 'en-US', de_DE: 'de-DE', es_ES: 'es-ES', fr_FR: 'fr-FR', ja_JP: 'ja-JP', ko_KR: 'ko-KR', pt_BR: 'pt-BR', ru_RU: 'ru-RU' };
export const REASON_NAMESPACES = ['common.skipped', 'tiers.reason', 'alerts.test_reason', 'overview.exit_reason', 'alerts.onboarding_reason', 'shell.host_reason'];
export const CHECKS = ['json', 'id_format', 'icu', 'args', 'plural', 'desc', 'frozen', 'backend_keys', 'catalog',
  ...REASON_NAMESPACES.map(ns => `reason.${ns}`), 'orphans', 'missing', 'glossary', 'truncation', 'three_catalogs'];
const ID_RE_PLAIN = /^[a-z0-9_]+(\.[a-z0-9_-]+)+$/;
// catalog choice ids end in the backend's value verbatim (catalog.<proto>.<field>.choice.<v>,
// T-P4-04e), e.g. AlertType CRITICAL or 2022-blake3-aes-128-gcm
export const ID_RE = { test: id => { const at = id.indexOf('.choice.'); return at < 0 ? ID_RE_PLAIN.test(id) : ID_RE_PLAIN.test(id.slice(0, at)) && /^[A-Za-z0-9_-]+$/.test(id.slice(at + 8)); } };
const posix = p => p.split(sep).join('/');
const stripBom = s => (s.charCodeAt(0) === 0xfeff ? s.slice(1) : s);
const readJson = p => JSON.parse(stripBom(readFileSync(p, 'utf8')));

/** Hash of the zh_CN source text a translation (or a freeze) was made from. */
export const srcHash = text => createHash('sha256').update(String(text), 'utf8').digest('hex').slice(0, 16);

export function pluralCategories(lang) {
  return new Intl.PluralRules(BCP47[lang] || lang.replace('_', '-')).resolvedOptions().pluralCategories;
}

function python(repo) {
  if (process.env.TESSERACT_UI_PYTHON) return process.env.TESSERACT_UI_PYTHON;
  const venv = join(repo, '.venv', 'Scripts', 'python.exe');
  return existsSync(venv) ? venv : join(repo, '.venv', 'bin', 'python');
}
function runPython(repo, script) {
  const r = spawnSync(python(repo), ['-B', script, '--repo', repo], { cwd: repo, encoding: 'utf8', maxBuffer: 64 << 20, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' } });
  if (r.status !== 0) throw new Error(`${posix(relative(repo, script))} exited ${r.status ?? r.error?.code}: ${(r.stderr || '').trim().slice(-400)}`);
  return JSON.parse(r.stdout);
}

// ---- catalog loading ---------------------------------------------------------------
export function loadCatalogs(root) {
  const dir = join(root, 'src', 'i18n', 'locales');
  const langs = {}, meta = {}, problems = [];
  if (!existsSync(dir)) return { langs, meta, problems: [{ file: 'src/i18n/locales', msg: 'missing directory' }] };
  for (const lang of readdirSync(dir).sort()) {
    const ldir = join(dir, lang);
    if (!statSync(ldir).isDirectory()) continue;
    if (!LANGS.includes(lang)) problems.push({ file: `src/i18n/locales/${lang}`, msg: 'unknown language directory' });
    const entries = Object.create(null); // ids are data: "__proto__" must stay an own key
    for (const name of readdirSync(ldir).filter(n => n.endsWith('.json')).sort()) {
      const file = `src/i18n/locales/${lang}/${name}`;
      let data;
      try { data = readJson(join(ldir, name)); } catch (e) { problems.push({ file, msg: `invalid JSON: ${e.message}` }); continue; }
      if (!data || typeof data !== 'object' || Array.isArray(data)) { problems.push({ file, msg: 'top level must be an object' }); continue; }
      if (name === '_meta.json') {
        if (lang === SOURCE) { problems.push({ file, msg: 'zh_CN has no _meta.json' }); continue; }
        for (const [id, v] of Object.entries(data)) {
          if (!v || typeof v !== 'object' || typeof v.src_hash !== 'string') problems.push({ file, id, msg: '_meta entry must be {src_hash, reused_from?, needs_review?}' });
        }
        meta[lang] = data;
        continue;
      }
      const ns = name.replace(/\.json$/, '');
      for (const [id, v] of Object.entries(data)) {
        if (RESERVED_IDS.includes(id)) { problems.push({ file, id, msg: 'reserved message id (__proto__ / constructor / prototype)' }); continue; }
        if (entries[id]) { problems.push({ file, id, msg: `defined twice (also in ${entries[id].file})` }); continue; }
        if (lang === SOURCE) {
          if (!v || typeof v !== 'object' || Array.isArray(v) || typeof v.m !== 'string') { problems.push({ file, id, msg: 'zh_CN entry must be {"m": string, "_desc": string}' }); continue; }
          const extra = Object.keys(v).filter(k => k !== 'm' && k !== '_desc');
          if (extra.length) problems.push({ file, id, msg: `unexpected keys ${extra.join(', ')} (no _max: critique F8)` });
          entries[id] = { text: v.m, desc: v._desc, file, ns };
        } else {
          if (typeof v !== 'string') { problems.push({ file, id, msg: 'translation must be a string' }); continue; }
          entries[id] = { text: v, file, ns };
        }
      }
    }
    langs[lang] = entries;
  }
  if (!langs[SOURCE]) problems.push({ file: 'src/i18n/locales/zh_CN', msg: 'source language missing' });
  return { langs, meta, problems };
}

const compileSafe = (text, where) => { try { return { msg: icu.compile(text, where) }; } catch (e) { return { error: e.message }; } };
const kind = types => (types.has('select') ? 'select' : types.has('datetime') ? 'datetime' : 'value');
function shape(msg) {
  const a = icu.analyze(msg);
  return {
    args: Object.fromEntries(Object.entries(a.args).map(([k, v]) => [k, kind(v)])),
    tags: [...a.tags].sort(),
    selects: Object.fromEntries(Object.entries(a.selects).map(([k, v]) => [k, [...v].sort()])),
    plurals: a.plurals,
  };
}

// ---- Rust native_text.rs -----------------------------------------------------------
function rustString(src, i) {
  // src[i] === '"'; returns [value, index after closing quote]
  let out = '';
  for (i++; i < src.length; i++) {
    const c = src[i];
    if (c === '"') return [out, i + 1];
    if (c !== '\\') { out += c; continue; }
    const n = src[++i];
    if (n === 'n') out += '\n'; else if (n === 't') out += '\t'; else if (n === 'r') out += '\r';
    else if (n === '0') out += '\0';
    else if (n === 'u') { const m = /^\{([0-9a-fA-F]{1,6})\}/.exec(src.slice(i + 1)); out += String.fromCodePoint(parseInt(m[1], 16)); i += m[0].length; }
    else if (n === '\n') { while (/\s/.test(src[i + 1])) i++; }
    else out += n;
  }
  throw new Error('unterminated Rust string');
}
export function parseNativeText(source) {
  const messages = [];
  const re = /Message\s*\{\s*id\s*:\s*"/g;
  let m;
  while ((m = re.exec(source))) {
    const [id, after] = rustString(source, m.index + m[0].length - 1);
    let i = source.indexOf('[', after);
    const texts = [];
    for (;;) {
      while (/[\s,]/.test(source[i + 1] ?? '')) i++;
      if (source[i + 1] === ']') break;
      const q = source.indexOf('"', i);
      const [v, next] = rustString(source, q);
      texts.push(v);
      i = next - 1;
    }
    messages.push({ id, texts });
  }
  const locales = (/LOCALES\s*:\s*\[&str;\s*9\]\s*=\s*\[([^\]]*)\]/.exec(source) || [])[1];
  return { messages, locales: locales ? [...locales.matchAll(/"([A-Za-z_]+)"/g)].map(x => x[1]) : [] };
}
const bracePlaceholders = s => [...String(s).matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map(x => x[1]).sort();

// ---- the checks --------------------------------------------------------------------
export function runChecks(opts = {}) {
  const root = resolve(opts.root || DEFAULT_ROOT);
  const repo = resolve(opts.repo || DEFAULT_REPO);
  const level = opts.level || 'p4';
  const gatesPath = join(root, 'tools', 'i18n_gates.json');
  const gates = existsSync(gatesPath) ? readJson(gatesPath) : {};
  const results = [];
  const levelOf = id => { const g = gates[id]; const v = g && g[level]; return v === 'report' ? 'report' : 'hard'; };
  const add = (id, items, extra = {}) => { results.push({ id, level: levelOf(id), ok: items.length === 0 && !extra.error, count: items.length, items, ...extra }); };

  const { langs, meta, problems } = loadCatalogs(root);
  const zh = langs[SOURCE] || {};
  add('json', problems);

  // id_format
  const idItems = [];
  for (const [lang, entries] of Object.entries(langs)) {
    for (const [id, e] of Object.entries(entries)) {
      if (!ID_RE.test(id)) idItems.push({ lang, id, file: e.file, msg: 'id must be dotted lower-case segments' });
      else if (id.split('.')[0] !== e.ns) idItems.push({ lang, id, file: e.file, msg: `id does not start with its namespace "${e.ns}."` });
    }
  }
  add('id_format', idItems);

  // icu (every language) — compiled messages kept for the next checks
  const compiled = {};
  const icuItems = [];
  for (const [lang, entries] of Object.entries(langs)) {
    compiled[lang] = {};
    for (const [id, e] of Object.entries(entries)) {
      if (typeof e.text !== 'string' || (lang !== SOURCE && !e.text.trim())) continue;
      const r = compileSafe(e.text, `${e.file}:${id}`);
      if (r.error) icuItems.push({ lang, id, msg: r.error }); else compiled[lang][id] = r.msg;
    }
  }
  add('icu', icuItems);

  // args / plural
  const argItems = [], pluralItems = [];
  const zhShape = Object.fromEntries(Object.entries(compiled[SOURCE] || {}).map(([id, m]) => [id, shape(m)]));
  for (const [lang, msgs] of Object.entries(compiled)) {
    let cats;
    try { cats = pluralCategories(lang); } catch { cats = ['other']; }
    for (const [id, msg] of Object.entries(msgs)) {
      const s = lang === SOURCE ? zhShape[id] : shape(msg);
      for (const p of s.plurals) {
        const keys = p.keys.filter(k => !k.startsWith('='));
        const lack = cats.filter(c => !keys.includes(c));
        if (lack.length) pluralItems.push({ lang, id, arg: p.name, msg: `plural lacks ${lack.join(', ')} (CLDR ${lang}: ${cats.join(', ')})` });
      }
      if (lang === SOURCE || !zhShape[id]) continue;
      const z = zhShape[id];
      const diff = [];
      const za = Object.keys(z.args).sort().join(','), la = Object.keys(s.args).sort().join(',');
      if (za !== la) diff.push(`arguments {${la}} ≠ zh_CN {${za}}`);
      for (const k of Object.keys(z.args)) if (k in s.args && s.args[k] !== z.args[k]) diff.push(`{${k}} is ${s.args[k]}, zh_CN ${z.args[k]}`);
      if (z.tags.join(',') !== s.tags.join(',')) diff.push(`tags <${s.tags.join('> <')}> ≠ zh_CN <${z.tags.join('> <')}>`);
      for (const [k, v] of Object.entries(z.selects)) if (s.selects[k] && s.selects[k].join('|') !== v.join('|')) diff.push(`select {${k}} branches ${s.selects[k].join('|')} ≠ zh_CN ${v.join('|')}`);
      if (diff.length) argItems.push({ lang, id, msg: diff.join('; ') });
    }
  }
  add('args', argItems);
  add('plural', pluralItems);

  // desc
  add('desc', Object.entries(zh).filter(([, e]) => typeof e.desc !== 'string' || !e.desc.trim()).map(([id, e]) => ({ id, file: e.file, msg: 'missing _desc' })));

  // frozen
  const frozenItems = [];
  const frozenPath = join(root, 'src', 'i18n', 'frozen.json');
  if (!existsSync(frozenPath)) frozenItems.push({ msg: 'src/i18n/frozen.json missing' });
  else {
    let frozen = {};
    try { frozen = readJson(frozenPath); } catch (e) { frozenItems.push({ msg: `frozen.json: ${e.message}` }); }
    for (const [id, h] of Object.entries(frozen)) {
      if (!zh[id]) frozenItems.push({ id, msg: 'frozen id was deleted from zh_CN (update frozen.json and list it in the report)' });
      else if (h !== srcHash(zh[id].text)) frozenItems.push({ id, msg: `frozen text changed: frozen.json ${h}, zh_CN ${srcHash(zh[id].text)}` });
    }
  }
  add('frozen', frozenItems);

  // backend keys (python, or a supplied JSON)
  let backend = null, backendError = null;
  try { backend = opts.backendKeys ? readJson(opts.backendKeys) : runPython(repo, join(repo, 'ui', 'tools', 'backend_keys.py')); } catch (e) { backendError = e.message; }
  if (backend) {
    const items = Object.keys(backend.errors || {}).filter(k => !zh[k]).map(k => ({ id: k, msg: 'backend message_key missing in zh_CN', sources: backend.errors[k].slice(0, 3) }));
    for (const u of backend.unresolved || []) items.push({ id: null, msg: `unresolved dynamic key site ${u.site}: ${u.why}` });
    const extra = Object.keys(zh).filter(id => id.startsWith('errors.') && !Object.prototype.hasOwnProperty.call(backend.errors || {}, id));
    // errors.json holds backend message keys only (critique C6): a front-end id there fails too
    for (const id of extra) items.push({ id, msg: 'errors.* id that no backend sends (errors.json is for backend message keys only; use the page namespace)' });
    add('backend_keys', items, { extra_in_errors_json: extra, keys: Object.keys(backend.errors || {}).length, by_domain: backend.errors_by_domain });
    const cat = backend.catalog || {};
    const catItems = [];
    for (const [group, ids] of Object.entries(cat)) for (const id of ids) if (!zh[id]) catItems.push({ id, group, msg: 'catalog id missing in zh_CN' });
    add('catalog', catItems, { totals: Object.fromEntries(Object.entries(cat).map(([g, v]) => [g, v.length])) });
    for (const ns of REASON_NAMESPACES) {
      const codes = (backend.reasons || {})[ns] || [];
      add(`reason.${ns}`, codes.filter(c => !zh[`${ns}.${c}`]).map(c => ({ id: `${ns}.${c}`, msg: 'reason_code has no message' })),
        { total: codes.length, source: (backend.reason_sources || {})[ns] });
    }
  } else {
    for (const id of ['backend_keys', 'catalog', ...REASON_NAMESPACES.map(ns => `reason.${ns}`)]) add(id, [], { error: `backend_keys.py: ${backendError}` });
  }

  // orphans / missing
  const orphanItems = [], missingItems = [];
  for (const lang of LANGS.filter(l => l !== SOURCE)) {
    const entries = langs[lang] || {};
    const m = meta[lang] || {};
    for (const id of Object.keys(entries)) if (!zh[id]) orphanItems.push({ lang, id });
    for (const id of Object.keys(m)) if (!zh[id]) orphanItems.push({ lang, id, msg: '_meta entry without a zh_CN id' });
    for (const [id, e] of Object.entries(zh)) {
      const t = entries[id];
      if (!t) missingItems.push({ lang, id, why: 'missing' });
      else if (!t.text.trim()) missingItems.push({ lang, id, why: 'empty' });
      else if (!m[id] || m[id].src_hash !== srcHash(e.text)) missingItems.push({ lang, id, why: m[id] ? 'stale src_hash' : 'no src_hash' });
    }
  }
  add('orphans', orphanItems);
  const byLang = {};
  for (const it of missingItems) { byLang[it.lang] ||= { missing: 0, empty: 0, stale: 0 }; byLang[it.lang][it.why === 'missing' ? 'missing' : it.why === 'empty' ? 'empty' : 'stale']++; }
  add('missing', missingItems, { by_lang: byLang });

  // glossary
  const allowPath = join(root, 'tools', 'i18n_allow.json');
  let allow = [];
  if (existsSync(allowPath)) {
    try { allow = readJson(allowPath).entries || []; } catch (e) { allow = []; }
  }
  const allowed = (check, it) => allow.some(a => a.check === check && typeof a.reason === 'string' && a.reason.trim()
    && ['id', 'lang', 'term_id', 'selector', 'page'].every(k => a[k] === undefined || a[k] === it[k]));
  const glossItems = [];
  const glossPath = join(root, 'src', 'i18n', 'glossary.json');
  let glossary = [];
  if (!existsSync(glossPath)) glossItems.push({ msg: 'src/i18n/glossary.json missing' });
  else {
    try { glossary = readJson(glossPath); } catch (e) { glossItems.push({ msg: `glossary.json: ${e.message}` }); }
    if (!Array.isArray(glossary)) { glossItems.push({ msg: 'glossary.json must be an array' }); glossary = []; }
  }
  const seen = new Set();
  for (const t of glossary) {
    const ok = t && typeof t.term_id === 'string' && typeof t.zh === 'string' && t.zh && typeof t.dnt === 'boolean'
      && Array.isArray(t.ns) && t.tr && typeof t.tr === 'object' && !Array.isArray(t.tr);
    if (!ok) { glossItems.push({ term_id: t && t.term_id, msg: 'entry must be {term_id, zh, dnt:boolean, ns:[], tr:{}, note?}' }); continue; }
    if (seen.has(t.term_id)) glossItems.push({ term_id: t.term_id, msg: 'duplicate term_id' });
    seen.add(t.term_id);
    for (const [id, e] of Object.entries(zh)) {
      if (!e.text.includes(t.zh)) continue;
      const ns = id.split('.')[0];
      if (!t.ns.includes('*') && !t.ns.includes(ns)) continue;
      for (const lang of LANGS.filter(l => l !== SOURCE)) {
        const tr = langs[lang] && langs[lang][id];
        if (!tr || !tr.text.trim()) continue;
        const want = t.dnt ? t.zh : t.tr[lang];
        if (!want) continue;
        const hit = t.dnt ? tr.text.includes(want) : tr.text.toLowerCase().includes(String(want).toLowerCase());
        const it = { check: 'glossary', id, lang, term_id: t.term_id, msg: t.dnt ? `"${want}" must stay untranslated` : `use "${want}" for ${t.zh}` };
        if (!hit && !allowed('glossary', it)) glossItems.push(it);
      }
    }
  }
  add('glossary', glossItems);

  // truncation (T-P4-10 summary). Found by itself, only a summary whose coverage.complete is true
  // counts (第五波 T-P4-10 #9, T-P4-12): an unfiltered run.mjs --langs all (every case, the 9
  // languages, both sizes). The newest complete one wins; a newer partial run (a --case smoke with
  // --langs all) never stands in for the full matrix. It must also have rendered the current
  // source: coverage.source_digest === sourceDigest(ui) (build.mjs), so a complete matrix of an
  // older build is skipped as stale (T-P4-12 fix 1). An explicit --truncation file is read as
  // given, and the result says whether it was complete and current; where truncation is hard
  // (p7) an incomplete or stale explicit file is the check's error.
  let truncPath = opts.truncation || null, truncSkipped = 0, truncStale = 0;
  let curDigest;
  const digestNow = () => (curDigest === undefined ? (curDigest = opts.sourceDigest !== undefined ? opts.sourceDigest : sourceDigest(root)) : curDigest);
  const truncComplete = doc => !!(doc && !Array.isArray(doc) && doc.coverage && doc.coverage.complete === true);
  const truncCurrent = doc => !!(doc && !Array.isArray(doc) && doc.coverage && typeof doc.coverage.source_digest === 'string' && doc.coverage.source_digest === digestNow());
  if (!truncPath) {
    const base = join(repo, '.build', '新界面迁移_工单', 'evidence', 'render');
    if (existsSync(base)) {
      const cands = readdirSync(base).map(d => join(base, d, 'truncation.json')).filter(p => existsSync(p));
      cands.sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
      for (const p of cands) {
        let d = null;
        try { d = readJson(p); } catch { /* half-written: not complete */ }
        if (!truncComplete(d)) { truncSkipped++; continue; }
        if (!truncCurrent(d)) { truncStale++; continue; }
        truncPath = p; break;
      }
    }
  }
  if (!truncPath || !existsSync(truncPath)) {
    const why = [truncSkipped ? `${truncSkipped} newer partial one(s) skipped` : '', truncStale ? `${truncStale} complete but stale one(s) skipped (coverage.source_digest ≠ current source ${String(digestNow()).slice(0, 12)})` : ''].filter(Boolean).join('; ');
    add('truncation', [], { error: `no truncation summary (T-P4-10 \`run.mjs --langs all\` without --case / --page has not produced a truncation.json with coverage.complete=true for the current source${why ? `; ${why}` : ''})`, skipped_partial: truncSkipped, skipped_stale: truncStale });
  }
  else {
    // a half-written or broken summary (T-P4-10 still running) is this check's failure, not a crash
    let doc = null, truncError = null;
    try {
      doc = readJson(truncPath);
      if (!doc || typeof doc !== 'object' || (!Array.isArray(doc) && doc.items !== undefined && !Array.isArray(doc.items))) throw new Error('expected [] or {langs, items: []}');
    } catch (e) { truncError = `${posix(relative(repo, truncPath))}: ${e.message}`; }
    if (truncError) add('truncation', [], { error: truncError, source: posix(relative(repo, truncPath)) });
    else {
      const items = (Array.isArray(doc) ? doc : doc.items || []).filter(x => x && typeof x === 'object').map(x => ({ check: 'truncation', ...x })).filter(x => !allowed('truncation', x));
      const complete = truncComplete(doc), digestMatch = truncCurrent(doc);
      const extra = { source: posix(relative(repo, truncPath)), langs: doc.langs, complete, digest_match: digestMatch, skipped_partial: truncSkipped, skipped_stale: truncStale };
      // only an explicit file can get here incomplete or stale; at the hard level it does not pass
      const bad = [complete ? '' : 'coverage.complete is not true (a filtered or legacy run)', digestMatch ? '' : `coverage.source_digest ≠ current source ${String(digestNow()).slice(0, 12)} (stale build)`].filter(Boolean);
      if (bad.length && levelOf('truncation') === 'hard') extra.error = `${extra.source}: ${bad.join('; ')}`;
      add('truncation', items, extra);
    }
  }

  // three catalogs
  const triItems = [];
  const tri = {};
  try {
    const py = opts.pyCatalog ? readJson(opts.pyCatalog) : runPython(repo, join(repo, 'ui', 'tools', 'py_catalog_dump.py'));
    const zhPy = py.languages[py.source] || {};
    tri.python = { source: py.source, keys: Object.keys(zhPy).length, qt_free: py.qt_free, languages: {} };
    for (const lang of LANGS) {
      const cat = py.languages[lang];
      if (!cat) { triItems.push({ catalog: 'python', lang, msg: 'language missing' }); continue; }
      const missing = Object.keys(zhPy).filter(k => !(k in cat));
      const extra = Object.keys(cat).filter(k => !(k in zhPy));
      const ph = Object.keys(zhPy).filter(k => k in cat && cat[k].join(',') !== zhPy[k].join(','));
      tri.python.languages[lang] = { keys: Object.keys(cat).length, missing: missing.length, extra: extra.length, placeholder_mismatch: ph.length };
      for (const k of missing) triItems.push({ catalog: 'python', lang, key: k, msg: 'key missing' });
      for (const k of extra) triItems.push({ catalog: 'python', lang, key: k, msg: 'key not in the source catalog' });
      for (const k of ph) triItems.push({ catalog: 'python', lang, key: k, msg: `placeholders {${cat[k].join('},{')}} ≠ source {${zhPy[k].join('},{')}}` });
    }
    // overlay keys outside TRANSLATIONS_EN are the "extra" keys above; summarised, not re-listed
    tri.python.overlay_orphans = Object.fromEntries(Object.entries(py.overlay_orphans || {}).map(([l, v]) => [l, v.length]));
  } catch (e) { triItems.push({ catalog: 'python', msg: `py_catalog_dump.py: ${e.message}` }); }
  try {
    const rsPath = opts.nativeText || join(repo, 'shell', 'app', 'src', 'native_text.rs');
    const rs = parseNativeText(readFileSync(rsPath, 'utf8'));
    tri.native = { messages: rs.messages.length, locales: rs.locales };
    if (rs.locales.join(',') !== LANGS.join(',')) triItems.push({ catalog: 'native', msg: `LOCALES order ${rs.locales.join(',')} ≠ ${LANGS.join(',')}` });
    for (const { id, texts } of rs.messages) {
      if (texts.length !== 9) { triItems.push({ catalog: 'native', id, msg: `${texts.length} texts, expected 9` }); continue; }
      const ref = bracePlaceholders(texts[0]).join(',');
      texts.forEach((t, i) => {
        if (!t.trim()) triItems.push({ catalog: 'native', id, lang: LANGS[i], msg: 'empty text' });
        else if (bracePlaceholders(t).join(',') !== ref) triItems.push({ catalog: 'native', id, lang: LANGS[i], msg: `placeholders {${bracePlaceholders(t).join('},{')}} ≠ zh_CN {${ref.split(',').filter(Boolean).join('},{')}}` });
      });
    }
  } catch (e) { triItems.push({ catalog: 'native', msg: `native_text.rs: ${e.message}` }); }
  add('three_catalogs', triItems, tri);

  // counts
  const nsCount = {};
  for (const e of Object.values(zh)) nsCount[e.ns] = (nsCount[e.ns] || 0) + 1;
  const errorsByDomain = {};
  for (const id of Object.keys(zh).filter(i => i.startsWith('errors.'))) {
    const p = id.split('.');
    const d = p.length > 2 ? p[1] : '(code)';
    errorsByDomain[d] = (errorsByDomain[d] || 0) + 1;
  }
  const unknownGates = Object.keys(gates).filter(k => !k.startsWith('_') && !CHECKS.includes(k));
  const failed = results.filter(r => r.level === 'hard' && !r.ok).map(r => r.id);
  return { level, root: posix(root), checks: results, failed, unknown_gates: unknownGates, counts: { zh_CN: nsCount, errors_by_domain: errorsByDomain } };
}

function parseArgs(argv) {
  const o = { level: 'p4' };
  const take = (i, k) => { if (argv[i + 1] === undefined) throw new Error(`${argv[i]} needs a value`); o[k] = argv[i + 1]; };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--level') { take(i, 'level'); i++; if (!['p4', 'p7'].includes(o.level)) throw new Error('--level p4|p7'); }
    else if (a === '--report') { take(i, 'report'); i++; }
    else if (a === '--root') { take(i, 'root'); i++; }
    else if (a === '--repo') { take(i, 'repo'); i++; }
    else if (a === '--backend-keys') { take(i, 'backendKeys'); i++; }
    else if (a === '--py-catalog') { take(i, 'pyCatalog'); i++; }
    else if (a === '--native-text') { take(i, 'nativeText'); i++; }
    else if (a === '--truncation') { take(i, 'truncation'); i++; }
    else if (a === '--source-digest') { take(i, 'sourceDigest'); i++; }
    else if (a === '--quiet') o.quiet = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return o;
}

export function main(argv = process.argv.slice(2)) {
  let o;
  try { o = parseArgs(argv); } catch (e) { console.error(`i18n_check: ${e.message}`); return 2; }
  const res = runChecks(o);
  if (!o.quiet) {
    for (const r of res.checks) {
      const state = r.error ? `could not run: ${r.error}` : `${r.count}`;
      console.log(`[i18n] ${r.id.padEnd(32)} ${r.level.padEnd(6)} ${r.ok ? 'ok    ' : r.level === 'hard' ? 'FAIL  ' : 'report'} ${state}`);
      if (r.level === 'hard' && !r.ok) for (const it of r.items.slice(0, 20)) console.log(`         ${JSON.stringify(it)}`);
    }
    if (res.unknown_gates.length) console.log(`[i18n] i18n_gates.json lists unknown checks: ${res.unknown_gates.join(', ')}`);
    console.log(`[i18n] level ${res.level}: ${res.failed.length ? `FAILED (${res.failed.join(', ')})` : 'ok'}`);
  }
  if (o.report) {
    mkdirSync(dirname(resolve(o.report)), { recursive: true });
    writeFileSync(resolve(o.report), JSON.stringify(res, null, 1) + '\n');
  }
  return res.failed.length ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = main();
}
