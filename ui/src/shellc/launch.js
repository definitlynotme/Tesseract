// The launch screen (11-05 window-first launch; DESIGN section 7 rule 11).
//
// The shell opens the window while the runtime is still starting (its first hello takes a few
// seconds on a real machine). Until the page has real state, a white ground carries the brand
// mark, and the mark tells its own story once: the two strokes draw from the narrow passage out
// to the edges, the wordmark comes up, the light lands; then the light breathes while the page
// waits (something is pending, rule 7). When the page is ready, the mark travels onto the
// sidebar's own mark (one transform) while the ground fades, so the app is revealed by the logo
// it already has. No words (the wordmark is the brand, never translated), nothing added on top.
//
// Phases: 'cover' (mounted with the page, nothing drawn; the window is still cloaked)
//   -> 'shown'   the replay ended without a service.state snapshot (the runtime is starting): the
//                draw starts and the page may let the shell reveal the window (ready.js)
//   -> 'leaving' ready, or a fallback ran out: the mark travels, the ground fades
//   -> 'gone'    the cover is removed; #app is no longer inert
// Real state before the cover was shown (a reload or a recreated window over a live runtime):
// the cover goes in the same frame, nothing is drawn. Reduced motion or a hidden window: every
// step is instant (the strokes are drawn, the light lit, the leave is a removal).
//
// Ports: doc, win; ready() -> bool (the page shows real state: ready.js's condition);
// reduced() -> bool; visible() -> bool; settle {easing, duration} (T.motion.SP.settle);
// now() -> ms; timer(fn, ms) -> handle, clear(handle); onShown() once the launch is on screen
// (index.js runs ready.check: the window may be revealed); onGone() after the cover left.

export const LAUNCH_SHOW_FALLBACK_MS = 700;   // no rt:replayed by then: show anyway (the shell reveals at 1.5 s)
export const LAUNCH_MIN_SHOWN_MS = 1150;      // the draw has finished (60 + 640 strokes, light lands by 1120)
export const LAUNCH_AFTER_HELLO_MS = 4000;    // a runtime that said hello but sent no state: leave anyway
export const LAUNCH_LIMIT_MS = 30000;         // never hold the app behind the cover longer than this
export const LAUNCH_SETTLE_MS = 32;           // the leave starts after the page applied the state (run, dot colour)
const NS = 'http://www.w3.org/2000/svg';
// the sidebar mark's two subpaths (index.html .brand-mark), drawn separately so both open at once
const STROKES = ['M42.5 90V47A17 17 0 0 0 25.5 30H6', 'M57.5 90V47A17 17 0 0 1 74.5 30H94'];

export function createLaunch({ doc, win, ready = () => false, reduced = () => false, visible = () => true,
  settle = { easing: 'cubic-bezier(.2,.8,.2,1)', duration: 460 }, now = () => Date.now(),
  timer = (fn, ms) => setTimeout(fn, ms), clear = h => clearTimeout(h), onShown = () => {}, onGone = () => {} } = {}) {
  let phase = 'cover', shownAt = null, helloAt = null, pending = null;
  const timers = new Set();
  const later = (fn, ms) => { const h = timer(() => { timers.delete(h); fn(); }, ms); timers.add(h); return h; };
  const root = doc.documentElement;
  const app = doc.getElementById('app');

  const el = doc.createElement('div');
  el.className = 'launch';
  el.id = 'launch';
  el.setAttribute('aria-hidden', 'true');
  const ground = doc.createElement('div');
  ground.className = 'launch-ground';
  const brand = doc.createElement('div');
  brand.className = 'launch-brand';
  const svg = doc.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'launch-mark');
  svg.setAttribute('viewBox', '0 0 100 100');
  for (const d of STROKES) {
    const p = doc.createElementNS(NS, 'path');
    p.setAttribute('d', d);
    p.setAttribute('pathLength', '1');
    svg.appendChild(p);
  }
  const dot = doc.createElementNS(NS, 'circle');
  dot.setAttribute('class', 'launch-dot');
  dot.setAttribute('cx', '80'); dot.setAttribute('cy', '11'); dot.setAttribute('r', '5.8');
  svg.appendChild(dot);
  const word = doc.createElement('span');
  word.className = 'launch-word';
  word.setAttribute('translate', 'no');
  word.textContent = 'Tesseract';
  brand.append(svg, word);
  el.append(ground, brand);
  doc.body.appendChild(el);
  root.classList.add('launching');
  if (app) app.inert = true;
  later(() => { if (phase === 'cover') show(); }, LAUNCH_SHOW_FALLBACK_MS);
  later(() => leave(), LAUNCH_LIMIT_MS);

  function instant() { return reduced() || !visible(); }

  function finish() {
    if (phase === 'gone') return;
    phase = 'gone';
    for (const h of timers) clear(h);
    timers.clear();
    el.remove();
    root.classList.remove('launching');
    if (app) app.inert = false;
    onGone();
  }

  // the runtime is still starting: draw (or, without motion, the drawn mark at once)
  function show() {
    if (phase !== 'cover') return;
    if (ready()) { finish(); return; }
    phase = 'shown';
    shownAt = now();
    el.classList.add(instant() ? 'is-still' : 'is-drawing');
    onShown();
  }

  // the shell's replay ended: no service.state in it means the runtime has not said hello yet
  function replayed() {
    if (phase === 'cover') { if (ready()) finish(); else show(); }
    else check();
  }

  function hello() {
    if (helloAt !== null || phase === 'gone') return;
    helloAt = now();
    later(() => leave(), LAUNCH_AFTER_HELLO_MS);
  }

  // real state may have arrived: leave once the draw has had its time
  function check() {
    if (!ready()) return;
    if (phase === 'cover') { finish(); return; }
    if (phase !== 'shown') return;
    if (instant()) { leave(); return; }
    // the state that made the page ready is applied in the same task (run state, the sidebar dot's
    // colour): the travel starts a frame later, never before the draw has had its time
    const wait = Math.max(LAUNCH_SETTLE_MS, shownAt + LAUNCH_MIN_SHOWN_MS - now());
    if (!pending) pending = later(() => { pending = null; if (ready()) leave(); }, wait);
  }

  // the mark travels onto the sidebar's mark (one transform from the launch mark's box to the
  // sidebar mark's box; the wordmark shares the transform and lands on the sidebar wordmark, the
  // two being laid out at exactly twice the sidebar's size), the light takes the sidebar dot's
  // colour, the ground fades; then the cover is removed in the frame the sidebar shows its own
  function leave() {
    if (phase === 'gone' || phase === 'leaving') return;
    if (phase === 'cover' || instant()) { finish(); return; }
    phase = 'leaving';
    const target = doc.querySelector('.side .brand-mark');
    const to = target && target.getBoundingClientRect();
    const from = svg.getBoundingClientRect();
    const box = brand.getBoundingClientRect();
    if (!to || !to.width || !from.width || typeof brand.animate !== 'function') { finish(); return; }
    const targetWord = doc.querySelector('.side .wordmark');
    const wordShown = !!(targetWord && targetWord.getBoundingClientRect().width);
    const targetDot = target.querySelector('.brand-dot');
    // the sidebar dot's own colour, not a frame of its fill transition (it is hidden while the
    // cover is up, so ending that transition early shows nowhere)
    const fillOf = () => {
      if (!targetDot || !win || typeof win.getComputedStyle !== 'function') return null;
      const st = targetDot.style;
      if (st) st.transition = 'none';
      const f = win.getComputedStyle(targetDot).fill;
      if (st) st.transition = '';
      return f;
    };
    const fill = fillOf();
    const s = to.width / from.width;
    brand.style.transformOrigin = `${from.left - box.left}px ${from.top - box.top}px`;
    el.classList.remove('is-drawing', 'is-still');
    el.classList.add('is-leaving');
    if (fill) dot.style.fill = fill;
    const D = settle.duration;
    // the sidebar dot may still change while the mark travels (its run state): follow it once more
    later(() => { const f = fillOf(); if (f && f !== dot.style.fill) dot.style.fill = f; }, Math.round(D / 2));
    const fly = brand.animate([{ transform: 'none' }, { transform: `translate(${to.left - from.left}px, ${to.top - from.top}px) scale(${s})` }],
      { duration: D, easing: settle.easing, fill: 'forwards' });
    ground.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, delay: 120, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
    if (!wordShown) word.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
    let done = false;
    const end = () => { if (!done) { done = true; finish(); } };
    fly.finished.then(end, end);
    later(end, D + 200); // a hidden window may hold the animation clock
  }

  return {
    replayed, hello, check, leave,
    get phase() { return phase; },
    // ready.js: the launch is on screen, so the window may be revealed before real state
    get shown() { return phase === 'shown' || phase === 'leaving'; },
    dispose: finish,
  };
}
