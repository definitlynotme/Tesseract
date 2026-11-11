// Stale judgement on the receiving side (T-P4-02a; P4 architecture section 2.8, PROTOCOL
// sections 4 and 11). Time comes from T.clock. Change-driven topics never go stale for being
// quiet; only these reasons count:
//   service.state  run in {starting, running, stopping} and > 3 s without a frame;
//                  freshness.stale = true; no snapshot yet in this epoch
//   traffic.tick   service.state.core.running = true and > 3 s without a sample; no aging
//                  after the Core stops
//   other states   no snapshot yet in this epoch; gap; pages not complete; base_rev mismatch;
//                  a payload runtime generation (top-level runtime_generation,
//                  service.collect.generation) that differs from intent.runtime_generation;
//                  exit.identity: a Core generation (current.generation, idle / 0 skipped)
//                  that differs from service.state.core.core_generation
//   logs.batch, jobs  never stale
// After a suspend (two checks more than 5 s apart) the time rules wait a 3 s grace first,
// like the shell's watchdog restarting its timers on wake.
// Result: store `stale` = {[topic or topic/key]: bool}, event rt:stale {topic, stale}.

export const QUIET_MS = 3000;
export const SLEEP_GAP_MS = 5000;
export const GRACE_MS = 3000;
const NEVER = new Set(['logs.batch', 'jobs']);
const TIMED_RUN = new Set(['starting', 'running', 'stopping']);

export function staleKey(topic, key) { return key ? `${topic}/${key}` : topic; }

export function createStale({ clock, store, emit, getRun = () => null, getCoreRunning = () => false, getGeneration = () => null, getCoreGeneration = () => null }) {
  const reasons = new Map(); // key -> Set(reason)
  const lastFrame = new Map(); // key -> clock time of the latest frame
  const snapshot = new Set(); // keys that got a full value in this epoch
  const generationOf = new Map(); // key -> payload runtime generation
  const coreGenerationOf = new Map(); // key -> payload Core generation
  let lastCheck = null, graceUntil = -Infinity;
  const result = {};

  const r = key => { let s = reasons.get(key); if (!s) reasons.set(key, s = new Set()); return s; };

  function publish(key, value) {
    if (!!result[key] === value) return;
    result[key] = value;
    if (store) store.set('stale', { ...result });
    if (emit) emit('rt:stale', { topic: key, stale: value });
  }

  function timed(key) {
    const now = clock.now();
    if (now < graceUntil) return false;
    const last = lastFrame.get(key);
    const quiet = last == null ? false : now - last > QUIET_MS;
    if (key === 'service.state') return TIMED_RUN.has(getRun()) && quiet;
    if (key === 'traffic.tick') return !!getCoreRunning() && quiet;
    return false;
  }

  function generationMismatch(key) {
    const g = generationOf.get(key), cur = getGeneration();
    if (g != null && cur != null && g !== cur) return true;
    const cg = coreGenerationOf.get(key), ccur = getCoreGeneration();
    return cg != null && ccur != null && cg !== ccur;
  }

  function evaluate(key) {
    if (NEVER.has(key.split('/')[0])) return;
    const s = reasons.get(key);
    const value = !snapshot.has(key) || !!(s && s.size) || timed(key) || generationMismatch(key);
    publish(key, value);
  }

  const api = {
    // A frame (any kind) for this key arrived. `full` = it carried a complete value
    // (snapshot, edge, an installed page group or a merged patch).
    frame(topic, key, { full = false, generation = null, coreGeneration = null } = {}) {
      const k = staleKey(topic, key);
      lastFrame.set(k, clock.now());
      if (full) snapshot.add(k);
      if (generation != null) generationOf.set(k, generation); else generationOf.delete(k);
      if (coreGeneration != null) coreGenerationOf.set(k, coreGeneration); else coreGenerationOf.delete(k);
      evaluate(k);
    },
    mark(topic, key, reason, on) {
      const k = staleKey(topic, key);
      if (on) r(k).add(reason); else if (reasons.has(k)) reasons.get(k).delete(reason);
      evaluate(k);
    },
    // Topics the page expects in this epoch (hello caps): stale until their first value.
    expect(topics) { for (const t of topics) if (!NEVER.has(t)) evaluate(staleKey(t)); },
    // New epoch: everything is stale until its first snapshot.
    resetEpoch() {
      reasons.clear(); lastFrame.clear(); snapshot.clear(); generationOf.clear(); coreGenerationOf.clear();
      for (const k of Object.keys(result)) publish(k, true);
    },
    // Periodic: time rules, plus the suspend grace.
    check() {
      const now = clock.now();
      if (lastCheck != null && now - lastCheck > SLEEP_GAP_MS) graceUntil = now + GRACE_MS;
      lastCheck = now;
      for (const k of new Set([...Object.keys(result), ...lastFrame.keys()])) evaluate(k);
    },
    reevaluate() { for (const k of Object.keys(result)) evaluate(k); },
    isStale: (topic, key) => !!result[staleKey(topic, key)],
    snapshot: () => ({ ...result }),
  };
  return api;
}
