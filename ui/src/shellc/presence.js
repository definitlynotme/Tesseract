// Window presence (T-P4-06; P4 architecture section 2.9, mapping_core R6, G57).
//
// store `presence` = {visible, host, doc}:
//   host     the shell's last host:visibility{visible} (null until the shell says anything;
//            the shell replays the latest value on every connect)
//   doc      !document.hidden (document.visibilityState)
//   visible  host !== false && doc: the window can be seen
// While it is false the page pauses its scope timers (core.js T.scopes), chart redraws, toast
// countdowns and the uptime walk; on the way back the shell layer resumes them, and each paused
// interval runs once first so the data catches up (core.js).
//
// Ports: store (T.store), on (T.on), doc ({hidden, visibilityState, addEventListener}) or null.
//
// Reduced motion (G52, criticism D4): createReducedMotion(win).reduced = the shell's injected
// window.__TESSERACT_MOTION__.reduced (T-P4-C3: TESSERACT_REDUCED_MOTION, read once, frozen) OR
// matchMedia('(prefers-reduced-motion: reduce)').matches, which is followed live. core.js builds
// T.motion.reduced on it.

export function createReducedMotion(win) {
  const inj = win && win.__TESSERACT_MOTION__;
  const injected = !!(inj && typeof inj === 'object' && inj.reduced === true);
  let mq = null;
  try { mq = win && typeof win.matchMedia === 'function' ? win.matchMedia('(prefers-reduced-motion: reduce)') : null; } catch (e) { mq = null; }
  let media = !!(mq && mq.matches);
  const listeners = new Set();
  const value = () => injected || media;
  if (mq) {
    const change = e => {
      const before = value();
      media = !!(e && e.matches);
      if (value() !== before) for (const fn of Array.from(listeners)) fn(value());
    };
    if (typeof mq.addEventListener === 'function') mq.addEventListener('change', change);
    else if (typeof mq.addListener === 'function') mq.addListener(change);
  }
  return {
    get reduced() { return value(); },
    injected,
    get media() { return media; },
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}

export function createPresence({ store, on, doc = null }) {
  let host = null;
  const docShown = () => !(doc && (doc.hidden === true || doc.visibilityState === 'hidden'));
  const listeners = new Set();
  let last = null;

  function compute() {
    const d = docShown();
    const next = { visible: host !== false && d, host, doc: d };
    if (last && last.visible === next.visible && last.host === next.host && last.doc === next.doc) return;
    const prev = last;
    last = next;
    store.set('presence', next);
    if (!prev || prev.visible !== next.visible) for (const fn of Array.from(listeners)) { try { fn(next.visible, next); } catch (e) { setTimeout(() => { throw e; }); } }
  }

  on('host:visibility', f => {
    if (!f || typeof f.visible !== 'boolean') return;
    host = f.visible;
    compute();
  });
  if (doc && typeof doc.addEventListener === 'function') doc.addEventListener('visibilitychange', compute);
  compute();

  return {
    get visible() { return last ? last.visible : true; },
    get value() { return last; },
    // fn(visible, presence) on every change of `visible` → off()
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    refresh: compute,
  };
}
