// T.fmt on Intl (T-P4-03; P4 architecture §3.4; 映射_core §3.6 F3–F12).
//
// Every Intl object is cached per language and dropped by clearCache() when the language
// changes. Unit names, the number–unit joiner and the few phrases Intl cannot produce live
// in the `fmt` catalog namespace (DESIGN §8: "42 ms · 7.29 GB · 1.55 MB/s").
//
// Resolution for wording that Intl can also produce (units, relative time, uptime): the
// catalog of the *current* language when it has the id, otherwise Intl's own wording for
// that language (so de_DE without a translation reads "vor 5 Minuten", never zh_CN text).
// Wording Intl cannot produce (latency words) follows the normal fallback chain.
//
// The runtime (t.js) binds itself with bindRuntime(); until then this module formats as
// zh_CN with Intl wording, so core.js may import it before the i18n runtime exists.
//
// Function names and return shapes are the prototype's (core.js:181-218): bytesParts and
// rateParts still return [num, unit] arrays; new: region, list, compare, collator.

export const KB = 1024, MB = KB * 1024, GB = MB * 1024;

const BCP47 = {
  zh_CN: 'zh-CN', en_US: 'en-US', de_DE: 'de-DE', es_ES: 'es-ES', fr_FR: 'fr-FR',
  ja_JP: 'ja-JP', ko_KR: 'ko-KR', pt_BR: 'pt-BR', ru_RU: 'ru-RU', pseudo: 'en-XA',
};
/** zh_CN → zh-CN (the <html lang> and Intl locale). Unknown values pass through with _ → -. */
export function bcp47(lang) { return BCP47[lang] || String(lang || 'zh_CN').replace('_', '-'); }

let runtime = {
  lang: () => 'zh_CN',
  exact: () => null,   // (id, params) → string when the current language has id, else null
  text: () => null,    // (id, params) → string via the fallback chain, else null
};
/** Called once by t.js. */
export function bindRuntime(r) { runtime = { ...runtime, ...r }; }

const caches = new Map();
/** Per-language memo for Intl objects. */
export function cached(lang, key, make) {
  let m = caches.get(lang);
  if (!m) caches.set(lang, m = new Map());
  let v = m.get(key);
  if (v === undefined) { v = make(bcp47(lang)); m.set(key, v); }
  return v;
}
export function clearCache() { caches.clear(); }

export const numberFormat = (lang, opts = {}) => cached(lang, 'nf:' + JSON.stringify(opts), l => new Intl.NumberFormat(l, opts));
export const dateTimeFormat = (lang, opts = {}) => cached(lang, 'dtf:' + JSON.stringify(opts), l => new Intl.DateTimeFormat(l, opts));
export const pluralRules = lang => cached(lang, 'pr', l => new Intl.PluralRules(l));
// 'auto' only for the zero case ("now"); counts stay numeric ("2 days ago", never "the day before yesterday"),
// matching the zh_CN catalog wording.
const relNow = lang => cached(lang, 'rtf:auto', l => new Intl.RelativeTimeFormat(l, { numeric: 'auto' }));
const relTime = lang => cached(lang, 'rtf', l => new Intl.RelativeTimeFormat(l, { numeric: 'always' }));

/** The ICU formatter environment (icu.js formatParts) for one language. */
export function icuEnv(lang) {
  return {
    number: (n, style) => numberFormat(lang, style === 'percent' ? { style: 'percent' }
      : style === 'integer' ? { maximumFractionDigits: 0 } : {}).format(n),
    date: (v, style) => dateTimeFormat(lang, { dateStyle: style || 'medium' }).format(new Date(v)),
    time: v => dateTimeFormat(lang, { timeStyle: 'short', hourCycle: 'h23' }).format(new Date(v)),
    plural: n => pluralRules(lang).select(n),
  };
}

const lang = () => runtime.lang() || 'zh_CN';
const exact = (id, params, fallback) => { const s = runtime.exact(id, params); return s == null ? fallback() : s; };
const unit = (l, u, n, display) => numberFormat(l, { style: 'unit', unit: u, unitDisplay: display, maximumFractionDigits: 0 }).format(n);
const p2 = n => String(Math.floor(n)).padStart(2, '0');
const BYTE_UNITS = { B: 'fmt.unit.b', KB: 'fmt.unit.kb', MB: 'fmt.unit.mb', GB: 'fmt.unit.gb' };

function unitName(u) { return exact(BYTE_UNITS[u], null, () => u); }
function qty(num, u) { return exact('fmt.qty', { num, unit: u }, () => `${num} ${u}`); }

export const fmt = {
  KB, MB, GB,
  /** [number text, unit] — two decimals from KB up, an integer for bytes. */
  bytesParts(n) {
    const l = lang();
    n = Math.max(0, n || 0);
    const two = numberFormat(l, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (n >= GB) return [two.format(n / GB), unitName('GB')];
    if (n >= MB) return [two.format(n / MB), unitName('MB')];
    if (n >= KB) return [two.format(n / KB), unitName('KB')];
    return [numberFormat(l, { maximumFractionDigits: 0 }).format(Math.round(n)), unitName('B')];
  },
  bytes(n) { const [a, b] = fmt.bytesParts(n); return qty(a, b); },
  rateParts(n) { const [a, b] = fmt.bytesParts(n); return [a, exact('fmt.unit.per_second', { unit: b }, () => `${b}/s`)]; },
  rate(n) { const [a, b] = fmt.rateParts(n); return qty(a, b); },
  ms(n) {
    if (n == null) return '—';
    const v = Math.round(n);
    return exact('fmt.ms', { n: v }, () => unit(lang(), 'millisecond', v, 'short'));
  },
  ago(ts, now) {
    const l = lang();
    const s = Math.max(0, ((now || Date.now()) - ts) / 1000);
    if (s < 45) return exact('fmt.ago.now', null, () => relNow(l).format(0, 'second'));
    if (s < 3600) { const n = Math.max(1, Math.round(s / 60)); return exact('fmt.ago.minutes', { n }, () => relTime(l).format(-n, 'minute')); }
    if (s < 86400) { const n = Math.floor(s / 3600); return exact('fmt.ago.hours', { n }, () => relTime(l).format(-n, 'hour')); }
    const n = Math.floor(s / 86400);
    return exact('fmt.ago.days', { n }, () => relTime(l).format(-n, 'day'));
  },
  /** HH:MM:SS, 24-hour, in the language's digits and separators. */
  time(ts) { return dateTimeFormat(lang(), { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(new Date(ts)); },
  hm(ts) { return dateTimeFormat(lang(), { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(ts)); },
  /** Elapsed seconds as HH:MM:SS (digits only, every language). */
  clock: s => `${p2(s / 3600)}:${p2(s / 60 % 60)}:${p2(s % 60)}`,
  uptime(s) {
    const l = lang();
    s = Math.floor(s || 0);
    if (s < 60) return exact('fmt.uptime.under_minute', null, () => `< ${unit(l, 'minute', 1, 'long')}`);
    const h = Math.floor(s / 3600), m = Math.floor(s / 60 % 60);
    if (h) {
      return exact('fmt.uptime.hours_minutes', { h, m }, () => cached(l, 'lf:unit', b => new Intl.ListFormat(b, { type: 'unit', style: 'narrow' }))
        .format([unit(l, 'hour', h, 'short'), unit(l, 'minute', m, 'short')]));
    }
    return exact('fmt.uptime.minutes', { m }, () => unit(l, 'minute', m, 'long'));
  },
  pct: (x, d) => numberFormat(lang(), { style: 'percent', minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }).format(x),
  num: n => numberFormat(lang()).format(Number(n)),
  /** latency band against the ceiling (default 800): ≤ half 'ok' · ≤ ceiling 'slow' · above / null 'down' */
  latBand(ms, ceiling) { const c = ceiling || 800; return ms == null || ms > c ? 'down' : ms > c / 2 ? 'slow' : 'ok'; },
  latWord(ms, ceiling) { const id = 'fmt.lat.' + fmt.latBand(ms, ceiling); const s = runtime.text(id, null); return s == null ? '' : s; },
  /** Country/region name for an ISO 3166 code (IdentityLeg.country); the code itself when unknown. */
  region(code) {
    if (!code) return '';
    try { return cached(lang(), 'dn:region', b => new Intl.DisplayNames(b, { type: 'region' })).of(String(code).toUpperCase()) || String(code); } catch { return String(code); }
  },
  /** Join items the language's way: type 'conjunction' (default) | 'disjunction' | 'unit'. */
  list(items, { type = 'conjunction', style = 'long' } = {}) {
    return cached(lang(), `lf:${type}:${style}`, b => new Intl.ListFormat(b, { type, style })).format(Array.from(items, String));
  },
  collator() { return cached(lang(), 'col', b => new Intl.Collator(b)); },
  compare(a, b) { return fmt.collator().compare(String(a ?? ''), String(b ?? '')); },
};
