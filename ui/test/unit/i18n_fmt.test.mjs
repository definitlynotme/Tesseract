// T-P4-03: T.fmt on Intl (architecture §3.4, 映射_core §3.6 F3–F12).
//  1. snapshot of every function in zh_CN, en_US, de_DE, ru_RU, ja_JP with the real catalogs
//     (ui/test/fixtures/i18n/fmt_snapshot.json; UPDATE_I18N_SNAPSHOT=1 rewrites it);
//  2. zh_CN against the prototype implementation (core.js:181-218 before T-P4-03): every
//     difference must belong to a category of fixtures/i18n/fmt_allowed_diffs.json (critique E1).
process.env.TZ = 'Asia/Shanghai';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { HERE, UI, loadI18n } from '../fixtures/i18n/helpers.mjs';
import { readCatalogs, compileCatalogs } from '../../build/locales.mjs';

const m = await loadI18n();
const rt = m.createI18n({});
for (const [lang, cat] of Object.entries(compileCatalogs(readCatalogs(UI)))) rt.register(lang, cat);
m.bindRuntime({ lang: () => rt.lang, exact: rt.exact, text: rt.text });
const F = m.fmt;
const KB = 1024, MB = KB * 1024, GB = MB * 1024;
const NOW = Date.UTC(2026, 9, 7, 6, 30, 0); // 14:30:00 in Asia/Shanghai
const TS = Date.UTC(2026, 9, 7, 1, 4, 5);   // 09:04:05

const CASES = {
  bytesParts: [[0], [512], [1000], [1536], [1.5 * MB], [1234.5 * GB]],
  bytes: [[0], [7.29 * GB], [1023]],
  rateParts: [[1.55 * MB]],
  rate: [[1.55 * MB], [800]],
  ms: [[null], [42.4], [1234]],
  ago: [[NOW - 10e3, NOW], [NOW - 5 * 60e3, NOW], [NOW - 3 * 3600e3, NOW], [NOW - 2 * 86400e3, NOW]],
  time: [[TS]], hm: [[TS]],
  clock: [[3725]],
  uptime: [[30], [300], [3725]],
  pct: [[0.456], [0.456, 1], [12.5]],
  num: [[1234567.891], [42]],
  latBand: [[100], [500], [900], [null]],
  latWord: [[100], [500], [900], [null]],
  region: [['JP'], ['us'], ['']],
  list: [[['Vanguard', 'Sentinel', 'Bastion']]],
  compare: [['a', 'b']],
};
const LANGS = ['zh_CN', 'en_US', 'de_DE', 'ru_RU', 'ja_JP'];

async function snapshot() {
  const out = {};
  for (const lang of LANGS) {
    await rt.set(lang);
    out[lang] = {};
    for (const [fn, calls] of Object.entries(CASES)) out[lang][fn] = calls.map(args => F[fn](...args));
  }
  await rt.set('zh_CN');
  return out;
}

test('T.fmt keeps the prototype names and shapes', () => {
  for (const k of ['KB', 'MB', 'GB']) assert.equal(typeof F[k], 'number');
  for (const k of ['bytesParts', 'bytes', 'rateParts', 'rate', 'ms', 'ago', 'time', 'hm', 'clock', 'uptime', 'pct', 'num', 'latBand', 'latWord', 'region', 'list', 'compare', 'collator']) assert.equal(typeof F[k], 'function', k);
  assert.ok(Array.isArray(F.bytesParts(2048)) && F.bytesParts(2048).length === 2, 'bytesParts → [num, unit]');
  assert.ok(Array.isArray(F.rateParts(2048)) && F.rateParts(2048).length === 2, 'rateParts → [num, unit]');
});

test('T.fmt snapshot: zh_CN, en_US, de_DE, ru_RU, ja_JP (unit spacing, 24-hour clock, relative time, region names)', async () => {
  const snap = await snapshot();
  const file = join(HERE, 'fmt_snapshot.json');
  // The expected strings come from Node's ICU/CLDR; WebView2 or another Node may word them
  // differently. The snapshot records the engine it was taken with, and a mismatch on a
  // different engine says so instead of looking like a regression.
  const engine = { icu: process.versions.icu, cldr: process.versions.cldr, unicode: process.versions.unicode };
  if (process.env.UPDATE_I18N_SNAPSHOT) writeFileSync(file, JSON.stringify({ _engine: engine, ...snap }, null, 1) + '\n');
  const { _engine: taken, ...want } = JSON.parse(readFileSync(file, 'utf8'));
  assert.ok(taken && taken.icu && taken.cldr, 'fmt_snapshot.json records the ICU/CLDR versions it was taken with');
  const sameEngine = taken.icu === engine.icu && taken.cldr === engine.cldr;
  assert.deepEqual(snap, want, sameEngine ? undefined
    : `T.fmt output differs from fmt_snapshot.json, which was taken with ICU ${taken.icu} / CLDR ${taken.cldr}; this run uses ICU ${engine.icu} / CLDR ${engine.cldr}. ` +
      'If only the Intl wording changed, re-take it (UPDATE_I18N_SNAPSHOT=1) and note the version in fmt_allowed_diffs.json.');
  const diffs = JSON.parse(readFileSync(join(HERE, 'fmt_allowed_diffs.json'), 'utf8'));
  assert.deepEqual(diffs.engine, { icu: taken.icu, cldr: taken.cldr }, 'the allowed-differences table names the ICU/CLDR versions of the snapshot');
  // spot checks of the rules themselves
  assert.equal(snap.zh_CN.bytes[1], '7.29 GB');
  assert.equal(snap.zh_CN.rate[0], '1.55 MB/s');
  assert.equal(snap.zh_CN.ms[1], '42 ms');
  assert.equal(snap.zh_CN.ago[1], '5 分钟前');
  assert.equal(snap.zh_CN.uptime[2], '1 小时 2 分');
  assert.equal(snap.zh_CN.time[0], '09:04:05');
  assert.equal(snap.en_US.time[0], '09:04:05', '24-hour clock in English too');
  assert.equal(snap.en_US.ago[1], '5 minutes ago');
  assert.equal(snap.de_DE.ago[1], 'vor 5 Minuten');
  assert.equal(snap.de_DE.bytes[1], '7,29 GB');
  assert.equal(snap.ru_RU.region[0], 'Япония');
  assert.equal(snap.ja_JP.region[0], '日本');
  assert.equal(snap.en_US.region[1], 'United States');
  assert.equal(snap.en_US.list[0], 'Vanguard, Sentinel, and Bastion');
  // (T-P5-04) since P5 the catalogs word what Intl cannot or words differently: latency words,
  // ago "now", uptime, ru_RU byte units (fmt.* in each language; re-taken 10-20, report T-P5-04)
  assert.equal(snap.de_DE.latWord[0], 'Normal', 'latency words come from the de_DE catalog (fmt.lat.*)');
  assert.equal(snap.en_US.ago[0], 'just now');
  assert.equal(snap.en_US.uptime[2], '1 h 2 min');
  assert.deepEqual(snap.ru_RU.bytesParts[5], ['1 234,50', 'ГБ']);
  assert.equal(snap.ru_RU.rate[0], '1,55 МБ/с');
  assert.equal(snap.ja_JP.latWord[2], '利用不可');
});

// The prototype implementation, verbatim from core.js:181-218 before T-P4-03.
const p2 = n => String(Math.floor(n)).padStart(2, '0');
const OLD = {
  bytesParts(n) { n = Math.max(0, n || 0); if (n >= GB) return [(n / GB).toFixed(2), 'GB']; if (n >= MB) return [(n / MB).toFixed(2), 'MB']; if (n >= KB) return [(n / KB).toFixed(2), 'KB']; return [String(Math.round(n)), 'B']; },
  bytes(n) { const [a, b] = OLD.bytesParts(n); return `${a} ${b}`; },
  rateParts(n) { const [a, b] = OLD.bytesParts(n); return [a, `${b}/s`]; },
  rate(n) { const [a, b] = OLD.bytesParts(n); return `${a} ${b}/s`; },
  ms: n => (n == null ? '—' : `${Math.round(n)} ms`),
  ago(ts, now) { const s = Math.max(0, ((now || Date.now()) - ts) / 1000); if (s < 45) return '刚刚'; if (s < 3600) return `${Math.max(1, Math.round(s / 60))} 分钟前`; if (s < 86400) return `${Math.floor(s / 3600)} 小时前`; return `${Math.floor(s / 86400)} 天前`; },
  time(ts) { const d = new Date(ts); return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`; },
  hm(ts) { const d = new Date(ts); return `${p2(d.getHours())}:${p2(d.getMinutes())}`; },
  clock: s => `${p2(s / 3600)}:${p2(s / 60 % 60)}:${p2(s % 60)}`,
  uptime(s) { s = Math.floor(s || 0); if (s < 60) return '不到 1 分钟'; const h = Math.floor(s / 3600), m = Math.floor(s / 60 % 60); return h ? `${h} 小时 ${m} 分` : `${m} 分钟`; },
  pct: (x, d) => `${(x * 100).toFixed(d || 0)}%`,
  num: n => Number(n).toLocaleString('zh-CN'),
  latBand(ms, ceiling) { const c = ceiling || 800; return ms == null || ms > c ? 'down' : ms > c / 2 ? 'slow' : 'ok'; },
  latWord(ms, ceiling) { return { ok: '正常', slow: '较慢', down: '不可用' }[OLD.latBand(ms, ceiling)]; },
};

// zh_CN grid: values on and around every boundary the functions have.
function grid() {
  const bytes = [0, 1, 512, 999, 1000, 1023, 1024, 1025, 1536, 1.005 * KB, 1.125 * KB, 999.995 * KB, 1000 * KB, 1023.99 * KB,
    MB, 1.005 * MB, 1.555 * MB, 500 * MB, 1000 * MB, GB, 1.005 * GB, 999.999 * GB, 1000 * GB, 1234.5 * GB, 123456 * GB, -5, NaN, undefined];
  const out = [];
  for (const fn of ['bytesParts', 'bytes', 'rateParts', 'rate']) for (const n of bytes) out.push([fn, [n]]);
  for (const n of [null, undefined, 0, 0.4, 0.5, 1.5, 42.4, 999.5, 1000, 1234, 65432.1]) out.push(['ms', [n]]);
  for (const s of [0, 10, 44, 45, 89, 90, 1799, 3599, 3600, 7199, 86399, 86400, 30 * 86400]) out.push(['ago', [NOW - s * 1000, NOW]]);
  for (const ts of [TS, Date.UTC(2026, 0, 1, 16, 0, 0), Date.UTC(2026, 6, 1, 15, 59, 59)]) { out.push(['time', [ts]]); out.push(['hm', [ts]]); }
  for (const s of [0, 59, 60, 3599, 3600, 3725, 86400 + 61, 360000]) { out.push(['clock', [s]]); out.push(['uptime', [s]]); }
  for (const [x, d] of [[0, undefined], [0.456, undefined], [0.456, 1], [0.0005, 2], [0.125, 1], [0.135, 1], [1, 0], [12.5, 0], [123.4, 1]]) out.push(['pct', [x, d]]);
  for (const n of [0, 42, 1234, 1234567.891, 0.12345, -3, '17']) out.push(['num', [n]]);
  for (const v of [null, 0, 100, 400, 401, 800, 801, 2000]) { out.push(['latBand', [v]]); out.push(['latWord', [v]]); out.push(['latWord', [v, 300]]); }
  return out;
}

// Categories a zh_CN difference may fall into (fmt_allowed_diffs.json).
const strip = s => String(s).replace(/,/g, '');
function classify(oldV, newV) {
  const a = Array.isArray(oldV) ? oldV.join(' ') : String(oldV);
  const b = Array.isArray(newV) ? newV.join(' ') : String(newV);
  if (a === b) return null;
  if (strip(b) === a) return 'grouping';
  const na = parseFloat(a.replace(/[^\d.\-]/g, '')), nb = parseFloat(strip(b).replace(/[^\d.\-]/g, ''));
  const decimals = (a.split(' ')[0].split('.')[1] || '').length;
  if (!Number.isNaN(na) && !Number.isNaN(nb) && strip(b).replace(/[\d.\-]/g, '') === a.replace(/[\d.\-]/g, '') && Math.abs(na - nb) <= Math.pow(10, -decimals) + 1e-9) return 'rounding';
  if (a === 'NaN%' || a.startsWith('NaN') || b === 'NaN') return 'nan';
  return 'other';
}

test('zh_CN output vs the prototype: only the differences listed in fmt_allowed_diffs.json', async () => {
  await rt.set('zh_CN');
  const allowed = JSON.parse(readFileSync(join(HERE, 'fmt_allowed_diffs.json'), 'utf8'));
  const found = {};
  for (const [fn, args] of grid()) {
    const cat = classify(OLD[fn](...args), F[fn](...args));
    if (!cat) continue;
    (found[fn] ||= {})[cat] = (found[fn][cat] || 0) + 1;
    const ok = (allowed.zh_CN[fn] || []).some(r => r.category === cat);
    assert.ok(ok, `${fn}(${args.map(a => JSON.stringify(a)).join(', ')}): ${JSON.stringify(OLD[fn](...args))} → ${JSON.stringify(F[fn](...args))} (${cat}) is not an allowed difference`);
  }
  // and every listed zh_CN difference really happens (the table is not padded)
  for (const [fn, rows] of Object.entries(allowed.zh_CN)) for (const r of rows) assert.ok(found[fn] && found[fn][r.category], `${fn}: listed ${r.category} never occurs`);
  // the examples in the table are true
  for (const [fn, rows] of Object.entries(allowed.zh_CN)) {
    for (const r of rows) {
      assert.deepEqual(OLD[fn](...r.example.args), r.example.before, `${fn} before`);
      assert.deepEqual(F[fn](...r.example.args), r.example.after, `${fn} after`);
    }
  }
});

test('latBand thresholds are unchanged and latWord follows the catalog', () => {
  for (const v of [null, 0, 400, 401, 800, 801]) assert.equal(F.latBand(v), OLD.latBand(v));
  assert.equal(F.latBand(301, 600), 'slow');
  assert.equal(F.latWord(1000), '不可用');
});
