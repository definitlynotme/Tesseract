// Topic cache (T-P4-02a; P4 architecture section 2.5, PROTOCOL sections 4 and 11).
// Key (topic, key?) inside the current epoch; a new epoch clears everything.
//   snapshot  replaces the value; a paged snapshot is buffered and installed at once
//   patch     only on patchable topics, merged only when cached rev == base_rev; otherwise
//             the last value stays shown, the entry goes stale, increments pause and a
//             refetch is requested (never last-wins)
//   edge      a complete DTO: replaces the value; the edge seq is checked for continuity
//   gap       state topics go stale and are refetched; logs.batch gaps go to the log buffer;
//             event-only topics report rt:gap
// Replayed frames after a same-epoch reconnect are dropped by rev (jobs by seq).
import { applyPatch, createPageBuffer } from './paging.js';
import { TIER_IDS } from './project.js';

const SEP = '\u0000';
const cacheKey = (topic, key) => topic + SEP + (key || '');
const SHELL_STRIPPED = new Set(['ui.language', 'alerts.desktop']);
const MAX_QUEUED_PATCHES = 64;

// The runtime generation a payload belongs to (compared with intent.runtime_generation).
// exit.identity is not one of them: IdentityResult.generation is the Core generation
// (bridge_exit.py), see payloadCoreGeneration.
export function payloadGeneration(topic, data) {
  if (!data || typeof data !== 'object') return null;
  if (Number.isInteger(data.runtime_generation)) return data.runtime_generation;
  if (topic === 'service.collect' && Number.isInteger(data.generation)) return data.generation;
  return null;
}

// The Core generation a payload belongs to (compared with service.state.core.core_generation):
// exit.identity.current.generation, except the idle result (generation 0, nothing measured).
export function payloadCoreGeneration(topic, data) {
  if (topic !== 'exit.identity' || !data || typeof data !== 'object') return null;
  const c = data.current;
  if (!c || typeof c !== 'object' || c.status === 'idle' || !Number.isInteger(c.generation) || c.generation === 0) return null;
  return c.generation;
}

export function createTopics({ spec, stale, project, emit, diag = () => {}, onJob = () => {}, requestResync = () => {}, onRecovered = () => {} }) {
  const topics = spec.topics;
  const cache = new Map();
  const subs = new Map();
  let jobsSeq = 0;

  const entryOf = (topic, key) => {
    const k = cacheKey(topic, key);
    let e = cache.get(k);
    if (!e) cache.set(k, e = { topic, key: key || null, rev: 0, seq: null, data: undefined, rows: null, awaiting: false, awaitRev: 0, pager: null, queued: [], listView: null });
    return e;
  };
  const isList = topic => !!(topics[topic] && (topics[topic].patchable || topics[topic].keyed));
  const isState = topic => !!(topics[topic] && topics[topic].kinds.includes('snapshot'));

  function view(e) {
    if (e.rows) { if (!e.listView) e.listView = { items: [...e.rows.values()] }; return e.listView; }
    return e.data;
  }

  function notify(e, meta) {
    const set = subs.get(cacheKey(e.topic, e.key));
    if (!set) return;
    const v = view(e);
    for (const fn of [...set]) { try { fn(v, meta); } catch (err) { setTimeout(() => { throw err; }); } }
  }

  function install(e, value, f, instant) {
    if (isList(e.topic)) { e.rows = new Map((value.items || []).map(r => [r.id, r])); e.listView = null; e.data = undefined; }
    else e.data = value;
    e.rev = f.rev;
    const recovered = e.awaiting;
    e.awaiting = false;
    e.awaitRev = 0;
    for (const reason of ['gap', 'base_rev', 'paging']) stale.mark(e.topic, e.key, reason, false);
    const full = e.rows ? null : e.data;
    stale.frame(e.topic, e.key, { full: true, generation: payloadGeneration(e.topic, full), coreGeneration: payloadCoreGeneration(e.topic, full) });
    if (recovered) onRecovered(e.topic, e.key);
    if (e.topic === 'service.state') {
      stale.mark('service.state', null, 'freshness', !!(value.freshness && value.freshness.stale));
    }
    if (!e.key) project.apply(e.topic, value, { kind: f.kind, instant });
    if (e.topic === 'service.state') stale.reevaluate();
    notify(e, { topic: e.topic, key: e.key, kind: f.kind, rev: f.rev, instant });
  }

  // `rev`: the newest rev known when the mismatch was seen (the patch or gap frame's own rev).
  // A replacement must be at least that new.
  function mismatch(e, reason, rev = e.rev) {
    e.awaiting = true;
    e.awaitRev = Math.max(e.awaitRev, e.rev, Number.isInteger(rev) ? rev : 0);
    stale.mark(e.topic, e.key, reason, true);
    diag({ kind: 'topic_' + reason, topic: e.topic, key: e.key, rev: e.rev });
    requestResync(e.topic, e.key, reason);
  }

  function patch(e, f, instant) {
    if (e.awaiting || !e.rows) return mismatch(e, 'base_rev', f.rev);
    const r = applyPatch({ rev: e.rev, items: [...e.rows.values()] }, { base_rev: f.base_rev, rev: f.rev, ...f.data });
    if (r.needsSnapshot) return mismatch(e, 'base_rev', f.rev);
    install(e, { items: r.merged.items }, f, instant);
    return true;
  }

  function receive(f, { instant = false } = {}) {
    const topic = f.topic, meta = topics[topic];
    if (!meta) { diag({ kind: 'unknown_topic', topic: String(topic) }); return 'dropped'; }
    if (meta.shell_only || SHELL_STRIPPED.has(topic)) { diag({ kind: 'shell_topic', topic }); return 'dropped'; }
    if (!meta.kinds.includes(f.kind)) { diag({ kind: 'bad_kind', topic, k: f.kind }); return 'dropped'; }
    const key = f.key == null ? null : f.key;
    if (meta.keyed ? !TIER_IDS.includes(key) : key !== null) { diag({ kind: 'bad_key', topic, key }); return 'dropped'; }
    if (!Number.isInteger(f.rev) || f.rev < 1 || !f.data || typeof f.data !== 'object') { diag({ kind: 'bad_evt', topic }); return 'dropped'; }

    if (topic === 'jobs') {
      if (!Number.isInteger(f.seq) || f.seq <= jobsSeq) return 'dup';
      if (jobsSeq && f.seq !== jobsSeq + 1) diag({ kind: 'jobs_sequence_gap', from: jobsSeq, to: f.seq });
      jobsSeq = f.seq;
      onJob(f.data, { instant });
      return 'applied';
    }

    const e = entryOf(topic, key);
    // Same-epoch replay and duplicates: drop by rev. An entry waiting for a replacement takes
    // a complete value (snapshot / edge) only when it is at least as new as the newest rev
    // known at the mismatch: an older replayed snapshot would clear stale over known-old data.
    if (e.awaiting && f.kind !== 'patch' && f.kind !== 'gap' && topic !== 'logs.batch') {
      if (f.rev < e.awaitRev) { diag({ kind: 'replacement_too_old', topic, key, rev: f.rev, need: e.awaitRev }); return 'dup'; }
    } else if (f.rev <= e.rev) return 'dup';

    if (topic === 'logs.batch') {
      e.rev = f.rev;
      if (f.kind === 'edge' && e.seq != null && f.seq !== e.seq + 1) diag({ kind: 'logs_seq_jump', from: e.seq, to: f.seq });
      e.seq = f.seq;
      emit('rt:logs', { kind: f.kind, seq: f.seq, data: f.data, instant });
      return 'applied';
    }

    if (f.kind === 'gap') {
      e.rev = f.rev; e.seq = f.seq;
      if (isState(topic)) { mismatch(e, 'gap'); }
      else { diag({ kind: 'topic_gap', topic, key }); emit('rt:gap', { topic, key, data: f.data, instant }); }
      return 'applied';
    }

    if (f.kind === 'snapshot' && f.page) {
      if (!e.pager) e.pager = createPageBuffer();
      const r = e.pager.add(f);
      if (r.status === 'buffering') { stale.mark(topic, key, 'paging', true); return 'buffering'; }
      if (r.status === 'broken') { e.queued = []; mismatch(e, 'paging'); diag({ kind: 'page_broken', topic, key, reason: r.reason }); return 'dropped'; }
      install(e, { items: r.items }, { ...f, rev: r.rev }, instant);
      const queued = e.queued; e.queued = [];
      for (const p of queued) if (p.rev > e.rev) patch(e, p, false);
      return 'applied';
    }

    if (f.kind === 'patch') {
      if (e.pager && e.pager.active) {
        if (e.queued.length < MAX_QUEUED_PATCHES) e.queued.push(f); else { e.queued = []; e.pager.clear(); mismatch(e, 'paging'); }
        return 'buffering';
      }
      return patch(e, f, instant) ? 'applied' : 'stale';
    }

    if (f.kind === 'edge' && Number.isInteger(f.seq)) {
      if (e.seq != null && f.seq !== e.seq + 1) diag({ kind: 'edge_seq_jump', topic, from: e.seq, to: f.seq });
      e.seq = f.seq;
    }
    if (e.pager && e.pager.active) { e.pager.clear(); e.queued = []; }
    install(e, f.data, f, instant);
    return 'applied';
  }

  return {
    receive,
    reset() { cache.clear(); jobsSeq = 0; },
    get(topic, key) { const e = cache.get(cacheKey(topic, key)); return e ? view(e) : undefined; },
    rev(topic, key) { const e = cache.get(cacheKey(topic, key)); return e ? e.rev : 0; },
    sub(topic, key, fn) {
      const k = cacheKey(topic, key);
      let set = subs.get(k); if (!set) subs.set(k, set = new Set());
      set.add(fn);
      return () => set.delete(fn);
    },
    jobsSeq: () => jobsSeq,
  };
}
