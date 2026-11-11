// The notice card stack as data (no DOM; ui/test/unit/notice_model.test.mjs).
//
// normalize(frame.card) turns a card from the shell (shell/app/src/notify.rs Notice::card and
// shell_card) into what the view draws, or null for a malformed one. Text is never markup: a
// card carries message ids (rendered with T.t / T.ti and its params) plus the frame's fallback
// title / detail text for an id the catalogs lack (and for a legacy frame without a notice).
//
// createStack() keeps which cards are on screen and which are folded behind the 「还有 N 条」 row:
// newest at the bottom, at most MAX_VISIBLE on screen, the oldest fold first and come back
// (the most recently folded first, on top) when room frees. One card per incident: a later frame
// of the incident changes its card in place (resolved morphs it into the recovered state); a
// frame for a folded incident brings it back as the newest; closed takes it away silently.
// Every method returns the operations the view runs, in order:
//   {op:'add', card}             a new card at the bottom (enters)
//   {op:'update', card, prev}    the incident's card shows the new frame in place
//   {op:'morph', card, prev}     ... and turns into the recovered state (resolved)
//   {op:'fold', card}            the oldest card on screen moves behind the row (no exit motion)
//   {op:'unfold', card}          a folded card comes back on top
//   {op:'remove', card}          a card leaves (exit motion)
//   {op:'more', count}           the row says how many are folded (0: no row)

export const MAX_VISIBLE = 3;
export const STATES = Object.freeze(['open', 'update', 'resolved', 'closed']);
const TONES = Object.freeze({ info: 'info', warn: 'warn', error: 'err', critical: 'err' });
const ICONS = Object.freeze({ err: 'error', warn: 'warning', info: 'info', ok: 'success' });
const LEVELS = Object.freeze({ err: 'notice.level.error', warn: 'notice.level.warn' });
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const text = v => (typeof v === 'string' ? v : '');
// a UI message id: lower-case dotted segments (notice.outage.title, errors.start.port_in_use)
const messageId = v => (typeof v === 'string' && v.length <= 96 && /^[a-z0-9_]+(\.[a-z0-9_]+)+$/.test(v) ? v : null);

/** A card frame from the shell → the view's card, or null. */
export function normalize(c) {
  if (!c || typeof c !== 'object' || Array.isArray(c)) return null;
  if (typeof c.delivery_id !== 'string' || !c.delivery_id || typeof c.incident !== 'string' || !c.incident) return null;
  if (!STATES.includes(c.state)) return null;
  const severity = own(TONES, c.severity) ? c.severity : 'info';
  const resolved = c.state === 'resolved';
  const tone = resolved ? 'ok' : TONES[severity];
  const params = {};
  if (c.params && typeof c.params === 'object' && !Array.isArray(c.params)) {
    for (const [k, v] of Object.entries(c.params)) {
      if (typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v))) params[k] = v;
    }
  }
  const sticky = c.sticky === true;
  const ttl = sticky ? 0 : (Number.isFinite(c.ttl_ms) && c.ttl_ms > 0 ? Math.min(Math.max(c.ttl_ms, 1500), 120000)
    : resolved ? 6000 : severity === 'info' ? 8000 : 10000);
  return {
    id: c.delivery_id, incident: c.incident, state: c.state, kind: text(c.kind), tone, icon: ICONS[tone], level: LEVELS[tone] || null,
    titleId: messageId(c.title_id), detailId: messageId(c.detail_id), reasonId: messageId(c.reason_id), fixId: messageId(c.fix_id),
    title: text(c.title), detail: text(c.detail), params, action: c.action === 'view', sticky, ttl, shell: c.shell === true,
  };
}

export function createStack({ max = MAX_VISIBLE } = {}) {
  const shown = [];  // on screen, oldest first (top → bottom)
  const folded = []; // behind the row, oldest first
  const at = (list, incident) => list.findIndex(c => c.incident === incident);
  const more = ops => { ops.push({ op: 'more', count: folded.length }); return ops; };
  function foldOldest(ops) {
    while (shown.length > max) { const old = shown.shift(); folded.push(old); ops.push({ op: 'fold', card: old }); }
  }
  function refill(ops) {
    while (shown.length < max && folded.length) { const back = folded.pop(); shown.unshift(back); ops.push({ op: 'unfold', card: back }); }
  }
  function drop(incident, ops) {
    const i = at(shown, incident);
    if (i >= 0) { const [gone] = shown.splice(i, 1); ops.push({ op: 'remove', card: gone }); refill(ops); return true; }
    const j = at(folded, incident);
    if (j >= 0) folded.splice(j, 1);
    return j >= 0;
  }
  return {
    /** A frame from the shell (a normalized card). */
    apply(card) {
      const ops = [];
      if (card.state === 'closed') { drop(card.incident, ops); return more(ops); }
      const i = at(shown, card.incident);
      if (i >= 0) {
        const prev = shown[i];
        shown[i] = card;
        ops.push({ op: card.state === 'resolved' ? 'morph' : 'update', card, prev });
        return more(ops);
      }
      const j = at(folded, card.incident);
      if (j >= 0) folded.splice(j, 1);
      shown.push(card);
      ops.push({ op: 'add', card });
      foldOldest(ops);
      return more(ops);
    },
    /** The shell takes an incident's card away (silently). */
    close(incident) { const ops = []; drop(incident, ops); return more(ops); },
    /** A card left the screen on its own (×, its timer, opened): no remove op, the view animates it. */
    leave(incident) {
      const ops = [];
      const i = at(shown, incident);
      if (i < 0) return ops;
      shown.splice(i, 1);
      refill(ops);
      return more(ops);
    },
    /** A new sidecar epoch: every card but the shell's own goes. */
    clear() {
      const ops = [];
      for (const card of shown.filter(c => !c.shell)) { shown.splice(shown.indexOf(card), 1); ops.push({ op: 'remove', card }); }
      for (const card of folded.filter(c => !c.shell)) folded.splice(folded.indexOf(card), 1);
      refill(ops);
      return more(ops);
    },
    find(incident) { const i = at(shown, incident); return i >= 0 ? shown[i] : null; },
    get shown() { return shown.slice(); },
    get folded() { return folded.slice(); },
  };
}

/** The damped spring of DESIGN §3.8 as a CSS linear() curve (k 380, c 38: the soft preset). */
export function springCurve(k, c) {
  const w0 = Math.sqrt(k), z = c / (2 * Math.sqrt(k)), wd = w0 * Math.sqrt(Math.max(1e-6, 1 - z * z));
  const x = t => 1 - Math.exp(-z * w0 * t) * (Math.cos(wd * t) + (z * w0 / wd) * Math.sin(wd * t));
  let T = 0;
  while (T < 2 && !(Math.abs(1 - x(T)) < 1e-3 && Math.abs(1 - x(T + 0.016)) < 1e-3)) T += 0.004;
  const pts = [];
  for (let i = 0; i <= 40; i++) pts.push(i === 40 ? 1 : +x(T * i / 40).toFixed(4));
  return { easing: `linear(${pts.join(', ')})`, duration: Math.round(T * 1000) };
}

/** The card's time line: notice.time.now within a minute, then notice.time.minutes {n}. */
export function timeLabel(at, now) {
  const n = Math.floor(Math.max(0, now - at) / 60000);
  return n < 1 ? { id: 'notice.time.now', params: null } : { id: 'notice.time.minutes', params: { n } };
}
