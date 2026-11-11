// T.rt: session state machine, epoch, hello, rt:reset, host:* dispatch (T-P4-02a; P4
// architecture sections 2.1-2.5, PROTOCOL sections 2-4, shell/README P3 / T-P3-05 / T-P3-06).
//
//   idle --connect--> connecting --first hello--> live
//   live --rt:reset{runtime_gone | new epoch}--> resetting --new hello--> live
//   any --order fault / channel stuck / resync fallback--> reconnecting --replay--> live
//
// rt:reset comes in two kinds (criticism B9):
//   generation change (reason runtime_gone, or an epoch other than the current one):
//     in-flight promises reject with runtime_gone, the topic cache, page buffers and
//     sequence watermarks are cleared, run goes 'unknown', every state topic is stale until
//     its first snapshot; event rt:reset {generation_changed: true}
//   same-epoch reconnect (reason reconnect, same epoch): nothing is cleared or rejected;
//     replayed frames are de-duplicated by (topic, key, rev) and jobs by seq;
//     event rt:reset {generation_changed: false}
// Frames with an epoch other than the current one are dropped (diagnostics); host:* frames
// belong to the shell and are never epoch-filtered.
// After each connect or rt:reset the first frame of every topic is `instant` (no edge
// notices, no light-up); a jobs frame is instant unless it belongs to one of this page's own
// calls. When the shell announces rt:replayed (host:caps.events, T-P4-C3) every frame before
// rt:replayed is instant instead, except the jobs frames of this page's own calls (T-P4-H).
import SPEC from './spec.gen.json' with { type: 'json' };
import { createCalls } from './calls.js';
import { createClock } from './clock.js';
import { RtError, errorText, reasonText } from './errors.js';
import { createFeatures } from './features.js';
import { createIds } from './ids.js';
import { createProjector } from './project.js';
import { createStale } from './stale.js';
import { createTopics } from './topics.js';

export const RESYNC_FALLBACK_MS = 30000;
// The reconnect fallback replays every topic, so requests from several topics inside this
// window share one reconnect.
export const RECONNECT_MIN_GAP_MS = 2000;
// An rt_resync in flight for a topic is not repeated until its replacement snapshot is
// installed, or until this long has passed without one.
export const RESYNC_INFLIGHT_MS = 10000;
export const STALE_CHECK_MS = 500;
// T.rt.requestResync: one page request per slot inside this window (the shell's rate limit)
export const PAGE_RESYNC_GAP_MS = 1000;
// T.rt.invokeHost: the shell invokes a page may make (PROTOCOL section 13; the same ten names
// as shellc's HOST_INVOKES, rt_h2.test keeps the two equal). Any other name is refused here
// too (diag host_unknown, RtError not_allowed), so a page cannot go around shellc's list by
// calling T.rt.invokeHost directly (T-P4-H2 fix round 1).
export const HOST_INVOKE_NAMES = Object.freeze(['rt', 'rt_connect', 'rt_resync', 'host_notify_ready', 'host_notify_rendered',
  'host_confirm_quit', 'host_set_page', 'host_force_restart', 'host_ready', 'host_window']);
const SIDECAR_FRAMES = new Set(['hello', 'evt', 'batch', 'ack', 'res', 'error', 'bye', 'fatal', 'subscribed', 'pong']);

export function createRt({ T, clock = createClock(), spec = SPEC, ids = createIds(), enabledFeatures = [] } = {}) {
  if (!T || !T.store || typeof T.emit !== 'function') throw new Error('createRt: T.store and T.emit are required');
  const S = T.store;
  const emit = (name, payload) => T.emit(name, payload);
  let state = 'idle', epoch = null, transport = null, hello = null, caps = null, hostCaps = null;
  let seen = new Set(), replayMarker = false, replayDone = false, genLive = false;
  const lastResync = new Map();
  const resyncInflight = new Map(); // topic/key -> clock time of the rt_resync call
  const logGaps = new Set(); // epoch/from_seq/to_seq of rt:log-gap frames already forwarded
  let lastReconnect = null, reconnectTimer = null;
  let stopTimer = null;

  const calls = createCalls({ spec, ids, clock, getTransport: () => transport, getEpoch: () => epoch, isLive: () => state !== 'idle' });
  const diag = entry => calls.note(entry);
  const projector = createProjector({
    store: S, emit, inflightArgs: cmd => calls.inflightArgs(cmd),
    hasTopic: t => !!(caps && Array.isArray(caps.topics) && caps.topics.includes(t)),
  });
  const stale = createStale({
    clock, store: S, emit, getRun: () => projector.run(), getCoreRunning: () => projector.coreRunning(),
    getGeneration: () => { const s = topics.get('service.state'); return s && s.intent && Number.isInteger(s.intent.runtime_generation) ? s.intent.runtime_generation : null; },
    getCoreGeneration: () => { const s = topics.get('service.state'); return s && s.core && Number.isInteger(s.core.core_generation) ? s.core.core_generation : null; },
  });
  // A gap in tier.transition may have lost a level edge: the projector reconciles the next
  // tiers.summary by level (T-P4-H fix round 1, review S6). Everything else passes through.
  const topicsEmit = (name, payload) => {
    if (name === 'rt:gap' && payload && payload.topic === 'tier.transition') projector.transitionGap();
    emit(name, payload);
  };
  const topics = createTopics({
    spec, stale, project: projector, emit: topicsEmit, diag,
    // Jobs are history plus live edges: a frame of one of this page's own calls is live; any
    // other job frame (replayed history, a job of an earlier page load, another page's job) is
    // instant. When the shell marks the end of its replay with rt:replayed, every other job
    // frame before it is instant and after it live; a call of this page stays live inside that
    // window too (a new epoch's seed window can be up to SEED_GRACE = 15 s long, and a job the
    // page starts in it is no replay; 5th-wave ruling C3-2). calls.owns() only knows the calls
    // of the current epoch (a generation change ends them all), so an older call is not ours.
    onJob: d => { const own = calls.owns(d); const instant = replayMarker ? !replayDone && !own : !own; calls.onJob(d); projector.job(d, { instant }); },
    requestResync: (topic, key, reason) => resync(topic, key, reason),
    onRecovered: (topic, key) => resyncInflight.delete(topic + '\u0000' + (key || '')),
  });
  const features = createFeatures({ enabled: enabledFeatures, caps: () => caps, diag });

  function setState(next) {
    if (state === next) return;
    const prev = state;
    state = next;
    emit('rt:conn', { state: next, prev });
  }

  function instantFor(topic, key) {
    if (replayMarker) return !replayDone;
    const k = topic + '\u0000' + (key || '');
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }

  function freshCycle() { seen = new Set(); replayDone = false; }

  // -> whether a generation the page had seen (a hello since the last change) ended. A second
  // runtime_gone before the next hello (the shell's gone() and then its reset() for the new
  // epoch) ends nothing new, so pages get generation_changed:true once per lost generation.
  function generationChange(newEpoch) {
    const ended = epoch !== null && genLive;
    epoch = newEpoch;
    genLive = false;
    calls.rejectAll('runtime_gone');
    topics.reset();
    stale.resetEpoch();
    projector.reset();
    lastResync.clear();
    resyncInflight.clear();
    pageResync.clear();
    logGaps.clear();
    return ended;
  }

  function onReset(f) {
    freshCycle();
    const changed = f.reason === 'runtime_gone' || f.epoch !== epoch;
    if (changed) {
      const ended = generationChange(typeof f.epoch === 'string' ? f.epoch : null);
      if (state !== 'connecting' || f.reason === 'runtime_gone') setState('resetting');
      emit('rt:reset', { reason: f.reason, epoch: f.epoch, generation_changed: ended });
    } else {
      emit('rt:reset', { reason: f.reason, epoch: f.epoch, generation_changed: false });
    }
  }

  function onHello(f) {
    if (epoch !== null && f.epoch !== epoch) {
      // A new epoch without an rt:reset in front of it: same treatment as a generation change.
      freshCycle();
      generationChange(f.epoch);
      emit('rt:reset', { reason: 'runtime_gone', epoch: f.epoch, generation_changed: true });
    }
    epoch = f.epoch;
    genLive = true;
    hello = f;
    caps = f.caps && typeof f.caps === 'object' ? f.caps : { commands: [], topics: [], features: [] };
    S.patch({ caps, recovery: f.network_recovery || null, appVersion: f.app_version || null });
    if (typeof f.lang === 'string') S.set('lang', f.lang);
    projector.hello(f);
    stale.expect((caps.topics || []).filter(t => spec.topics[t] && spec.topics[t].kinds.includes('snapshot') && !spec.topics[t].shell_only && !spec.topics[t].keyed && t !== 'ui.language'));
    setState('live');
    emit('rt:caps', { caps, instant: true });
  }

  function onEvt(f) {
    if (f.epoch !== epoch) { diag({ kind: 'epoch_mismatch', t: 'evt', topic: f.topic }); return; }
    topics.receive(f, { instant: instantFor(f.topic, f.key) });
  }

  function onFrame(frame) {
    if (!frame || typeof frame !== 'object' || typeof frame.t !== 'string') { diag({ kind: 'bad_frame' }); return; }
    const t = frame.t;
    if (t === 'rt:reset') return onReset(frame);
    if (t.startsWith('host:')) {
      if (t === 'host:caps') {
        hostCaps = { invokes: Array.isArray(frame.invokes) ? frame.invokes.slice() : [], events: Array.isArray(frame.events) ? frame.events.slice() : [] };
        replayMarker = hostCaps.events.includes('rt:replayed');
        S.set('hostCaps', hostCaps);
      } else if (t === 'host:language' && typeof frame.language === 'string') {
        S.set('lang', frame.language);
      }
      emit(t, frame);
      return;
    }
    if (t === 'rt:replayed') { replayDone = true; emit('rt:replayed', { epoch: frame.epoch }); return; }
    if (t === 'rt:log-gap') {
      if (frame.epoch !== epoch) { diag({ kind: 'epoch_mismatch', t }); return; }
      // The shell rebuilds the same log-gap on every replay (transport.rs replay): forward
      // each (epoch, from_seq, to_seq) once, so a same-epoch reconnect adds no second marker.
      const d = frame.data || {};
      const k = `${epoch}/${d.from_seq}/${d.to_seq}`;
      if (logGaps.has(k)) { diag({ kind: 'log_gap_dup' }); return; }
      logGaps.add(k);
      emit('rt:log-gap', d);
      return;
    }
    if (t === 'rt:stale') {
      // The shell's own word that a slot's cache is stale (PROTOCOL section 13 rt:stale, T-P3-H2;
      // 5th-wave ruling 31b-5). Forwarded as the page event rt:slot-stale with the frame's
      // payload as it is ({epoch, topic, key, reason}; key null for a topic that is not keyed;
      // reason resync or exhausted, exhausted forwarded too). The page-internal rt:stale
      // (stale.js: {topic, stale}) is another event. 8th-wave ruling, T-P4-H2 item 1.
      if (frame.epoch !== epoch) { diag({ kind: 'epoch_mismatch', t }); return; }
      emit('rt:slot-stale', { epoch: frame.epoch, topic: frame.topic, key: frame.key == null ? null : frame.key, reason: frame.reason });
      return;
    }
    if (!SIDECAR_FRAMES.has(t)) { diag({ kind: 'unknown_frame', t }); return; }
    if (t === 'hello') return onHello(frame);
    if (frame.epoch !== epoch) { diag({ kind: 'epoch_mismatch', t }); return; }
    switch (t) {
      case 'evt': return onEvt(frame);
      case 'batch':
        for (const item of Array.isArray(frame.items) ? frame.items : []) {
          if (item && item.t === 'evt') onEvt(item); else diag({ kind: 'bad_batch_item' });
        }
        return;
      case 'ack': return calls.onAck(frame);
      case 'res': return calls.onRes(frame);
      case 'error': return calls.onError(frame);
      case 'bye': emit('rt:bye', { exit_code: frame.exit_code, network_recovery: frame.network_recovery || null }); return;
      case 'fatal': diag({ kind: 'fatal', code: frame.error && frame.error.code || null }); emit('rt:fatal', { where: frame.where, error: frame.error || null }); return;
      default: return; // subscribed, pong
    }
  }

  function onDelivery(d) {
    try { onFrame(d && d.frame); }
    catch (err) { diag({ kind: 'frame_threw', message: String(err && err.message || err) }); setTimeout(() => { throw err; }); }
  }

  // Refetch one topic: the shell's rt_resync when it has it (T-P4-C3), once per topic until
  // the replacement snapshot is installed (or RESYNC_INFLIGHT_MS passed); otherwise reconnect
  // for a full replay, at most once per topic per 30 s and at most once per 2 s overall.
  function resync(topic, key, reason) {
    const k = topic + '\u0000' + (key || '');
    const now = clock.now();
    if (hostHas('rt_resync')) {
      const since = resyncInflight.get(k);
      if (since != null && now - since < RESYNC_INFLIGHT_MS) { diag({ kind: 'resync_inflight', topic }); return 'inflight'; }
      resyncInflight.set(k, now);
      const args = key ? { topic, key } : { topic };
      transport.invokeHost('rt_resync', args).then(() => diag({ kind: 'resync', topic, code: 'ok' }),
        e => { if (resyncInflight.get(k) === now) resyncInflight.delete(k); diag({ kind: 'resync', topic, code: (e && e.code) || 'internal' }); });
      return 'rt_resync';
    }
    const last = lastResync.get(k);
    if (last != null && now - last < RESYNC_FALLBACK_MS) { diag({ kind: 'resync_throttled', topic }); return 'throttled'; }
    lastResync.set(k, now);
    if (!transport || typeof transport.reconnect !== 'function') return 'none';
    diag({ kind: 'resync_reconnect', topic, reason });
    if (reconnectTimer) return 'merged';
    const wait = lastReconnect == null ? 0 : lastReconnect + RECONNECT_MIN_GAP_MS - now;
    if (wait > 0) {
      reconnectTimer = clock.after(wait, () => { reconnectTimer = null; lastReconnect = clock.now(); if (transport) transport.reconnect('resync'); });
      return 'merged';
    }
    lastReconnect = now;
    transport.reconnect('resync');
    return 'reconnect';
  }

  function hostHas(name) { return !!(hostCaps && hostCaps.invokes.includes(name)); }

  // A page asks the shell to deliver one slot's complete snapshot again (T.rt.requestResync; a
  // write-after-read, T-P4-05b): rt_resync when the shell has it, else nothing (the data is not
  // known to be broken, so no reconnect fallback). A slot whose own mismatch already has an
  // rt_resync in flight is not asked twice, and a page request is not repeated inside
  // PAGE_RESYNC_GAP_MS (the shell's 1 s rate limit, PROTOCOL section 13 rt_resync). Never
  // rejects: -> Promise of the shell's answer ({source}) or null; the outcome is in diag().
  const pageResync = new Map(); // topic/key -> clock time of the page's last rt_resync
  function requestResync(topic, key) {
    if (typeof topic !== 'string' || !topic) return Promise.resolve(null);
    const k = topic + '\u0000' + (key || '');
    const now = clock.now();
    if (!transport || !hostHas('rt_resync')) { diag({ kind: 'resync_unsupported', topic }); return Promise.resolve(null); }
    const since = resyncInflight.get(k);
    if (since != null && now - since < RESYNC_INFLIGHT_MS) { diag({ kind: 'resync_inflight', topic }); return Promise.resolve(null); }
    const last = pageResync.get(k);
    if (last != null && now - last < PAGE_RESYNC_GAP_MS) { diag({ kind: 'resync_throttled', topic }); return Promise.resolve(null); }
    pageResync.set(k, now);
    let p;
    try { p = Promise.resolve(transport.invokeHost('rt_resync', key ? { topic, key } : { topic })); } catch (e) { p = Promise.reject(e); }
    return p.then(r => { diag({ kind: 'resync', topic, code: 'ok', by: 'page' }); return r == null ? null : r; },
      e => { diag({ kind: 'resync', topic, code: (e && e.code) || 'internal', by: 'page' }); return null; });
  }

  projector.seed();

  const rt = {
    get state() { return state; },
    get epoch() { return epoch; },
    get transport() { return transport ? transport.kind : null; },
    connect(t) {
      if (!t || typeof t.connect !== 'function') throw new Error('T.rt.connect: a transport is required');
      transport = t;
      setState('connecting');
      freshCycle();
      if (!stopTimer) stopTimer = clock.every(STALE_CHECK_MS, () => stale.check());
      return t.connect(onDelivery, {
        onReconnect: reason => {
          freshCycle(); setState('reconnecting'); diag({ kind: 'reconnect', reason });
          // A transport fault lost a delivery: waiters answered by ack / res / error end now
          // (calls.endUnanswered). The resync fallback drops a healthy Channel and keeps them.
          if (reason !== 'resync') calls.endUnanswered(reason);
        },
        onDiag: diag,
      });
    },
    can: cmd => !!(caps && Array.isArray(caps.commands) && caps.commands.includes(cmd)),
    has: topic => !!(caps && Array.isArray(caps.topics) && caps.topics.includes(topic)),
    hostHas,
    requestResync,
    feature: name => features.feature(name),
    call: (cmd, args, opts) => calls.call(cmd, args, opts),
    query: (cmd, args, opts) => calls.query(cmd, args, opts),
    cancel: target => calls.cancel(target),
    owner: (label, opts) => calls.owner(label, opts),
    invokeHost(name, args) {
      if (!HOST_INVOKE_NAMES.includes(name)) { diag({ kind: 'host_unknown', name: String(name) }); return Promise.reject(new RtError('not_allowed')); }
      if (!transport) return Promise.reject(new RtError('runtime_unavailable'));
      return transport.invokeHost(name, args);
    },
    topic(name, key) {
      return { get: () => topics.get(name, key), sub: fn => topics.sub(name, key, fn) };
    },
    errorText: err => errorText(T, err),
    reasonText: (ns, code, params) => reasonText(T, ns, code, params),
    diag: () => calls.diag(),
    get hello() { return hello ? { app_version: hello.app_version, lang: hello.lang, caps } : null; },
    RtError,
    reqId: () => ids.next(),
  };
  return rt;
}

// Install T.rt and T.clock on the page (boot/rt-app.js, boot/rt-design.js).
export function installRt(T, opts = {}) {
  const clock = opts.clock || createClock();
  T.clock = clock;
  T.rt = createRt({ ...opts, T, clock });
  return T.rt;
}
