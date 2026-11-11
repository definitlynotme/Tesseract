// Notices: the toast queue and the shell's host:notify (T-P4-06; P4 architecture section 2.9,
// mapping_core N1-N4, R7, G57; PROTOCOL section 13 "host:notify and its receipt").
//
// Toast queue (createToastQueue): at most `max` (5) toasts on screen at once; from the 6th on
// the rest wait, merged into one row "N more notices" (view.merge(n)); a toast that leaves
// makes room for the oldest waiting one. pause(reason) / resume(reason) hold every countdown
// (the window is hidden: presence.visible=false or document.hidden); a toast shown while held
// starts held.
//   view.toast(o, {onClose}) -> handle {el, dismiss(instant?), pause(reason), resume(reason)};
//        calls onClose() once, as soon as the toast starts to leave
//   view.merge(n)            the merge row: n waiting notices (0 removes it)
//
// host:notify (createHostNotify): severity info -> a toast; warn -> a warning banner; error /
// critical -> an error banner. title / detail are untrusted text (rendered as text nodes by
// core.js, never as HTML). A notice with a target gets a "View" action (same mapping as
// host:navigate). The receipt host_notify_rendered{delivery_id} goes out only once the notice
// is really on screen: its element is in the DOM and the window is visible. While the window is
// hidden the notice waits and is rendered when it shows again; one that is not on screen within
// RECEIPT_MS (3 s) of its arrival gets no receipt (the shell records it as dropped). At most
// WAITING_CAP (50) notices wait while hidden: the oldest beyond that is dropped (never shown, no
// receipt; the shell keeps it as dropped). Receipts are
// for the page connection that received the notice; after a reconnect none is sent for older
// ones. Every receipt error (not_allowed, unknown_delivery, already_receipted, stale_page, ...)
// is silent. A delivery_id seen before in this connection is ignored.
//
// 10-30 notice redesign (.build/提醒模块重设计_20261030.md §4.1, §10.4): a frame may carry a
// `notice` object {kind, incident, state, group, title_id, detail_id?, reason_id?, fix_id?, params,
// action?, sticky, ttl_ms}. Then the words are catalog messages (T.ti with the params; the quoted
// reason in the voice, then ` · ` and the fix), title / detail are only the fallback for an id
// the catalogs lack, and the notice's id is `notice:<incident>`, so a later frame of the
// incident replaces it in place:
//   open / update  info → a toast, warn → a warning banner, error / critical → an error banner
//                  (an incident that changes severity moves between them: the other goes)
//   resolved       the incident's banner goes, a success toast says it recovered
//   closed         the incident's banner or toast goes silently, at once even while hidden; no
//                  receipt (the shell receipted it already; also the shell's own clear frames)
// On the overview, an open / update of the connection or tier_fallback groups is not shown (the
// hero already shows that state) but is receipted with {shown: false} (the shell plays no sound).
// A new sidecar (rt:reset with generation_changed) takes every notice banner and toast down.
// A frame whose notice is malformed renders as before, from its title / detail.

export const MAX_VISIBLE = 5;
export const WAITING_CAP = 50;
export const RECEIPT_MS = 3000;
export const MAX_HOST_BANNERS = 5;
export const SEVERITY_KIND = Object.freeze({ info: 'info', warn: 'warning', error: 'error', critical: 'error' });
export const NOTICE_STATES = Object.freeze(['open', 'update', 'resolved', 'closed']);
/** The groups the overview's hero already shows (an open / update of them is not shown there). */
export const HERO_GROUPS = Object.freeze(['connection', 'tier_fallback']);
const MESSAGE_ID = /^[a-z0-9_]+(\.[a-z0-9_]+)+$/;
const messageId = v => (typeof v === 'string' && v.length <= 96 && MESSAGE_ID.test(v) ? v : null);

/** The frame's notice object as the page uses it, or null (absent or malformed: the old rendering). */
export function noticeOf(f) {
  const n = f && f.notice;
  if (!n || typeof n !== 'object' || Array.isArray(n)) return null;
  if (typeof n.incident !== 'string' || !n.incident || !NOTICE_STATES.includes(n.state)) return null;
  const titleId = messageId(n.title_id);
  if (!titleId && n.state !== 'closed') return null;
  const params = {};
  if (n.params && typeof n.params === 'object' && !Array.isArray(n.params)) {
    for (const [k, v] of Object.entries(n.params)) if (typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v))) params[k] = v;
  }
  return {
    incident: n.incident, state: n.state, group: typeof n.group === 'string' ? n.group : '', titleId,
    detailId: messageId(n.detail_id), reasonId: messageId(n.reason_id), fixId: messageId(n.fix_id), params,
    action: n.action === 'view', sticky: n.sticky === true, ttl: Number.isFinite(n.ttl_ms) && n.ttl_ms > 0 ? n.ttl_ms : 0,
  };
}

export function createToastQueue({ view, max = MAX_VISIBLE, cap = WAITING_CAP }) {
  const shown = []; // {ticket, handle}
  const waiting = [];
  const held = new Set();

  const sync = () => view.merge(waiting.length);

  function show(ticket) {
    const entry = { ticket, handle: null };
    shown.push(entry);
    ticket.shown = true;
    entry.handle = view.toast(ticket.o, { onClose: () => leave(entry) });
    ticket.handle = entry.handle;
    for (const r of held) entry.handle.pause(r);
    if (ticket.onShown) { try { ticket.onShown(entry.handle); } catch (e) { setTimeout(() => { throw e; }); } }
  }

  function leave(entry) {
    const i = shown.indexOf(entry);
    if (i < 0) return;
    shown.splice(i, 1);
    entry.ticket.closed = true;
    promote();
  }

  function promote() {
    while (shown.length < max && waiting.length) show(waiting.shift());
    sync();
  }

  const api = {
    // o: the T.notify options of a toast; onShown(handle) runs when it is rendered (maybe later)
    push(o, { onShown } = {}) {
      const ticket = {
        o, onShown, shown: false, closed: false, handle: null,
        dismiss(instant) {
          if (ticket.closed) return;
          if (ticket.shown) { ticket.handle.dismiss(instant); return; }
          const i = waiting.indexOf(ticket);
          if (i >= 0) waiting.splice(i, 1);
          ticket.closed = true;
          sync();
        },
      };
      // the same id replaces the notice it names (a word updated in place, no second row)
      if (o && o.id != null) {
        for (const e of shown.slice()) if (e.ticket.o && e.ticket.o.id === o.id) e.handle.dismiss(true);
        for (const w of waiting.slice()) if (w.o && w.o.id === o.id) w.dismiss();
      }
      if (shown.length < max && !waiting.length) show(ticket);
      else {
        waiting.push(ticket);
        while (waiting.length > cap) { const old = waiting.shift(); old.closed = true; }
      }
      sync();
      return ticket;
    },
    pause(reason = 'hidden') {
      if (held.has(reason)) return;
      held.add(reason);
      for (const e of shown) e.handle.pause(reason);
    },
    resume(reason = 'hidden') {
      if (!held.delete(reason)) return;
      for (const e of shown) e.handle.resume(reason);
    },
    // the merge row's close: the waiting notices are dropped
    dropWaiting() { for (const w of waiting.splice(0)) w.closed = true; sync(); },
    // the toast of this id goes (on screen: it leaves; waiting: it never shows)
    dismissId(id) {
      for (const e of shown.slice()) if (e.ticket.o && e.ticket.o.id === id) e.handle.dismiss();
      for (const w of waiting.slice()) if (w.o && w.o.id === id) w.dismiss();
    },
    clear(instant) {
      for (const w of waiting.splice(0)) w.closed = true;
      for (const e of shown.slice()) e.handle.dismiss(instant);
      sync();
    },
    get newest() { return shown.length ? shown[shown.length - 1].handle : null; },
    get shown() { return shown.length; },
    get waiting() { return waiting.length; },
    get paused() { return held.size > 0; },
  };
  return api;
}

// Ports:
//   toast(o, {onShown}) -> ticket           the toast queue's push
//   banner(o) -> handle {el, dismiss()}     core.js T.notify for warning / error
//   dismiss(id)                             core.js T.dismiss (a banner by id)
//   visible() -> bool                       presence.visible
//   onVisible(fn) -> off                    fn(visible) on every change
//   clock {now()}                           T.clock
//   host(name, args) -> Promise             a shell invoke (errors are the caller's to ignore)
//   go(target)                              host:navigate's mapping (navigate.js)
//   t(id, params) -> string                 T.t
//   diag(entry)                             optional: a diagnostic note
// 10-30 notices (optional; without them a notice frame still works, from its title / detail):
//   dropToast(id)                           the toast queue's dismissId (a toast by id)
//   page() -> string | null                 the router's current page id (T.router.current)
//   has(id) -> bool                         a catalog has the message id (T.i18n.hasAny)
//   message(notice, fallback) -> Node | string | null
//                                           the notice's body: the detail, then on its own line the
//                                           quoted reason (the voice) · the fix (built with T.ti)
export function createHostNotify({ toast, banner, dismiss = () => {}, visible, onVisible, clock, host, go, t, diag = () => {},
  dropToast = () => {}, page = () => null, has = () => false, message = (n, fallback) => fallback || null }) {
  let conn = 0;
  let seen = new Set();
  const waiting = [];
  const banners = []; // host notice banners, oldest first
  const notices = new Set(); // notice:<incident> ids shown (banners and toasts)

  function inDom(h) { return !!(h && h.el && h.el.isConnected !== false); }

  // shown: false confirms a notice the page deliberately did not show (the overview's hero)
  function receipt(item, handle, shown = true) {
    if (item.receipted) return;
    if (item.conn !== conn) { diag({ kind: 'notify_receipt_skipped', why: 'reconnected' }); return; }
    if (clock.now() - item.at > RECEIPT_MS) { diag({ kind: 'notify_receipt_skipped', why: 'late' }); return; }
    if (!visible() || (shown && !inDom(handle))) { diag({ kind: 'notify_receipt_skipped', why: 'not_on_screen' }); return; }
    item.receipted = true;
    Promise.resolve(host('host_notify_rendered', shown ? { delivery_id: item.id } : { delivery_id: item.id, shown: false })).catch(() => {});
  }

  function keepBanner(id) {
    if (!banners.includes(id)) banners.push(id);
    while (banners.length > MAX_HOST_BANNERS) dismiss(banners.shift());
  }
  function forget(id) {
    const i = banners.indexOf(id);
    if (i >= 0) banners.splice(i, 1);
    notices.delete(id);
  }
  // an incident's banner and toast go (silently)
  function takeDown(incident) {
    const id = `notice:${incident}`;
    dismiss(id);
    dropToast(id);
    forget(id);
  }

  function renderNotice(item, n) {
    const f = item.frame;
    const id = `notice:${n.incident}`;
    if (n.state !== 'resolved' && HERO_GROUPS.includes(n.group) && page() === 'overview') {
      diag({ kind: 'notify_on_hero', group: n.group });
      receipt(item, null, false);
      return;
    }
    const known = has(n.titleId);
    const actions = [];
    if (n.action && typeof f.target === 'string' && go.can(f.target)) {
      actions.push({ label: t('notice.action.view'), labelId: 'notice.action.view', keep: false, run: () => go(f.target) });
    }
    const body = message(n, f.detail);
    const o = Object.assign({ id, actions, message: body || '' },
      known ? { titleId: n.titleId, titleParams: n.params } : { title: f.title || '', data: true });
    const kind = n.state === 'resolved' ? 'success' : SEVERITY_KIND[f.severity];
    if (kind === 'success' || kind === 'info') {
      // a toast (the same id replaces it in the queue); a banner of the incident goes
      dismiss(id);
      forget(id);
      notices.add(id);
      const timing = kind === 'success' ? { timeout: n.ttl || 6000 } : n.sticky ? { sticky: true } : n.ttl ? { timeout: n.ttl } : {};
      toast(Object.assign(o, { kind }, timing), { onShown: h => receipt(item, h) });
      return;
    }
    dropToast(id);
    notices.add(id);
    const h = banner(Object.assign(o, { kind }));
    keepBanner(id);
    receipt(item, h);
  }

  function render(item) {
    const f = item.frame;
    if (f.notice) { renderNotice(item, f.notice); return; }
    const kind = SEVERITY_KIND[f.severity];
    const actions = [];
    // (the View label is a catalog message, relabelled in place; title / detail are data to the
    // page, already in the shell's language: data -> translate="no")
    if (typeof f.target === 'string' && go.can(f.target)) actions.push({ label: t('shell.notify.view'), labelId: 'shell.notify.view', keep: false, run: () => go(f.target) });
    const o = { kind, title: f.title || '', message: f.detail || '', actions, data: true };
    if (kind === 'info') {
      toast(o, { onShown: h => receipt(item, h) });
      return;
    }
    const id = `host-notify:${item.id}`;
    const h = banner(Object.assign(o, { id }));
    keepBanner(id);
    receipt(item, h);
  }

  function flush() {
    while (waiting.length && visible()) render(waiting.shift());
  }

  if (typeof onVisible === 'function') onVisible(v => { if (v) flush(); });

  return {
    // host:notify{delivery_id, severity, title, detail, target, notice?}
    receive(f) {
      if (!f || typeof f.delivery_id !== 'string' || !f.delivery_id) { diag({ kind: 'notify_bad_frame' }); return; }
      if (!Object.prototype.hasOwnProperty.call(SEVERITY_KIND, f.severity)) { diag({ kind: 'notify_bad_frame' }); return; }
      if (seen.has(f.delivery_id)) { diag({ kind: 'notify_duplicate' }); return; }
      seen.add(f.delivery_id);
      const notice = noticeOf(f);
      if (f.notice !== undefined && !notice) diag({ kind: 'notify_notice_invalid' });
      if (notice && notice.state === 'closed') {
        // at once, even while hidden; a waiting frame of the incident never shows
        takeDown(notice.incident);
        for (let i = waiting.length - 1; i >= 0; i--) if (waiting[i].frame.notice && waiting[i].frame.notice.incident === notice.incident) waiting.splice(i, 1);
        return;
      }
      const item = {
        id: f.delivery_id, conn, at: clock.now(), receipted: false,
        frame: { severity: f.severity, title: typeof f.title === 'string' ? f.title : '', detail: typeof f.detail === 'string' ? f.detail : '', target: f.target, notice },
      };
      if (visible()) { render(item); return; }
      waiting.push(item);
      while (waiting.length > WAITING_CAP) { waiting.shift(); diag({ kind: 'notify_dropped', why: 'waiting_cap' }); }
    },
    // a new page connection (ready.js: an rt_connect, not an rt:reset{runtime_gone} inside one):
    // earlier delivery ids are no longer this connection's; the notices still waiting are shown
    // later without a receipt
    connected() { conn++; seen = new Set(); },
    // a new sidecar (rt:reset{generation_changed}): its incidents are over; their banners and
    // toasts go, and the waiting notices of the old sidecar never show
    epoch() {
      for (const id of [...notices]) { dismiss(id); dropToast(id); forget(id); }
      for (let i = waiting.length - 1; i >= 0; i--) if (waiting[i].frame.notice) waiting.splice(i, 1);
    },
    get waiting() { return waiting.length; },
  };
}
