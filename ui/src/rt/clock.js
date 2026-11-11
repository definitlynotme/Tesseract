// T.clock (T-P4-02a; P4 architecture section 2.7, 2.8): the one time source for the rt
// client (stale checks, the 5 s unknown-op cache, the resync throttle) and, later, for page
// timers. Production uses performance.now() and the real timers; tests and fixture replay
// inject createFakeClock() and drive it with advance(ms).

const realNow = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());

export function createClock({ now = realNow } = {}) {
  return {
    kind: 'real',
    now,
    after(ms, fn) { const id = setTimeout(fn, ms); return () => clearTimeout(id); },
    every(ms, fn) { const id = setInterval(fn, ms); return () => clearInterval(id); },
  };
}

// Deterministic clock: timers fire only inside advance(), in due-time order (ties keep
// scheduling order). A timer scheduled by a firing timer runs in the same advance() when it
// falls due before the target time.
export function createFakeClock(start = 0) {
  let t = start, seq = 0;
  const timers = new Map(); // id -> {due, fn, every}
  const clock = {
    kind: 'fake',
    now: () => t,
    after(ms, fn) {
      const id = ++seq;
      timers.set(id, { due: t + Math.max(0, ms), fn, every: 0, order: id });
      return () => timers.delete(id);
    },
    every(ms, fn) {
      const id = ++seq, step = Math.max(1, ms);
      timers.set(id, { due: t + step, fn, every: step, order: id });
      return () => timers.delete(id);
    },
    advance(ms) {
      const end = t + Math.max(0, ms);
      for (;;) {
        let next = null, nextId = 0;
        for (const [id, tm] of timers) {
          if (tm.due > end) continue;
          if (!next || tm.due < next.due || (tm.due === next.due && tm.order < next.order)) { next = tm; nextId = id; }
        }
        if (!next) break;
        t = next.due;
        if (next.every) { next.due += next.every; next.order = ++seq; } else timers.delete(nextId);
        next.fn();
      }
      t = end;
      return t;
    },
    // Jump the time without firing timers (a suspended machine: no timer ran meanwhile).
    jump(ms) { t += Math.max(0, ms); for (const tm of timers.values()) if (tm.due < t) tm.due = t; return t; },
    pending: () => timers.size,
  };
  return clock;
}
