// The letter for users (11-05, user: 「塞进一个很小的说明书……做成书信的样子……用户没有打开过的
//情况下，可以有一个小标识引导用户打开」). DESIGN §6.22, §7 rule 12.
//
// The product name beside the brand mark (#letter-open, index.html) opens a short letter of six
// pages: hello, three steps to begin, the tiers, the two ways to take over traffic, the few things
// to keep in mind, and a last page that is signed. It is where the guidance lives that the interface
// itself no longer carries (F-03 / F-07: no product advice and no standing warnings on screen); the
// interface keeps only what it must say at the moment of an action or an event.
//
// The pages are leafed like a stack of letter paper (user: 页面向左偏转然后消失，就好像翻阅一叠
//信件纸张一样): the sheet in hand tilts to the left and slides away, showing the next one; going
// back, the sheet before returns from the left. The sheets under the paper show how many are left.
//
// Unread: an envelope beside the name until this edition of the letter has been opened once. What
// was opened is remembered as an edition number in WebView localStorage (every read and write in
// try / catch; when storage fails, for this page session only). A new LETTER_EDITION, given when
// the letter's words change in a way users should read again, shows the envelope again; small
// fixes keep the edition. test/unit/letter.test.mjs pins the zh_CN words of this edition
// (LETTER_DIGEST), so changing them means deciding: bump the edition, or only update the digest.
//
// Words: the letter namespace (zh_CN letter.json). Interface words a page quotes (a page name, a
// button) are the interface's own messages passed as arguments, so the letter always says what the
// screen shows. Not translated: the heading and the signature.

import { flourish, letterhead, modesArt, relayArt, stepsArt } from './art.js';

export const LETTER_EDITION = 1;
// sha256 of the zh_CN texts of LETTER_IDS joined by "\n", first 16 hex digits (letter.test.mjs)
export const LETTER_DIGEST = 'c55036076ca91521';
export const STORAGE_KEY = 'tesseract.letter';
export const HEADING = 'For users';
export const SIGNATURE = 'Mark Joel Wexler';

// a block's words: its message and the interface messages it quotes ({param: message id})
const W = (id, args) => ({ id, args: args || null });
export const PAGES = Object.freeze([
  { name: 'hello', blocks: [
    { kind: 'letterhead' },
    { kind: 'hello', ...W('letter.p1.hello') },
    { kind: 'p', ...W('letter.p1.thanks') },
    { kind: 'p', ...W('letter.p1.what') },
    { kind: 'p', ...W('letter.p1.about') },
  ] },
  { name: 'start', title: 'letter.p2.title', art: 'steps', blocks: [
    { kind: 'step', ...W('letter.p2.import', { tiers: 'shell.nav.profiles', import: 'tiers.toolbar.import' }) },
    { kind: 'step', ...W('letter.p2.start', { overview: 'shell.nav.overview', start: 'overview.run.start' }) },
    { kind: 'step', ...W('letter.p2.running', { running: 'overview.run.running' }) },
    { kind: 'note', ...W('letter.p2.tray') },
  ] },
  { name: 'tiers', title: 'letter.p3.title', art: 'relay', blocks: [
    { kind: 'p', ...W('letter.p3.vanguard') },
    { kind: 'p', ...W('letter.p3.sentinel') },
    { kind: 'p', ...W('letter.p3.bastion') },
    { kind: 'p', ...W('letter.p3.gateway') },
    { kind: 'note', ...W('letter.p3.relay') },
  ] },
  { name: 'modes', title: 'letter.p4.title', art: 'modes', blocks: [
    { kind: 'p', ...W('letter.p4.system', { system: 'mode.system.label' }) },
    { kind: 'p', ...W('letter.p4.tun', { tun: 'mode.tun.label' }) },
    { kind: 'note', ...W('letter.p4.drift', { overview: 'shell.nav.overview', restart: 'overview.restart.label' }) },
  ] },
  { name: 'care', title: 'letter.p5.title', blocks: [
    { kind: 'item', icon: 'killswitch', ...W('letter.p5.ks', { ks: 'settings.conn.ks.title' }) },
    { kind: 'item', icon: 'bastion', ...W('letter.p5.bastion') },
    { kind: 'item', icon: 'export', ...W('letter.p5.bundle') },
  ] },
  { name: 'end', title: 'letter.p6.title', blocks: [
    { kind: 'p', ...W('letter.p6.search') },
    { kind: 'p', ...W('letter.p6.logs', { logs: 'shell.nav.logs' }) },
    { kind: 'p', ...W('letter.p6.again') },
    { kind: 'wish', ...W('letter.p6.wish') },
    { kind: 'sign' },
  ] },
]);
// the words of the letter itself, in reading order (the digest covers these; the chrome's words
// below are not part of the edition)
export const LETTER_IDS = Object.freeze(PAGES.flatMap(p => [p.title, ...p.blocks.map(b => b.id)].filter(Boolean)));
export const CHROME_IDS = Object.freeze(['letter.entry.tip', 'letter.entry.aria', 'letter.aria', 'letter.page', 'letter.nav.prev',
  'letter.nav.next', 'letter.nav.done', 'letter.palette.label', 'letter.palette.keywords']);

function localStore(win) {
  try { const s = win && win.localStorage; return s && typeof s.getItem === 'function' ? s : null; } catch (e) { return null; }
}

/** has this edition been opened? { unread, markRead() } over a {getItem, setItem} storage or null */
export function createLetterState({ storage = null, edition = LETTER_EDITION } = {}) {
  let session = 0;
  const stored = () => {
    try {
      const raw = storage && storage.getItem(STORAGE_KEY);
      const v = raw ? JSON.parse(raw) : null;
      return v && Number.isInteger(v.read) ? v.read : 0;
    } catch (e) { return 0; }
  };
  return {
    get unread() { return Math.max(session, stored()) < edition; },
    markRead() {
      session = Math.max(session, edition);
      try { if (storage) storage.setItem(STORAGE_KEY, JSON.stringify({ read: Math.max(edition, stored()) })); } catch (e) { /* this page session only */ }
    },
  };
}

const TURN_MS = 520;      // forward: the sheet in hand tilts left and slides away
const RETURN_MS = 480;    // backward: the sheet before comes back onto the stack

export function installLetter(T, { win = window, doc = document, storage } = {}) {
  const btn = doc.getElementById('letter-open');
  if (!btn) return null;
  const M = T.motion;
  const mail = btn.querySelector('.brand-mail');
  const state = createLetterState({ storage: storage !== undefined ? storage : localStore(win) });
  const t = (id, params) => T.t(id, params);

  const label = () => T.ti(btn, 'letter.entry.aria', { unread: state.unread ? 'yes' : 'no' }, { attr: 'aria-label' });
  T.ti(btn, 'letter.entry.tip', null, { attr: 'data-tip' });
  label();

  /* -- the envelope: there while this edition is unread. It arrives once, after the launch has
     handed the brand to the sidebar (or soon after boot when there was no launch) ------------- */
  // (an <svg> has no hidden property: the attribute itself)
  if (mail) {
    mail.toggleAttribute('hidden', !state.unread);
    if (state.unread && !M.reduced) {
      mail.classList.add('is-waiting');
      const arrive = () => win.setTimeout(() => {
        mail.classList.remove('is-waiting');
        M.anim(mail, [{ opacity: 0, transform: 'translate(-6px, 3px) rotate(-12deg) scale(.7)' }, { opacity: 1, transform: 'none' }],
          { duration: M.SP.soft.duration + 120, easing: M.SP.soft.easing });
      }, 560);
      const root = doc.documentElement;
      if (root.classList.contains('launching') && typeof win.MutationObserver === 'function') {
        const mo = new win.MutationObserver(() => { if (!root.classList.contains('launching')) { mo.disconnect(); arrive(); } });
        mo.observe(root, { attributes: true, attributeFilter: ['class'] });
      } else arrive();
    }
  }
  function putAway() {
    if (!mail || mail.hasAttribute('hidden')) return;
    mail.classList.remove('is-waiting');
    const a = M.anim(mail, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translate(4px, -3px) scale(.6)' }], { duration: 200, easing: M.ease.in, fill: 'forwards' });
    M.onDone(a, () => { mail.setAttribute('hidden', ''); if (a) a.cancel(); }, 400);
  }

  /* -- one page --------------------------------------------------------------------------- */
  const words = b => {
    const args = {};
    if (b.args) for (const k of Object.keys(b.args)) args[k] = t(b.args[k]);
    return T.tx(b.id, args);
  };
  const ART = {
    steps: () => stepsArt(T, doc),
    relay: () => relayArt(T, doc),
    modes: () => modesArt(T, doc, { system: t('mode.system.label'), tun: t('mode.tun.label') }),
  };
  function pageNode(i) {
    const p = PAGES[i];
    const plays = [];
    const lines = [];
    const kids = [];
    if (i > 0) kids.push(T.h('div', { class: 'lt-run', 'aria-hidden': 'true' }, T.h('span', { class: 'lt-run-h', translate: 'no' }, HEADING), T.h('span', { class: 'lt-run-n tnum' }, `${i + 1} / ${PAGES.length}`)));
    if (p.title) { const h = T.h('h2', { class: 'lt-title' }, t(p.title)); kids.push(h); lines.push(h); }
    if (p.art) { const a = ART[p.art](); kids.push(a.el); plays.push(a.play); }
    let list = null;
    const listOf = (tag, cls) => { if (!list || list.cls !== cls) { list = { cls, el: T.h(tag, { class: cls }) }; kids.push(list.el); } return list.el; };
    let n = 0;
    for (const b of p.blocks) {
      if (b.kind !== 'step' && b.kind !== 'item') list = null;
      if (b.kind === 'letterhead') {
        const m = letterhead(T, doc);
        plays.push(m.play);
        const head = T.h('div', { class: 'lt-head' }, m.el, T.h('h1', { class: 'lt-heading', translate: 'no' }, HEADING));
        kids.push(head); lines.push(head.lastChild);
      } else if (b.kind === 'step') {
        const li = T.h('li', { class: 'lt-step' }, T.h('span', { class: 'lt-step-n', 'aria-hidden': 'true' }, String(++n)), T.h('span', null, words(b)));
        listOf('ol', 'lt-steps').append(li); lines.push(li);
      } else if (b.kind === 'item') {
        const li = T.h('li', { class: 'lt-item' }, T.iconEl(b.icon, 18), T.h('span', null, words(b)));
        listOf('ul', 'lt-items').append(li); lines.push(li);
      } else if (b.kind === 'sign') {
        const f = flourish(T, doc);
        plays.push(f.play);
        const s = T.h('div', { class: 'lt-sign' }, T.h('span', { class: 'lt-sign-n', translate: 'no' }, SIGNATURE), f.el);
        kids.push(s); lines.push(s.firstChild);
      } else {
        const cls = { hello: 'lt-hello', p: 'lt-p', note: 'lt-note', wish: 'lt-wish' }[b.kind] || 'lt-p';
        const el = T.h('p', { class: cls }, words(b));
        kids.push(el); lines.push(el);
      }
    }
    const el = T.h('article', { class: `lt-page lt-pg-${p.name}` }, ...kids);
    return { el, lines, play: () => plays.forEach(f => f()) };
  }

  /* -- the letter -------------------------------------------------------------------------- */
  let cur = null;
  function open() {
    if (cur) return cur;
    if (state.unread) { state.markRead(); label(); putAway(); }
    cur = build();
    return cur;
  }

  function build() {
    const L = { i: 0, turn: null };
    const panel = T.h('div', { class: 'letter', role: 'dialog', 'aria-modal': 'true' });
    T.ti(panel, 'letter.aria', null, { attr: 'aria-label' });
    const stack = T.h('div', { class: 'lt-stack', 'aria-hidden': 'true', 'data-left': '0' }, T.h('i'), T.h('i'));
    const sheet = T.h('div', { class: 'lt-sheet' });
    const shade = T.h('div', { class: 'lt-shade', 'aria-hidden': 'true' });
    const x = T.h('button', { class: 'icon-btn lt-x', type: 'button', 'data-tip-kbd': 'Esc' }, T.iconEl('close', 16));
    T.ti(x, 'common.action.close', null, { attr: 'aria-label' });
    T.ti(x, 'common.action.close', null, { attr: 'data-tip' });
    const paper = T.h('div', { class: 'lt-paper' }, sheet, shade, x);
    const prev = T.h('button', { class: 'icon-btn lt-prev', type: 'button' }, T.iconEl('chevron-left', 16));
    T.ti(prev, 'letter.nav.prev', null, { attr: 'aria-label' });
    T.ti(prev, 'letter.nav.prev', null, { attr: 'data-tip' });
    const dots = PAGES.map((p, i) => {
      const d = T.h('button', { class: 'lt-dot', type: 'button', 'data-i': String(i) });
      T.ti(d, 'letter.page', { n: i + 1, total: PAGES.length }, { attr: 'aria-label' });
      return d;
    });
    const nextLab = T.h('span', { class: 'lab' });
    const next = T.h('button', { class: 'btn btn-sm lt-next', type: 'button' }, nextLab, T.iconEl('chevron', 15));
    const live = T.h('span', { class: 'sr-only', 'aria-live': 'polite' });
    const nav = T.h('div', { class: 'lt-nav' }, prev, T.h('div', { class: 'lt-dots' }, ...dots), next, live);
    panel.append(stack, paper, nav);

    const last = () => L.i === PAGES.length - 1;
    function syncNav(first) {
      prev.disabled = L.i === 0;
      dots.forEach((d, i) => d.setAttribute('aria-current', String(i === L.i)));
      const word = t(last() ? 'letter.nav.done' : 'letter.nav.next');
      if (first || M.reduced) nextLab.textContent = word; else if (nextLab.textContent !== word) M.swapText(nextLab, word);
      next.classList.toggle('btn-primary', last());
      stack.dataset.left = String(Math.min(2, PAGES.length - 1 - L.i));
      if (!first) live.textContent = t('letter.page', { n: L.i + 1, total: PAGES.length });
    }
    // the paper is as tall as this language's longest page (one height for every page, so a page
    // turns onto a sheet of the same size), within what the window allows (letter.css --lt-h)
    function fit() {
      const probe = T.h('div', { class: 'lt-probe', 'aria-hidden': 'true' });
      paper.append(probe);
      let h = 0;
      for (let i = 0; i < PAGES.length; i++) { const pg = pageNode(i); probe.replaceChildren(pg.el); h = Math.max(h, pg.el.scrollHeight); }
      probe.remove();
      if (h > 0) panel.style.setProperty('--lt-fit', `${Math.ceil(h) + 8}px`);
    }
    // the button under the paper keeps one width for 下一页 and 合上 (the row never shifts)
    function steadyNext() {
      next.style.removeProperty('min-width');
      const keep = [nextLab.textContent, next.classList.contains('btn-primary')];
      let w = 0;
      for (const [word, primary] of [[t('letter.nav.next'), false], [t('letter.nav.done'), true]]) {
        nextLab.textContent = word; next.classList.toggle('btn-primary', primary); w = Math.max(w, next.offsetWidth);
      }
      nextLab.textContent = keep[0]; next.classList.toggle('btn-primary', keep[1]);
      if (w > 0) next.style.setProperty('min-width', `${Math.ceil(w)}px`);
    }
    function show(pg, delay) {
      M.stagger(pg.lines, { base: delay, step: 34, y: 5, dur: 280, max: 8 });
      pg.play();
    }

    function go(to) {
      to = Math.max(0, Math.min(PAGES.length - 1, to));
      if (to === L.i) return;
      if (L.turn) L.turn.end();
      const dir = to > L.i ? 1 : -1;
      const pg = pageNode(to);
      L.i = to;
      syncNav(false);
      const old = sheet.firstElementChild;
      if (M.reduced || !old) { sheet.replaceChildren(pg.el); pg.play(); return; }
      // leafing through a stack of letter paper (user 11-05: 页面向左偏转然后消失): the sheet in hand
      // tilts to the left and slides away, the next sheet is already under it; going back, the sheet
      // before comes in from the left and settles on top. 2D only: a turn, a slide, a fade.
      const AWAY = [
        { transform: 'none', opacity: 1 },
        { transform: 'translate(-17%, 4px) rotate(-6deg)', opacity: 1, offset: 0.58 },
        { transform: 'translate(-44%, 18px) rotate(-12deg)', opacity: 0 },
      ];
      if (dir > 0) {
        const leaf = T.h('div', { class: 'lt-leaf', 'aria-hidden': 'true' }, old);
        sheet.replaceChildren(pg.el);
        paper.insertBefore(leaf, x);
        const a = M.anim(leaf, AWAY, { duration: TURN_MS, easing: 'cubic-bezier(.4,.1,.3,1)', fill: 'forwards' });
        M.anim(shade, [{ opacity: 0.05 }, { opacity: 0 }], { duration: TURN_MS, easing: M.ease.out });
        show(pg, 140);
        const end = () => { if (L.turn !== turn) return; L.turn = null; leaf.remove(); };
        const turn = { end };
        L.turn = turn;
        M.onDone(a, end, TURN_MS + 400);
      } else {
        // the sheet before comes back from the left onto the one in hand, which stays under it
        pg.el.setAttribute('aria-hidden', 'true');
        const leaf = T.h('div', { class: 'lt-leaf', 'aria-hidden': 'true' }, pg.el);
        paper.insertBefore(leaf, x);
        const a = M.anim(leaf, AWAY.slice().reverse().map((k, i) => ({ ...k, offset: [0, 0.42, 1][i] })),
          { duration: RETURN_MS, easing: 'cubic-bezier(.2,.7,.25,1)', fill: 'forwards' });
        M.anim(shade, [{ opacity: 0 }, { opacity: 0.05 }], { duration: RETURN_MS, easing: 'ease-in' });
        pg.play();
        const end = () => {
          if (L.turn !== turn) return;
          L.turn = null;
          pg.el.removeAttribute('aria-hidden');
          sheet.replaceChildren(pg.el);
          leaf.remove();
        };
        const turn = { end };
        L.turn = turn;
        M.onDone(a, end, RETURN_MS + 400);
      }
    }

    const first = pageNode(0);
    sheet.append(first.el);
    syncNav(true);
    const entry = T.overlays.open(panel, {
      inT: 'translateY(16px) scale(.985)', outT: 'translateY(8px) scale(.99)',
      onClose: () => { if (L.turn) L.turn.end(); offLang(); cur = null; },
    });
    fit();
    steadyNext();
    // the sheets under the paper slide out once it has landed; the first page writes itself in
    win.requestAnimationFrame(() => { stack.dataset.left = String(Math.min(2, PAGES.length - 1)); });
    show(first, 160);

    x.addEventListener('click', () => entry.cancel());
    prev.addEventListener('click', () => go(L.i - 1));
    next.addEventListener('click', () => { if (last()) entry.close(true); else go(L.i + 1); });
    dots.forEach((d, i) => d.addEventListener('click', () => go(i)));
    panel.addEventListener('keydown', e => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const k = e.key;
      const to = k === 'ArrowRight' || k === 'PageDown' ? L.i + 1 : k === 'ArrowLeft' || k === 'PageUp' ? L.i - 1
        : k === 'Home' ? 0 : k === 'End' ? PAGES.length - 1 : null;
      if (to == null) return;
      e.preventDefault();
      go(to);
    });
    // a language switch rewrites the page in hand at once (the chrome's words are bound by T.ti)
    const offLang = T.on('i18n:change', () => {
      if (L.turn) L.turn.end();
      sheet.replaceChildren(pageNode(L.i).el);
      nextLab.textContent = t(last() ? 'letter.nav.done' : 'letter.nav.next');
      fit();
      steadyNext();
    });
    win.setTimeout(() => { if (panel.isConnected && !panel.contains(doc.activeElement)) next.focus({ preventScroll: true }); }, 30);
    return { close: () => entry.close(false), go, get page() { return L.i; } };
  }

  btn.addEventListener('click', () => open());
  if (T.cmd && T.cmd.register) {
    T.cmd.register([{ id: 'letter-open', group: 'go', label: () => t('letter.palette.label'), icon: 'mail',
      keywords: () => `letter guide help ${t('letter.palette.keywords')}`, run: () => open() }]);
  }
  return { open, get unread() { return state.unread; }, get letter() { return cur; } };
}
