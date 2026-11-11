// i18n runtime: T.t / T.tx / T.ti / T.i18n (T-P4-03; P4 architecture §3.1–§3.8).
//
//   T.t(id, params?) → string            for title / aria-label / placeholder and plain text;
//                                         tags are dropped, their words kept
//   T.tx(id, params?, slots?) → DocumentFragment
//                                         tags become elements (rich.js); params are text nodes
//   T.ti(el, id, params?, {attr}?) → el   writes the text (or one attribute) and registers it
//                                         (data-i18n / data-i18n-attr) for in-place relabel
//   T.i18n.lang                           the applied language (zh_CN … ru_RU, pseudo)
//   T.i18n.set(lang) → Promise<lang>      load → <html lang> → clear Intl caches → store `lang`
//                                         → relabel(document) → one 'i18n:change'. Same language
//                                         (already loaded): resolves at once, nothing reloads. A load
//                                         failure keeps the current language and rejects (code
//                                         load_failed / not_registered / unsupported_language). The
//                                         latest call wins: an earlier call still loading rejects with
//                                         code 'superseded', so a resolved value is always the
//                                         language that call asked for and that was applied.
//   T.i18n.boot() → Promise<lang>         before hello: only *preload* the navigator.language guess
//                                         (and the en_US / zh_CN fallbacks); the page stays on its
//                                         current language until hello.lang / host:language
//                                         (architecture §3.5: the page never switches optimistically).
//                                         The store's `lang` is followed: any write by someone else
//                                         (the rt client's hello / host:language) calls set(lang).
//   T.i18n.register(lang, compiled)       called by the language files
//   T.i18n.onChange(fn) → off             fn({lang, prev}) after each applied change
//   T.i18n.has(id, lang?) · stats() · guess(list) · relabel(root) · relocalizePage(def, rerender)
//   T.i18n.hasAny(id) → bool              "does T.t(id) find a message?": the same chain as T.t
//                                         (current → en_US → zh_CN). has() looks at one language
//                                         only; use hasAny to choose between ids (T-P4-H4, P5 #34).
//
// Missing ids fall back to en_US, then zh_CN; a development build counts each fallback and
// warns once per id. --i18n-trace builds mark nodes from T.tx / T.ti with data-i18n-id and keep
// a text → id index for T.t strings (T.i18n.traced()).
import { formatParts, partsToText, hasTags } from './icu.js';
import { renderParts } from './rich.js';
import { bindText, bindAttr, relabel as relabelTree, relocalizePage } from './relabel.js';
import { createLoader } from './loader.js';
import { bindRuntime, bcp47, clearCache, icuEnv } from './intl.js';

export const SOURCE_LANG = 'zh_CN';
export const FALLBACK_LANG = 'en_US';
export const LANGS = Object.freeze(['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU']);

// Primary-language mapping for the pre-hello guess. Same table as the shell's
// native_text.rs language() (LANGID primary language; everything else → en_US), so the
// page's first frame and the native dialogs agree (architecture §3.5).
const PRIMARY = { zh: 'zh_CN', de: 'de_DE', es: 'es_ES', fr: 'fr_FR', ja: 'ja_JP', ko: 'ko_KR', pt: 'pt_BR', ru: 'ru_RU', en: 'en_US' };
export function guessLanguage(list) {
  for (const raw of Array.from(list || [])) {
    const tag = String(raw || '').trim();
    if (!tag) continue;
    const primary = tag.split(/[-_]/)[0].toLowerCase();
    if (PRIMARY[primary]) return PRIMARY[primary];
    return FALLBACK_LANG;
  }
  return FALLBACK_LANG;
}

/**
 * Build a runtime. win/doc may be omitted (Node tests: T.t only). store/emit are core.js's
 * T.store and T.emit when installed into a page.
 */
export function createI18n({ win, doc, store, emit, files = {}, dev = false, trace = false } = {}) {
  const catalogs = new Map();
  const listeners = new Set();
  const counts = { fallback: 0, missing: 0, byId: new Map() };
  const warned = new Set();
  const traced = new Map(); // text → Set(id)   (trace builds only)
  let current = SOURCE_LANG;
  let seq = 0;
  let writingStore = false;

  const warn = msg => { if (dev && win && win.console) win.console.warn(`[i18n] ${msg}`); };
  const warnOnce = (key, msg) => { if (dev && !warned.has(key)) { warned.add(key); warn(msg); } };
  const loader = createLoader({ doc, files, isRegistered: l => catalogs.has(l) });

  function register(lang, compiled) {
    if (typeof lang !== 'string' || !compiled || typeof compiled !== 'object') return;
    const prev = catalogs.get(lang) || {};
    catalogs.set(lang, Object.assign(Object.create(null), prev, compiled));
  }
  const has = (id, lang = current) => { const c = catalogs.get(lang); return !!c && Object.prototype.hasOwnProperty.call(c, id); };
  const chainOf = () => [current, FALLBACK_LANG, SOURCE_LANG].filter((l, i, a) => a.indexOf(l) === i);
  /** True when T.t(id) finds a message somewhere on its fallback chain (no counting, no warning). */
  const hasAny = id => chainOf().some(l => has(id, l));

  /** [message, language it came from] following current → en_US → zh_CN, or null. */
  function lookup(id) {
    const chain = chainOf();
    for (let i = 0; i < chain.length; i++) {
      if (has(id, chain[i])) {
        if (i > 0 && dev) { counts.fallback++; counts.byId.set(id, (counts.byId.get(id) || 0) + 1); warnOnce('fb:' + id, `"${id}" missing in ${current}, using ${chain[i]}`); }
        return [catalogs.get(chain[i])[id], chain[i]];
      }
    }
    if (dev) { counts.missing++; counts.byId.set(id, (counts.byId.get(id) || 0) + 1); }
    warnOnce('miss:' + id, `"${id}" is not in any loaded catalog`);
    return null;
  }

  function parts(id, params, msgLang) {
    const found = msgLang ? [catalogs.get(msgLang)[id], msgLang] : lookup(id);
    if (!found) return null;
    const env = { ...icuEnv(found[1]), missing: name => { warnOnce(`arg:${id}:${name}`, `"${id}": parameter {${name}} not given`); return ''; } };
    return { parts: formatParts(found[0], params, env), msg: found[0] };
  }

  function t(id, params) {
    const r = parts(id, params);
    if (!r) return id;
    if (dev && hasTags(r.msg)) warnOnce('tags:' + id, `"${id}" has tag placeholders: T.t drops them, use T.tx for rich text`);
    const s = partsToText(r.parts);
    if (trace) { let set = traced.get(s); if (!set) traced.set(s, set = new Set()); set.add(id); }
    return s;
  }

  function tx(id, params, slots) {
    const r = parts(id, params);
    const frag = r ? renderParts(r.parts, { doc, slots, warn: m => warnOnce(`slot:${id}:${m}`, `"${id}": ${m}`) })
      : (() => { const f = doc.createDocumentFragment(); f.appendChild(doc.createTextNode(id)); return f; })();
    if (!trace) return frag;
    const span = doc.createElement('span');
    span.setAttribute('data-i18n-id', id);
    span.appendChild(frag);
    const out = doc.createDocumentFragment();
    out.appendChild(span);
    return out;
  }

  function writeText(el, id, params) {
    const r = parts(id, params);
    if (r && hasTags(r.msg)) {
      while (el.firstChild) el.removeChild(el.firstChild);
      el.appendChild(renderParts(r.parts, { doc, warn: m => warnOnce(`slot:${id}:${m}`, `"${id}": ${m}`) }));
    } else el.textContent = r ? partsToText(r.parts) : id;
    if (trace) el.setAttribute('data-i18n-id', id);
  }
  // Translated attributes are text-bearing ones only. A data-i18n-attr name comes from the DOM,
  // and T.t returns the id itself when it is unknown, so an unrestricted name would let markup
  // that is already injected write on* / href / src / style through relabel.
  function writeAttr(el, attr, id, params) {
    if (!attrAllowed(attr)) { warnOnce('attr:' + attr, `attribute "${attr}" is not translatable (allowed: ${TEXT_ATTRS.join(', ')}, aria-*); skipped`); return; }
    el.setAttribute(attr, t(id, params));
  }

  function ti(el, id, params, opts) {
    if (!el) return el;
    if (opts && opts.attr) {
      if (!attrAllowed(opts.attr)) { writeAttr(el, opts.attr, id, params); return el; } // warns, binds nothing
      bindAttr(el, opts.attr, id, params, writeAttr);
    } else bindText(el, id, params, writeText);
    return el;
  }

  const relabel = root => relabelTree(root || doc, { writeText, writeAttr });

  function apply(lang) {
    const prev = current;
    current = lang;
    if (doc && doc.documentElement) doc.documentElement.setAttribute('lang', bcp47(lang));
    clearCache();
    writeStore(lang);
    if (doc) relabel(doc);
    const payload = { lang, prev };
    if (emit) emit('i18n:change', payload);
    for (const fn of Array.from(listeners)) { try { fn(payload); } catch (e) { if (win) win.setTimeout(() => { throw e; }); } }
  }

  const known = lang => LANGS.includes(lang) || loader.has(lang);
  const TEXT_ATTRS = ['title', 'placeholder', 'alt', 'label', 'value', 'data-tip'];
  const ID_REF_ARIA = /^aria-(labelledby|describedby|controls|owns|activedescendant|details|errormessage|flowto)$/;
  const attrAllowed = attr => TEXT_ATTRS.includes(attr) || (/^aria-[a-z]+$/.test(attr) && !ID_REF_ARIA.test(attr));

  const superseded = lang => Object.assign(new Error(`language switch to ${lang} superseded by a later request`), { code: 'superseded' });
  const writeStore = lang => { if (store) { writingStore = true; try { store.set('lang', lang); } finally { writingStore = false; } } };
  // best effort: a missing en_US must not block a language switch
  const preloadFallbacks = lang => Promise.all([FALLBACK_LANG, SOURCE_LANG].filter(l => l !== lang && loader.has(l)).map(l => loader.load(l).catch(() => null)));

  async function set(lang) {
    if (!known(lang)) throw Object.assign(new Error(`unsupported language: ${lang}`), { code: 'unsupported_language' });
    const token = ++seq; // the latest set() wins; an earlier one still loading rejects 'superseded'
    if (lang === current && catalogs.has(lang)) return lang;
    try {
      await loader.load(lang);
    } catch (e) {
      if (token !== seq) throw superseded(lang);
      // the current language stays; if the store was written ahead of us (hello / host:language),
      // put it back so it names the applied language and a later write of the same value retries
      if (store && store.get('lang') === lang && lang !== current) writeStore(current);
      throw e;
    }
    await preloadFallbacks(lang);
    if (token !== seq) throw superseded(lang);
    apply(lang);
    return lang;
  }

  function drain(win_) {
    const q = win_ && win_.T && win_.T.__i18nq;
    if (Array.isArray(q)) { for (const [l, c] of q.splice(0)) register(l, c); }
  }

  let booted = false;
  const follow = v => set(v).catch(e => { if (e && e.code !== 'superseded') warn(`language ${v}: ${e.message}`); });
  async function boot() {
    drain(win);
    if (store && !booted) {
      // The page language has one source: hello.lang, then host:language (architecture §3.5).
      // The rt client writes them into the store; follow every such write (whatever the value:
      // a write equal to the applied language still supersedes a switch in flight), so the
      // catalog is loaded and the page relabelled. The store is not pre-written here: it stays
      // undefined until the first hello, so that write always notifies.
      store.sub('lang', v => { if (!writingStore && typeof v === 'string') follow(v); });
      const early = store.get('lang'); // hello already arrived
      if (typeof early === 'string' && early !== current) follow(early);
    }
    booted = true;
    // Preload only (no switch): the guess and its fallbacks are fetched, so when hello.lang
    // names the guessed language the switch is immediate. Failures are ignored.
    const nav = win && win.navigator ? (win.navigator.languages && win.navigator.languages.length ? win.navigator.languages : [win.navigator.language]) : [];
    const guess = guessLanguage(nav);
    // the source language never falls back, so a zh guess needs nothing beyond zh_CN
    if (guess !== SOURCE_LANG) await Promise.all([loader.has(guess) ? loader.load(guess).catch(() => null) : null, preloadFallbacks(guess)]);
    return current;
  }

  const api = {
    get lang() { return current; },
    set, boot, register, has, hasAny, relabel,
    t, tx, ti,
    guess: guessLanguage,
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    relocalizePage,
    languages: () => [...LANGS, ...Object.keys(files).filter(l => !LANGS.includes(l))],
    loaded: () => [...catalogs.keys()],
    stats: () => ({ fallback: counts.fallback, missing: counts.missing, byId: Object.fromEntries(counts.byId) }),
    traced: () => Object.fromEntries([...traced].map(([s, ids]) => [s, [...ids]])),
    /** For T.fmt (intl.js): the message for id only when the current language has it. */
    exact(id, params) { if (!has(id)) return null; const r = parts(id, params, current); return r ? partsToText(r.parts) : null; },
    text(id, params) { const r = parts(id, params); return r ? partsToText(r.parts) : null; },
  };
  return api;
}

/** Install into a page: T.t / T.tx / T.ti / T.i18n, bound to T.store / T.emit and T.fmt. */
export function install(win) {
  const T = win.T = win.T || {};
  if (T.i18n && T.i18n.__installed) return T.i18n;
  // __LOCALES__ is a JSON string of {lang: file} (build/locales.mjs); an object is accepted too
  const raw = typeof __LOCALES__ !== 'undefined' ? __LOCALES__ : '{}';
  const files = typeof raw === 'string' ? JSON.parse(raw) : raw;
  const dev = typeof __I18N_DEV__ !== 'undefined' ? __I18N_DEV__ : false;
  const trace = typeof __I18N_TRACE__ !== 'undefined' ? __I18N_TRACE__ : false;
  const rt = createI18n({ win, doc: win.document, store: T.store, emit: T.emit, files, dev, trace });
  Object.defineProperty(rt, '__installed', { value: true });
  T.i18n = rt;
  T.t = rt.t;
  T.tx = rt.tx;
  T.ti = rt.ti;
  bindRuntime({ lang: () => rt.lang, exact: rt.exact, text: rt.text });
  // language files that ran before the runtime (the statically preloaded source language)
  const q = T.__i18nq;
  if (Array.isArray(q)) { for (const [l, c] of q.splice(0)) rt.register(l, c); }
  return rt;
}
