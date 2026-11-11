// The notice cards in the DOM (the card window of shell/app/src/notice.rs; the look and motion
// of .build/claude_notice_mockup, DESIGN §4.2 / §7 of the plan).
//
// show(card) / close(incident) / clear() run the stack's operations (model.js): a new card
// enters at the bottom on the soft spring (8 px, scale .98, fade; fades only under reduced
// motion), the others glide (FLIP), a recovered incident morphs in place (the icon cross-fades,
// the words roll), a card leaves in 140 ms to the right. The countdown ring around the icon is
// the timer of a card that is not sticky; the pointer over the stack pauses every ring. × closes
// (notice_dismiss), the body or 「查看」 opens the main window at the card's target (notice_open).
// After every change the stack's height goes to the shell (notice_resize: it sizes and shows or
// hides the window), then a new frame is confirmed (notice_rendered). Text is text nodes only:
// catalog messages through T.ti (relabelled in place when the language changes), fallback text
// through textContent with translate="no".
import { createStack, springCurve, timeLabel } from './model.js';
import { iconNode, markNode, ringNode, RING_RADIUS } from './svg.js';

const EXIT = Object.freeze({ duration: 140, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
const FADE = Object.freeze({ duration: 160, easing: 'cubic-bezier(.2,.8,.2,1)' });
/** How often the time line (刚刚 / n 分钟前) is redrawn while cards are on screen. */
export const CLOCK_MS = 30000;
const sameText = (a, b, id, value) => a[id] === b[id] && a[value] === b[value] && JSON.stringify(a.params) === JSON.stringify(b.params);

export function createView({ doc, win, T, invoke, reduced = false, stack = createStack() }) {
  const root = doc.getElementById('stack');
  const SP = springCurve(380, 38);
  const has = id => !!id && !!(T.i18n && typeof T.i18n.hasAny === 'function' && T.i18n.hasAny(id));
  const icon = (name, size) => iconNode(doc, String(T.icon(name, size)));
  const entries = new Map(); // incident → entry of a card on screen (not leaving)
  const byNode = new WeakMap();
  let row = null, paused = false, clock = 0, height = -1;

  doc.documentElement.style.setProperty('--spring', SP.easing);
  doc.documentElement.style.setProperty('--spring-dur', `${SP.duration}ms`);
  if (reduced) doc.documentElement.classList.add('is-reduced');

  const el = (tag, cls) => { const e = doc.createElement(tag); if (cls) e.className = cls; return e; };
  // a catalog message when the catalogs have the id, else the frame's fallback text (data)
  function put(target, id, params, fallback) {
    if (id && has(id)) T.ti(target, id, params || null);
    else { target.textContent = fallback || ''; if (fallback) target.setAttribute('translate', 'no'); }
    return target;
  }
  const titleSpan = card => put(el('span'), card.titleId, card.params, card.title || card.titleId || '');
  const hasDetail = card => (card.detailId ? has(card.detailId) || !!card.detail : !card.titleId && !!card.detail);
  const detailSpan = card => put(el('span'), card.detailId, card.params, card.detail);

  // ---- motion ----------------------------------------------------------------------------
  function once(animation, fn, fallback = 400) {
    let done = false;
    const run = () => { if (!done) { done = true; fn(); } };
    if (animation) { animation.onfinish = run; animation.oncancel = run; }
    win.setTimeout(run, fallback);
  }
  function enter(node) {
    node.animate(reduced ? [{ opacity: 0 }, { opacity: 1 }]
      : [{ opacity: 0, translate: '0 8px', scale: 0.98 }, { opacity: 1, translate: '0 0', scale: 1 }], reduced ? FADE : SP);
  }
  // the cards that stay glide to their new place on the soft spring
  function flip(mutate) {
    const before = new Map([...root.children].map(k => [k, k.getBoundingClientRect().top]));
    mutate();
    if (reduced) return;
    for (const k of root.children) {
      if (!before.has(k)) continue;
      const dy = before.get(k) - k.getBoundingClientRect().top;
      if (Math.abs(dy) > 0.5) k.animate([{ translate: `0 ${dy}px` }, { translate: '0 0' }], SP);
    }
  }
  // words roll: the old line leaves upwards while the new one rises into the same cell
  function swap(box, next) {
    const old = box.firstElementChild;
    if (!old || reduced) { box.replaceChildren(next); return; }
    box.classList.add('is-swap');
    box.appendChild(next);
    once(old.animate([{ opacity: 1, translate: '0 0' }, { opacity: 0, translate: '0 -6px' }], EXIT), () => {
      old.remove();
      if (box.childElementCount <= 1) box.classList.remove('is-swap');
    });
    next.animate([{ opacity: 0, translate: '0 6px' }, { opacity: 1, translate: '0 0' }], { duration: SP.duration, easing: SP.easing, delay: 60, fill: 'backwards' });
  }

  // ---- one card ---------------------------------------------------------------------------
  function setLevel(entry) {
    const hd = entry.node.firstElementChild;
    for (const n of hd.querySelectorAll('.nc-sep, .nc-lv')) n.remove();
    if (!entry.card.level) return;
    const sep = el('span', 'nc-sep'); sep.textContent = '·'; sep.setAttribute('aria-hidden', 'true');
    hd.insertBefore(sep, entry.time);
    hd.insertBefore(T.ti(el('span', 'nc-lv'), entry.card.level), entry.time);
  }
  function setBody(entry, roll) {
    const { card, tx } = entry;
    let m = tx.querySelector(':scope > .nc-m');
    if (hasDetail(card)) {
      if (!m) { m = el('div', 'nc-m'); m.appendChild(detailSpan(card)); entry.title.after(m); }
      else if (roll && !sameText(entry.shownDetail || {}, card, 'detailId', 'detail')) swap(m, detailSpan(card));
      else if (!roll) m.replaceChildren(detailSpan(card));
    } else if (m) m.remove();
    entry.shownDetail = card;
    for (const n of tx.querySelectorAll(':scope > .nc-r, :scope > .nc-ft')) n.remove();
    const reason = has(card.reasonId), fix = has(card.fixId);
    if (reason || fix) {
      const r = el('div', 'nc-r');
      if (reason) r.appendChild(put(el('span', 't-voice'), card.reasonId, card.params));
      if (reason && fix) r.appendChild(doc.createTextNode(' · '));
      if (fix) r.appendChild(put(el('span'), card.fixId, card.params));
      tx.appendChild(r);
    }
    if (card.action) {
      const ft = el('div', 'nc-ft');
      const b = el('button', 'nc-btn'); b.type = 'button';
      T.ti(b, 'notice.action.view');
      b.addEventListener('click', e => { e.stopPropagation(); open(entry.node); });
      ft.appendChild(b);
      tx.appendChild(ft);
    }
  }
  function stamp(entry) {
    const { id, params } = timeLabel(entry.at, Date.now());
    T.ti(entry.time, id, params);
  }
  function make(card) {
    const node = el('article', `nc nc-${card.tone}`);
    node.setAttribute('role', card.tone === 'err' ? 'alert' : 'status');
    const hd = el('div', 'nc-hd');
    const app = el('span', 'nc-app'); app.textContent = 'Tesseract'; app.setAttribute('translate', 'no');
    const time = el('span', 'nc-time');
    const x = el('button', 'nc-x'); x.type = 'button';
    T.ti(x, 'notice.close', null, { attr: 'aria-label' });
    x.appendChild(icon('close', 12));
    hd.append(markNode(doc), app, time, x);
    const bd = el('div', 'nc-bd');
    const ic = el('span', 'nc-ic');
    const ringSvg = ringNode(doc);
    ic.append(icon(card.icon, 16), ringSvg);
    const tx = el('div', 'nc-tx');
    const title = el('div', 'nc-t'); title.appendChild(titleSpan(card));
    tx.appendChild(title);
    bd.append(ic, tx);
    node.append(hd, bd);
    const entry = { node, card, time, ic, ringSvg, ring: ringSvg.firstElementChild, tx, title, at: Date.now(), timer: null, shownDetail: null };
    byNode.set(node, entry);
    entries.set(card.incident, entry);
    setLevel(entry); setBody(entry, false); stamp(entry);
    x.addEventListener('click', e => { e.stopPropagation(); leave(entry.card.incident, 'user'); });
    node.addEventListener('click', e => { if (e.target && e.target.closest && e.target.closest('button')) return; open(node); });
    return entry;
  }
  function crossIcon(entry, name) {
    const old = entry.ic.querySelector(':scope > .ic');
    const next = icon(name, 16);
    entry.ic.insertBefore(next, entry.ringSvg);
    if (!old) return;
    if (reduced) { old.remove(); return; }
    next.animate([{ opacity: 0, scale: 0.6 }, { opacity: 1, scale: 1 }], SP);
    once(old.animate([{ opacity: 1 }, { opacity: 0 }], EXIT), () => old.remove());
  }
  // a later frame of the incident in place (update), or its recovered state (morph)
  function refresh(entry, card, prev) {
    entry.card = card;
    const { node } = entry;
    if (prev.tone !== card.tone) { node.classList.remove(`nc-${prev.tone}`); node.classList.add(`nc-${card.tone}`); }
    node.setAttribute('role', card.tone === 'err' ? 'alert' : 'status');
    setLevel(entry);
    if (prev.icon !== card.icon) crossIcon(entry, card.icon);
    if (!sameText(prev, card, 'titleId', 'title')) swap(entry.title, titleSpan(card));
    // (inside run()'s FLIP: rows that come or go move the cards above on the same glide)
    setBody(entry, true);
    entry.at = Date.now();
    stamp(entry);
    arm(entry);
  }
  // the countdown ring is the timer; a sticky card has none
  function arm(entry) {
    disarm(entry);
    const { card } = entry;
    entry.ringSvg.classList.toggle('is-off', card.sticky || !card.ttl);
    if (card.sticky || !card.ttl) return;
    const len = 2 * Math.PI * RING_RADIUS;
    entry.ring.setAttribute('stroke-dasharray', len.toFixed(2));
    const timer = entry.ring.animate([{ strokeDashoffset: 0 }, { strokeDashoffset: len }],
      { duration: card.ttl, easing: reduced ? `steps(${Math.max(1, Math.round(card.ttl / 1000))}, end)` : 'linear', fill: 'forwards' });
    if (paused) timer.pause();
    timer.onfinish = () => { if (entry.timer === timer) leave(entry.card.incident, 'timeout'); };
    entry.timer = timer;
  }
  function disarm(entry) { const t = entry.timer; entry.timer = null; if (t) t.cancel(); }

  // ---- the stack --------------------------------------------------------------------------
  function setMore(count) {
    if (!count) { if (row) { row.remove(); row = null; } return; }
    if (!row) { row = el('div', 'nc-more'); root.prepend(row); enter(row); }
    T.ti(row, 'notice.more', { count });
  }
  function apply(op) {
    if (op.op === 'add' || op.op === 'unfold') {
      const entry = make(op.card);
      const first = op.op === 'unfold' ? root.querySelector(':scope > .nc') : null;
      if (first) root.insertBefore(entry.node, first); else root.appendChild(entry.node);
      enter(entry.node);
      arm(entry);
    } else if (op.op === 'fold') {
      const entry = entries.get(op.card.incident);
      if (entry) { disarm(entry); entry.node.remove(); entries.delete(op.card.incident); }
    } else if (op.op === 'update' || op.op === 'morph') {
      const entry = entries.get(op.card.incident);
      if (entry) refresh(entry, op.card, op.prev);
    } else if (op.op === 'more') setMore(op.count);
  }
  // a card leaves to the right; what follows it (a folded card back on top, the row) runs once it is gone
  function exit(incident, rest, why) {
    const entry = entries.get(incident);
    if (!entry) { flip(() => rest.forEach(apply)); report(); return; }
    entries.delete(incident);
    disarm(entry);
    if (why === 'user') invoke('notice_dismiss', { delivery_id: entry.card.id });
    const animation = entry.node.animate(reduced ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 1, translate: '0 0' }, { opacity: 0, translate: '12px 0' }], EXIT);
    once(animation, () => { flip(() => { entry.node.remove(); rest.forEach(apply); }); report(); });
  }
  function run(ops) {
    const removes = ops.filter(o => o.op === 'remove');
    const rest = ops.filter(o => o.op !== 'remove');
    if (!removes.length) { flip(() => rest.forEach(apply)); return; }
    removes.forEach((o, i) => exit(o.card.incident, i === removes.length - 1 ? rest : [], 'quiet'));
  }
  function leave(incident, why) {
    if (!entries.has(incident)) return;
    exit(incident, stack.leave(incident), why);
  }
  function open(node) {
    const entry = byNode.get(node);
    if (!entry || entries.get(entry.card.incident) !== entry) return;
    invoke('notice_open', { delivery_id: entry.card.id });
    leave(entry.card.incident, 'open');
  }
  // the stack's height to the shell (it sizes, shows or hides the window)
  function report() {
    const next = root.childElementCount ? Math.ceil(root.getBoundingClientRect().height) : 0;
    if (!entries.size && clock) { win.clearInterval(clock); clock = 0; }
    if (next === height) return;
    height = next;
    invoke('notice_resize', { height: next });
  }
  function startClock() {
    if (clock || !entries.size) return;
    clock = win.setInterval(() => { for (const entry of entries.values()) stamp(entry); }, CLOCK_MS);
  }

  root.addEventListener('pointerenter', () => { paused = true; for (const e of entries.values()) if (e.timer) e.timer.pause(); });
  root.addEventListener('pointerleave', () => { paused = false; for (const e of entries.values()) if (e.timer) e.timer.play(); });
  if (typeof win.ResizeObserver === 'function') new win.ResizeObserver(() => report()).observe(root);

  return {
    /** A frame from the shell (a normalized card); confirmed once it is in the page. */
    show(card) {
      const ops = stack.apply(card);
      run(ops);
      report();
      if (ops.some(o => o.card === card && (o.op === 'add' || o.op === 'update' || o.op === 'morph'))) invoke('notice_rendered', { delivery_id: card.id });
      startClock();
    },
    close(incident) { run(stack.close(incident)); report(); },
    clear() { run(stack.clear()); report(); },
    get count() { return entries.size; },
  };
}
