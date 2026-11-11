// UI guard (T-P6-05, item 3b): feed the frames a real sidecar sent to the page's runtime layer.
//
//   node ui/tools/guard/project_frames.mjs --frames <frames.jsonl> [--probe <probe.json>] --out <result.json>
//
// The frames come from ui/tools/guard/sidecar_probe.py (frozen sidecar, isolated scope). They go
// through the same code a page runs (src/rt/client.js: createRt + the projector + topics + calls),
// in node, the way the shell forwards them (shell-only topics stripped, control replies kept by
// the shell). Reported, each a finding of the guard:
//   exceptions      anything the runtime layer threw while taking a frame
//   contract_diag   the runtime layer's own contract diagnostics (unknown topic / kind / key,
//                   bad event, page broken, sequence or rev mismatch, epoch mismatch, ...)
//   reconnects      the runtime layer asked for a reconnect / resync (it judged its data broken)
//   queries         every read-only query of the probe replayed through T.rt.query: it must end
//   type_mismatch   a field whose JSON type differs from the one the UI's own fixtures
//                   (ui/test/fixtures/scenarios, what the pages are tested with) carry for the
//                   same topic / query and path
//   enum_unknown    a value on an enum field of the target tree's bridge_protocol (probe.json
//                   enum_paths: every "a|b|c" / tier field of the v1 topics and query replies, and
//                   the enum fields of the function-validated P1 topics from
//                   sidecar_probe.P1_ENUM_PATHS: readiness.state, jobs state / name, logs.batch
//                   records, ...) that the UI fixtures never carry on the same topic / query and
//                   path, i.e. the pages were never tested with it (a new backend value); plus four
//                   hand-picked checks against the page's own mappings (startup_phase, carrier /
//                   order, update state, node reason_code)
//   text_missing    an error / reason the page would show whose catalog key is missing in zh_CN
//                   (the page falls back to errors.unknown / common.reason_unknown) or in one of
//                   the other 8 languages
// Exit 1 when anything above (except info) was found.
import { existsSync, readFileSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import SPEC from '../../src/rt/spec.gen.json' with { type: 'json' };
import { createRt } from '../../src/rt/client.js';
import { createFakeClock } from '../../src/rt/clock.js';
import { createIds } from '../../src/rt/ids.js';
import { runOf, TIER_IDS, updateStateOf } from '../../src/rt/project.js';
import { REASON_CODES } from '../../src/rt/project_tiers.js';
import { errorText, reasonText } from '../../src/rt/errors.js';
import { loadCatalogs, LANGS, SOURCE, REASON_NAMESPACES } from '../i18n_check.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
export const UI = resolve(HERE, '..', '..');
export const SCENARIOS = join(UI, 'test', 'fixtures', 'scenarios');
const PAGE_STRIPPED = new Set(['ui.language', 'alerts.desktop']);
// Diagnostics of src/rt that mean "the frames do not fit what the page expects".
const CONTRACT_DIAG = new Set(['bad_frame', 'unknown_frame', 'unknown_topic', 'bad_kind', 'bad_key', 'bad_evt',
  'bad_batch_item', 'frame_threw', 'page_broken', 'epoch_mismatch', 'jobs_sequence_gap', 'edge_seq_jump',
  'logs_seq_jump', 'replacement_too_old', 'res_cmd_mismatch', 'res_mode', 'ack_mode']);
const CONTRACT_DIAG_PREFIX = ['topic_'];
// Which reason namespace a request / job name renders its reason_code with (P4 mapping docs;
// ui/tools/backend_keys.py builds the same vocabularies from the backend).
export function reasonNamespace(name) {
  const n = String(name || '');
  if (n === 'alerts.test') return 'alerts.test_reason';
  if (n === 'exit.verify') return 'overview.exit_reason';
  if (n.startsWith('alerts.onboarding.')) return 'alerts.onboarding_reason';
  if (n.startsWith('host.')) return 'shell.host_reason';
  if (n.startsWith('nodes.') || n.startsWith('subscriptions.') || n.startsWith('import.')) return 'tiers.reason';
  return null;
}

// ---- JSON type signatures ------------------------------------------------------------
// '' counts as empty, like null: YAML-derived config leaves unset fields '' whatever their kind
// (e.g. the template's ntfy priority), and the pages treat both as "no value".
const typeOf = v => (v === null || v === '' ? 'null' : Array.isArray(v) ? 'array' : typeof v === 'number' ? 'number' : typeof v);
export function signature(value, sig = new Map(), path = '') {
  const t = typeOf(value);
  if (!sig.has(path)) sig.set(path, new Set());
  sig.get(path).add(t);
  if (t === 'array') for (const item of value) signature(item, sig, `${path}[]`);
  else if (t === 'object') for (const [k, v] of Object.entries(value)) signature(v, sig, `${path}.${k}`);
  return sig;
}
const addSig = (all, key, value) => { if (!all.has(key)) all.set(key, new Map()); signature(value, all.get(key)); };

// ---- string values on each path (enum coverage) -----------------------------------------
export function stringValues(value, map = new Map(), path = '') {
  if (typeof value === 'string') { if (!map.has(path)) map.set(path, new Set()); map.get(path).add(value); }
  else if (Array.isArray(value)) for (const v of value) stringValues(v, map, `${path}[]`);
  else if (value && typeof value === 'object') for (const [k, v] of Object.entries(value)) stringValues(v, map, `${path}.${k}`);
  return map;
}
const addVals = (all, key, value) => { if (!all.has(key)) all.set(key, new Map()); stringValues(value, all.get(key)); };
// A list / keyed patch carries {changed, removed}: its rows are the snapshot's items.
const eventData = item => (item.kind === 'patch' && item.data && Array.isArray(item.data.changed) ? { items: item.data.changed } : item.data);

function evtItems(frame) {
  if (!frame || typeof frame !== 'object') return [];
  if (frame.t === 'evt') return [frame];
  if (frame.t === 'batch' && Array.isArray(frame.items)) return frame.items.filter(i => i && i.t === 'evt');
  return [];
}

/** Type signatures of the UI fixtures: topic:<name> (event data) and res:<cmd> (query data). */
export function fixtureSignatures(dir = SCENARIOS) {
  return fixtureIndex(dir).sigs;
}

/** {sigs, values}: the type signatures and the string values of the UI fixtures, per topic:<name> /
 * res:<cmd> and path (values: list / keyed patch rows count as `.items[]`). */
export function fixtureIndex(dir = SCENARIOS) {
  const all = new Map(), values = new Map();
  if (!existsSync(dir)) return { sigs: all, values };
  for (const name of readdirSync(dir).filter(n => n.endsWith('.json'))) {
    let doc;
    try { doc = JSON.parse(readFileSync(join(dir, name), 'utf8')); } catch { continue; }
    const steps = Array.isArray(doc) ? doc : (doc && doc.steps) || [];
    const frames = [...((doc && doc.frames) || []), ...((doc && doc.replay) || [])];
    for (const step of steps) {
      if (!step || typeof step !== 'object') continue;
      if (step.deliver) frames.push(step.deliver);
      if (step.on_invoke && Array.isArray(step.reply)) {
        for (const r of step.reply) if (r && r.t === 'res' && r.ok === true && r.data !== undefined) {
          addSig(all, `res:${step.on_invoke.cmd}`, r.data);
          addVals(values, `res:${step.on_invoke.cmd}`, r.data);
        }
      }
    }
    for (const f of frames) {
      for (const item of evtItems(f)) if (item.data !== undefined && typeof item.topic === 'string') {
        addSig(all, `topic:${item.topic}`, item.data);
        addVals(values, `topic:${item.topic}`, eventData(item));
      }
      if (f && f.t === 'res' && f.ok === true && typeof f.cmd === 'string' && f.data !== undefined) {
        addSig(all, `res:${f.cmd}`, f.data);
        addVals(values, `res:${f.cmd}`, f.data);
      }
    }
  }
  return { sigs: all, values };
}

/** String values the page received, per topic:<name> / res:<cmd> and path. */
export function realValues(page, queries = []) {
  const real = new Map();
  for (const f of page) for (const item of evtItems(f)) if (item.data !== undefined) addVals(real, `topic:${item.topic}`, eventData(item));
  for (const q of queries) if (q.reply && q.reply.ok === true && q.reply.data !== undefined) addVals(real, `res:${q.cmd}`, q.reply.data);
  return real;
}

export const ENUM_FIX_FILE = 'ui/src/rt（或页面）的映射；ui/test/fixtures/scenarios';

export const ENUM_BASELINE = join(HERE, 'enum_baseline.json');

/** The accepted gaps {key: {path: [values]}}: declared enum values the UI fixtures do not carry yet,
 * recorded when the rule was introduced (ui/tools/guard/enum_baseline.json). A value listed there is
 * reported as known, any other untested value fails. */
export function loadBaseline(path = ENUM_BASELINE) {
  if (!path || !existsSync(path)) return {};
  const doc = JSON.parse(readFileSync(path, 'utf8'));
  return (doc && doc.gaps) || {};
}
const inBaseline = (baseline, key, path, value) => !!(baseline[key] && Array.isArray(baseline[key][path]) && baseline[key][path].includes(value));

/** Real values on an enum path of the target bridge_protocol (`enumPaths`, from probe.json) that
 * the UI fixtures never carry on the same key and path: the pages were never tested with them.
 * -> {findings, known} (known: listed in the baseline). */
export function enumCoverageFindings(real, fixtureValues, enumPaths = {}, baseline = {}) {
  const findings = [], known = [];
  for (const [key, paths] of Object.entries(enumPaths || {})) {
    const got = real.get(key);
    if (!got) continue;
    const fx = fixtureValues.get(key);
    for (const [path, declared] of Object.entries(paths)) {
      const vals = got.get(path);
      if (!vals) continue;
      const tested = fx && fx.get(path);
      for (const value of [...vals].sort()) {
        if (tested && tested.has(value)) continue;
        const row = { rule: 'untested_value', topic: key, path, value, declared: Array.isArray(declared) && declared.includes(value),
          tested: tested ? [...tested].sort() : [], file: ENUM_FIX_FILE };
        (inBaseline(baseline, key, path, value) ? known : findings).push(row);
      }
    }
  }
  return { findings, known };
}

/** Topics a page never receives (the shell keeps them): not part of the page's enum coverage. */
export const pageTopicKey = (key, spec = SPEC) => {
  if (!key.startsWith('topic:')) return true;
  const t = key.slice(6);
  return !(PAGE_STRIPPED.has(t) || (spec.topics[t] && spec.topics[t].shell_only));
};

/** Static coverage (guard item 1): every declared enum value of the page's topics and query replies
 * against the UI fixtures. -> {gaps: {key: {path: [values]}}, new: [...], stale: [...]} where `new`
 * are gaps not in the baseline (a value the backend added) and `stale` baseline entries that are no
 * longer gaps (covered now, or gone from the protocol). */
export function staticEnumCoverage(enumPaths, fixtureValues, baseline = {}, spec = SPEC) {
  const gaps = {}, fresh = [], stale = [];
  for (const [key, paths] of Object.entries(enumPaths || {})) {
    if (!pageTopicKey(key, spec)) continue;
    const fx = fixtureValues.get(key);
    for (const [path, declared] of Object.entries(paths)) {
      const tested = fx && fx.get(path);
      const gap = (declared || []).filter(v => !(tested && tested.has(v)));
      if (!gap.length) continue;
      (gaps[key] ||= {})[path] = gap;
      for (const value of gap) if (!inBaseline(baseline, key, path, value)) {
        fresh.push({ topic: key, path, value, declared, tested: tested ? [...tested].sort() : [], file: ENUM_FIX_FILE });
      }
    }
  }
  for (const [key, paths] of Object.entries(baseline || {})) {
    for (const [path, values] of Object.entries(paths || {})) {
      for (const value of values || []) if (!(gaps[key] && gaps[key][path] && gaps[key][path].includes(value))) stale.push({ topic: key, path, value });
    }
  }
  return { gaps, new: fresh, stale };
}

/** Paths both sides have where the real (non-null) type is one the fixtures never carry. */
export function typeMismatches(real, fixtures) {
  const out = [];
  for (const [key, sig] of real) {
    const fx = fixtures.get(key);
    if (!fx) continue;
    for (const [path, types] of sig) {
      const want = fx.get(path);
      if (!want) continue;
      const r = [...types].filter(t => t !== 'null'), w = [...want].filter(t => t !== 'null');
      if (!r.length || !w.length) continue;
      const bad = r.filter(t => !w.includes(t));
      if (bad.length) out.push({ key, path: path || '(root)', real: r.sort(), fixtures: w.sort() });
    }
  }
  return out;
}

// ---- texts -------------------------------------------------------------------------------
/** Which catalog key the page's own fallback chain (src/rt/errors.js errorText / reasonText) lands
 * on, with zh_CN as the catalog: {hit, asked} (hit null: no key of the chain exists). Calling the
 * page's functions keeps the guard in step with any change of the page's fallback rules
 * (review A SHOULD 4). */
export function pageTextKey(zh, render) {
  const asked = [];
  let hit = null;
  const T = { t: id => id, i18n: { hasAny: id => { asked.push(id); const ok = !!zh[id]; if (ok && hit === null) hit = id; return ok; } } };
  render(T);
  return { hit, asked };
}
export const ERROR_FALLBACK = 'errors.unknown';
export const REASON_FALLBACK = 'common.reason_unknown';

function errorObjects(value, ctx, out = [], depth = 0) {
  if (!value || typeof value !== 'object' || depth > 12) return out;
  if (!Array.isArray(value) && typeof value.code === 'string' && ('message_key' in value || 'params' in value)) out.push({ error: value, ctx });
  for (const v of Array.isArray(value) ? value : Object.values(value)) errorObjects(v, ctx, out, depth + 1);
  return out;
}

export function textFindings(frames, queries, catalogs) {
  const zh = catalogs.langs[SOURCE] || {};
  const others = LANGS.filter(l => l !== SOURCE);
  const findings = [], info = [];
  const seen = new Set();
  const translations = id => others.filter(l => !(catalogs.langs[l] && catalogs.langs[l][id] && String(catalogs.langs[l][id].text).trim()));
  const checkKey = (id, where) => {
    const k = `${id}`;
    if (seen.has(k)) return;
    seen.add(k);
    const lack = translations(id);
    if (lack.length) findings.push({ kind: 'translation_missing', id, langs: lack, where });
  };
  const sources = [];
  for (const f of frames) {
    if (f.t === 'res' || f.t === 'error') sources.push([f, f.cmd || null]);
    else if (f.t === 'ack') sources.push([f, f.cmd || null]);
    for (const item of evtItems(f)) sources.push([item.data, item.topic === 'jobs' ? item.data && item.data.name : `topic:${item.topic}`]);
  }
  for (const q of queries || []) sources.push([q.reply, q.cmd]);
  for (const [value, ctx] of sources) {
    for (const { error, ctx: c } of errorObjects(value, ctx)) {
      const { hit, asked } = pageTextKey(zh, T => errorText(T, error));
      const chain = asked.filter(id => id !== ERROR_FALLBACK || error.message_key === ERROR_FALLBACK);
      if (!hit || (hit === ERROR_FALLBACK && error.message_key !== ERROR_FALLBACK)) {
        findings.push({ kind: 'error_text_missing', id: chain[0] || null, chain, code: error.code, where: c, shows: ERROR_FALLBACK });
      } else checkKey(hit, c);
      const reason = (error.params && error.params.reason_code) || error.reason_code;
      if (typeof reason === 'string' && reason) reasonCheck(reason, c);
    }
    // ack.skipped_reason and topic rows with reason_code
    if (value && typeof value === 'object' && typeof value.skipped_reason === 'string') reasonCheck(value.skipped_reason, 'skipped', 'common.skipped');
  }
  function reasonCheck(reason, ctx, nsGiven = null) {
    const ns = nsGiven || reasonNamespace(ctx);
    if (!ns) { info.push({ kind: 'reason_unmapped', reason, where: ctx }); return; }
    const { hit, asked } = pageTextKey(zh, T => reasonText(T, ns, reason));
    const id = asked[0] || `${ns}.${reason}`;
    if (!hit || hit === REASON_FALLBACK) findings.push({ kind: 'reason_text_missing', id, reason, where: ctx, shows: REASON_FALLBACK });
    else checkKey(hit, ctx);
  }
  return { findings, info };
}

// ---- enums the projection maps ---------------------------------------------------------------
export function enumFindings(frames) {
  const out = [];
  const updateKnown = s => updateStateOf({ state: s, update_available: true }) !== updateStateOf({ state: '\u0000guard-unknown', update_available: true });
  for (const f of frames) {
    for (const item of evtItems(f)) {
      const d = item.data;
      if (!d || typeof d !== 'object') continue;
      if (item.topic === 'service.state' && d.runtime && d.runtime.startup_phase != null && runOf(d) === 'unknown') {
        out.push({ topic: item.topic, path: 'runtime.startup_phase', value: d.runtime.startup_phase, file: 'ui/src/rt/project.js (RUN_OF)' });
      }
      if (item.topic === 'tiers.summary') {
        if (d.carrier != null && !TIER_IDS.includes(d.carrier)) out.push({ topic: item.topic, path: 'carrier', value: d.carrier, file: 'ui/src/rt/project.js (TIER_IDS)' });
        for (const t of Array.isArray(d.order) ? d.order : []) if (!TIER_IDS.includes(t)) out.push({ topic: item.topic, path: 'order[]', value: t, file: 'ui/src/rt/project.js (TIER_IDS)' });
      }
      if (item.topic === 'update.state' && d.state != null && !updateKnown(d.state)) {
        out.push({ topic: item.topic, path: 'state', value: d.state, file: 'ui/src/rt/project.js (updateStateOf)' });
      }
      if (item.topic === 'nodes.list') {
        const rows = [...(Array.isArray(d.items) ? d.items : []), ...(Array.isArray(d.changed) ? d.changed : [])];
        for (const n of rows) if (n && typeof n.reason_code === 'string' && n.reason_code && !REASON_CODES.includes(n.reason_code)) {
          out.push({ topic: item.topic, key: item.key, path: 'items[].reason_code', value: n.reason_code, file: 'ui/src/rt/project_tiers.js (REASON_CODES)' });
        }
      }
    }
  }
  return out;
}

// ---- feeding the runtime layer ----------------------------------------------------------------
function makeT(catalogs) {
  const bus = new Map(), state = Object.create(null), subs = new Map();
  const zh = catalogs.langs[SOURCE] || {};
  const T = { events: [] };
  T.on = (e, fn) => { let s = bus.get(e); if (!s) bus.set(e, s = new Set()); s.add(fn); return () => s.delete(fn); };
  T.emit = (e, p) => { T.events.push([e, p]); const s = bus.get(e); if (s) for (const fn of [...s]) fn(p); };
  T.store = {
    get: k => state[k],
    set(k, v) { const prev = state[k]; if (Object.is(prev, v)) return; state[k] = v; const s = subs.get(k); if (s) for (const fn of [...s]) fn(v, prev); },
    patch(o) { for (const k in o) T.store.set(k, o[k]); },
    update(k, fn) { T.store.set(k, fn(state[k])); },
    sub(k, fn) { let s = subs.get(k); if (!s) subs.set(k, s = new Set()); s.add(fn); return () => s.delete(fn); },
    snapshot: () => Object.assign({}, state),
  };
  T.i18n = { hasAny: id => !!zh[id], has: id => !!zh[id] };
  T.t = (id, params) => (zh[id] ? zh[id].text : id);
  return T;
}

/** Strip what the shell never forwards to a page; keep the order. */
export function pageFrames(frames, spec = SPEC) {
  const out = [];
  const pageTopic = t => !(PAGE_STRIPPED.has(t) || (spec.topics[t] && spec.topics[t].shell_only));
  for (const f of frames) {
    if (!f || typeof f !== 'object') continue;
    if (f.t === 'evt') { if (pageTopic(f.topic)) out.push(f); continue; }
    if (f.t === 'batch') {
      const items = (f.items || []).filter(i => !i || i.t !== 'evt' || pageTopic(i.topic));
      if (items.length) out.push({ ...f, items });
      continue;
    }
    if (f.t === 'hello' || f.t === 'bye' || f.t === 'fatal') out.push(f);
    // subscribed / pong / snapshot_res belong to the shell; the probe's own replies are replayed
    // through T.rt.query below.
  }
  return out;
}

const flush = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

// `known`: {topic: why} slots the probe's scope cannot fill (T-P2-33 STOPPED_UNAVAILABLE, sent by
// sidecar_probe.py): their gap and the page's resync for them are recorded as known, not failed.
export async function project({ frames, queries = [], catalogs, fixtures = null, known = {}, enumPaths = {}, baseline = {} }) {
  const index = fixtures ? { sigs: fixtures, values: new Map() } : fixtureIndex();
  const result = { exceptions: [], contract_diag: [], reconnects: [], queries: [], type_mismatch: [], enum_unknown: [], text_missing: [], known: [], info: {} };
  const onError = e => result.exceptions.push(String(e && e.stack || e).slice(0, 2000));
  process.on('uncaughtException', onError);
  process.on('unhandledRejection', onError);
  try {
    const T = makeT(catalogs);
    const clock = createFakeClock(1000);
    const sent = [];
    let deliver = null;
    const transport = {
      kind: 'guard',
      connect(onDelivery) { deliver = frame => onDelivery({ frame }); return Promise.resolve(); },
      invoke(name, payload) { sent.push({ name, payload }); return Promise.resolve({ id: sent.length }); },
      invokeHost() { return Promise.resolve(null); },
      reconnect() {},
    };
    const rt = createRt({ T, clock, ids: createIds({ conn: 'guard' }) });
    rt.connect(transport);
    await flush();
    const hello = frames.find(f => f.t === 'hello');
    if (!hello) { result.exceptions.push('no hello frame in the probe'); return result; }
    const page = pageFrames(frames);
    const diagSeen = new Set();
    const takeDiag = at => {
      for (const e of rt.diag()) {
        if (e.at !== at) continue;
        const key = JSON.stringify(e);
        if (diagSeen.has(key)) continue;
        diagSeen.add(key);
        const contract = CONTRACT_DIAG.has(e.kind) || CONTRACT_DIAG_PREFIX.some(p => String(e.kind).startsWith(p));
        const reconnect = String(e.kind).startsWith('resync');
        if (!contract && !reconnect) continue;
        if (e.topic && Object.prototype.hasOwnProperty.call(known, e.topic)) { result.known.push({ ...e, why: known[e.topic] }); continue; }
        (contract ? result.contract_diag : result.reconnects).push(e);
      }
    };
    deliver({ t: 'rt:reset', epoch: hello.epoch, reason: 'reconnect' });
    for (const f of page) {
      clock.jump(1);
      try { deliver(f); } catch (e) { onError(e); }
      await flush();
      takeDiag(clock.now());
    }
    // The read-only queries the probe made, answered with the sidecar's real replies.
    for (const q of queries) {
      clock.jump(1);
      const before = sent.length;
      // a page query, or a page command answered with res (settings.validate)
      const p = (SPEC.queries[q.cmd] ? rt.query(q.cmd, q.args || {}) : rt.call(q.cmd, q.args || {}))
        .then(() => 'resolved', e => `rejected:${e && e.code}`);
      await flush();
      const req = sent.slice(before).find(s => s.name === 'rt' && s.payload && s.payload.cmd === q.cmd);
      const record = { cmd: q.cmd, sidecar_ok: !!(q.reply && q.reply.ok) };
      if (!req) { record.outcome = `not_sent (${await p})`; record.problem = true; result.queries.push(record); continue; }
      const reply = { ...q.reply, epoch: hello.epoch, req_id: req.payload.req_id, owner: req.payload.owner };
      try { deliver(reply); } catch (e) { onError(e); }
      const outcome = await Promise.race([
        p,
        new Promise(r => setTimeout(() => r('pending'), 2000)),
      ]);
      record.outcome = outcome;
      if (outcome === 'pending' || (record.sidecar_ok && outcome !== 'resolved')) record.problem = true;
      if (!record.sidecar_ok && outcome.startsWith('rejected:')) record.error_text_id = (q.reply.error && q.reply.error.message_key) || null;
      result.queries.push(record);
      takeDiag(clock.now());
    }
    await flush(5);
    await new Promise(r => setTimeout(r, 50));
    // Type signatures of what the page received, against the UI's own fixtures.
    const real = new Map();
    for (const f of page) for (const item of evtItems(f)) if (item.data !== undefined) addSig(real, `topic:${item.topic}`, item.data);
    for (const q of queries) if (q.reply && q.reply.ok === true && q.reply.data !== undefined) addSig(real, `res:${q.cmd}`, q.reply.data);
    result.type_mismatch = typeMismatches(real, index.sigs);
    const coverage = enumCoverageFindings(realValues(page, queries), index.values, enumPaths, baseline);
    result.enum_unknown = [...enumFindings(page), ...coverage.findings];
    result.info.enum_known_untested = coverage.known;
    const texts = textFindings(page, queries, catalogs);
    result.text_missing = texts.findings;
    result.info.reasons_unmapped = texts.info;
    result.info.frames = frames.length;
    result.info.page_frames = page.length;
    result.info.topics = [...new Set(page.flatMap(evtItems).map(i => i.topic))].sort();
    result.info.compared_keys = [...real.keys()].filter(k => index.sigs.has(k)).sort();
    // review A SHOULD 3: fields the UI fixtures carry on a payload's first level that the real frames
    // never had (a removed field shows up here; info only, a stopped scope leaves optional ones out)
    result.info.fixture_paths_absent_in_real = [];
    for (const [key, sig] of real) {
      const fx = index.sigs.get(key);
      if (!fx) continue;
      for (const path of fx.keys()) if (/^\.[^.[\]]+$/.test(path) && !sig.has(path)) result.info.fixture_paths_absent_in_real.push(`${key} ${path}`);
    }
    result.info.uncompared_keys = [...real.keys()].filter(k => !index.sigs.has(k)).sort();
    result.info.enum_paths_checked = Object.values(enumPaths || {}).reduce((n, p) => n + Object.keys(p).length, 0);
    result.info.store_run = T.store.get('run');
    result.info.rt_state = rt.state;
    return result;
  } finally {
    await new Promise(r => setTimeout(r, 10));
    process.off('uncaughtException', onError);
    process.off('unhandledRejection', onError);
  }
}

export function failed(result) {
  return result.exceptions.length + result.contract_diag.length + result.reconnects.length
    + result.queries.filter(q => q.problem).length + result.type_mismatch.length + result.enum_unknown.length
    + result.text_missing.length;
}

function parseArgs(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i], v = () => { if (argv[i + 1] === undefined) throw new Error(`${a} needs a value`); return argv[++i]; };
    if (a === '--frames') o.frames = v();
    else if (a === '--probe') o.probe = v();
    else if (a === '--out') o.out = v();
    else if (a === '--baseline') o.baseline = v();
    else if (a === '--enum-coverage') o.coverage = v();
    else if (a === '--print-baseline') o.printBaseline = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (o.coverage) { if (!o.out && !o.printBaseline) throw new Error('--enum-coverage needs --out or --print-baseline'); return o; }
  if (!o.frames || !o.out) throw new Error('--frames and --out are required');
  return o;
}

// Guard item 1: `--enum-coverage <enum_paths.json> --out <json> [--baseline <file>]`; with
// `--print-baseline` it prints the current gaps as a new baseline document (to review and save as
// ui/tools/guard/enum_baseline.json; the guard never writes it).
function coverageMain(o) {
  const enumPaths = JSON.parse(readFileSync(o.coverage, 'utf8'));
  const topics = enumPaths.topics || enumPaths;
  const { values } = fixtureIndex();
  const baseline = loadBaseline(o.baseline === undefined ? ENUM_BASELINE : o.baseline);
  const result = staticEnumCoverage(topics, values, baseline);
  if (o.printBaseline) {
    process.stdout.write(JSON.stringify({
      _note: 'T-P6-05 UI guard: declared enum values of bridge_protocol (page topics and query replies) that ui/test/fixtures/scenarios '
        + 'did not carry when this file was written. Items 1 and 3 report them as known; any other untested value fails. '
        + 'Shrink it as fixtures gain these values; add to it only with a Claude ruling.',
      generated_utc: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), gaps: result.gaps }, null, 1) + '\n');
    return 0;
  }
  mkdirSync(dirname(resolve(o.out)), { recursive: true });
  writeFileSync(resolve(o.out), JSON.stringify({ new: result.new, stale: result.stale, gaps: result.gaps }, null, 1) + '\n');
  console.log(`[project_frames] enum coverage: ${result.new.length} new untested values, ${result.stale.length} stale baseline entries`);
  return result.new.length ? 1 : 0;
}

export async function main(argv = process.argv.slice(2)) {
  let o;
  try { o = parseArgs(argv); } catch (e) { console.error(`project_frames: ${e.message}`); return 2; }
  if (o.coverage) return coverageMain(o);
  const all = readFileSync(o.frames, 'utf8').split('\n').filter(l => l.trim()).map(l => JSON.parse(l));
  const probe = o.probe && existsSync(o.probe) ? JSON.parse(readFileSync(o.probe, 'utf8')) : {};
  // Only the session up to the quit request (sidecar_probe.py quit_frame_index): the shutdown is the
  // page closing, and its timing-dependent edges would make the guard flaky. The probe still
  // validates every frame, the shutdown ones included, against bridge_protocol.
  const cut = Number.isInteger(probe.quit_frame_index) ? probe.quit_frame_index : all.length;
  const frames = all.slice(0, cut);
  const catalogs = loadCatalogs(UI);
  const enumPaths = (probe.enum_paths && probe.enum_paths.topics) || {};
  const baseline = loadBaseline(o.baseline === undefined ? ENUM_BASELINE : o.baseline);
  const result = await project({ frames, queries: probe.queries || [], catalogs, known: probe.stopped_unavailable || {}, enumPaths, baseline });
  result.info.enum_uncovered_topics = (probe.enum_paths && probe.enum_paths.uncovered) || [];
  result.info.shutdown_frames_not_projected = all.length - frames.length;
  mkdirSync(dirname(resolve(o.out)), { recursive: true });
  writeFileSync(resolve(o.out), JSON.stringify(result, null, 1) + '\n');
  const n = failed(result);
  console.log(`[project_frames] ${n ? `FAIL (${n})` : 'ok'}: ${result.info.page_frames} page frames, ${result.queries.length} queries, `
    + `exceptions ${result.exceptions.length}, contract ${result.contract_diag.length}, types ${result.type_mismatch.length}, `
    + `enums ${result.enum_unknown.length}, texts ${result.text_missing.length}, reconnects ${result.reconnects.length}`);
  return n ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
