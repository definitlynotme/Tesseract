// The shell-page contract on the page side (T-P4-06; P4 architecture section 2.9; PROTOCOL
// section 13, the shell-page contract; shell/README.md T-P3-04 / T-P3-05 page contracts).
//
// installShellc(T, opts) wires the modules of this folder to the page (boot/shellc.js calls it
// once, after core.js and the rt client, before the pages):
//   presence.js   host:visibility + document.visibilityState -> store `presence`; pauses the
//                 scope timers (core.js T.scopes), chart redraws and toast countdowns while hidden
//   notify.js     the toast queue (5 at once, the rest merged) and host:notify with its receipt
//   banners.js    shell-level banners (sidecar, startup / stop failure, cleanup, recovery, stale
//                 lists, command errors)
//   quit.js       host:confirmQuit -> host_confirm_quit{token, unsaved}; T.shellc.registerDraft
//   navigate.js   host:navigate -> T.go; host_set_page on every page change and connection
//   language.js   hello.lang / host:language -> T.i18n.set (once per language)
//   ready.js      host_notify_ready (then host_set_page) on every connection; host_ready once
//   launch.js     the launch screen while the runtime starts (opts.launch: the app target only)
//   frame.js      the in-page title bar's window buttons (#wc): host_window{action}; host:window
//                 {maximized} picks the maximize or restore glyph (11-05)
//   update.js     the new-version banner (once per version) and the "update finished" notice
// T.shellc (the page API): registerDraft(fn), presence, errorText(err), commandError(err),
// run(cmd, args) -> Promise (T.rt.call with the generic error banner), openFolder(kind),
// listStale(topic, key), reload(topic, key) (-> T.rt.requestResync, never rejects), unsaved(), diag().
//
// opts: win, doc (default window / document), allowHost(name) -> bool (default: every shell
// invoke of HOST_INVOKES; the design target's fixture mode narrows it to what the scenario
// registered, see boot/shellc.js), storage (default window.localStorage, null when unavailable),
// launch (default false; true mounts the launch screen, 11-05 window-first launch).
// A name outside HOST_INVOKES is refused in every mode (host_unknown).
import { createBanners } from './banners.js';
import { createFrame } from './frame.js';
import { createLanguage } from './language.js';
import { createNavigate } from './navigate.js';
import { createLaunch } from './launch.js';
import { createHostNotify, createToastQueue } from './notify.js';
import { createPresence } from './presence.js';
import { createQuit, drafts, registerDraft } from './quit.js';
import { createReady } from './ready.js';
import { createUpdateNotice } from './update.js';

const DIAG_MAX = 100;
export const READY_FRAME_FALLBACK_MS = 100;
// The page invokes the shell registers (PROTOCOL section 13 host:caps.invokes, the ten names;
// 11-05 host_window: the window buttons of the in-page title bar; the debug build's dev_quit is
// not for pages). host() refuses any other name before it reaches
// the shell or the fixture narrowing: a misspelt name is noted (host_unknown) and rejected with
// not_allowed, never skipped silently (T-P4-06 review B SHOULD-4, 8th-wave ruling T-P4-H2 item 2).
export const HOST_INVOKES = Object.freeze(['rt', 'rt_connect', 'rt_resync', 'host_notify_ready', 'host_notify_rendered',
  'host_confirm_quit', 'host_set_page', 'host_force_restart', 'host_ready', 'host_window']);

function localStore(win) {
  try { const s = win && win.localStorage; return s && typeof s.getItem === 'function' ? s : null; } catch (e) { return null; }
}

export function installShellc(T, opts = {}) {
  const win = opts.win !== undefined ? opts.win : (typeof window !== 'undefined' ? window : null);
  const doc = opts.doc !== undefined ? opts.doc : (win && win.document) || null;
  const allowHost = typeof opts.allowHost === 'function' ? opts.allowHost : () => true;
  const storage = opts.storage !== undefined ? opts.storage : localStore(win);
  const S = T.store;
  const ring = [];
  const note = e => { ring.push(e); if (ring.length > DIAG_MAX) ring.splice(0, ring.length - DIAG_MAX); };
  const t = (id, params) => (typeof T.t === 'function' ? T.t(id, params) : id);
  const rt = () => T.rt || null;

  // one shell invoke; every rejection is turned into a note here and rethrown for the caller
  function host(name, args) {
    if (!HOST_INVOKES.includes(name)) {
      note({ kind: 'host_unknown', name: String(name) });
      return Promise.reject(Object.assign(new Error('not_allowed'), { code: 'not_allowed' }));
    }
    if (!allowHost(name)) { note({ kind: 'host_skipped', name }); return Promise.resolve(null); }
    const r = rt();
    if (!r || typeof r.invokeHost !== 'function') { note({ kind: 'host_error', name, code: 'runtime_unavailable' }); return Promise.reject(Object.assign(new Error('runtime_unavailable'), { code: 'runtime_unavailable' })); }
    note({ kind: 'host', name });
    let p;
    try { p = Promise.resolve(r.invokeHost(name, args)); } catch (e) { p = Promise.reject(e); }
    return p.catch(e => { note({ kind: 'host_error', name, code: (e && e.code) || 'internal' }); throw e; });
  }
  // one slot's snapshot again (the list-stale banner's Reload, T.shellc.reload): through the rt
  // layer's T.rt.requestResync (its in-flight dedupe and 1 s page throttle, T-P4-H2 fix round 1),
  // never rejects; host('rt_resync') only when the rt layer has no requestResync
  function resync(topic, key) {
    if (!allowHost('rt_resync')) { note({ kind: 'host_skipped', name: 'rt_resync' }); return Promise.resolve(null); }
    const r = rt();
    if (r && typeof r.requestResync === 'function') {
      note({ kind: 'host', name: 'rt_resync', via: 'rt' });
      return Promise.resolve(r.requestResync(topic, key || undefined)).catch(() => null);
    }
    return host('rt_resync', key ? { topic, key } : { topic }).catch(() => null);
  }
  const call = (cmd, args) => {
    const r = rt();
    if (!r) return Promise.reject(Object.assign(new Error('runtime_unavailable'), { code: 'runtime_unavailable' }));
    return r.call(cmd, args || {});
  };
  const can = cmd => { const r = rt(); return !!(r && r.can(cmd)); };
  const errorText = err => { const r = rt(); return r ? r.errorText(err) : t('errors.unknown'); };
  const reasonText = (ns, code) => { const r = rt(); return r ? r.reasonText(ns, code) : t('common.reason_unknown'); };
  const topic = (name, key) => { const r = rt(); return r ? r.topic(name, key) : { get: () => null, sub: () => () => {} }; };
  const go = (page, params) => (typeof T.go === 'function' ? T.go(page, params) : false);
  // a banner still on screen (core.js keeps the banners by id; without them every banner counts
  // as shown, as before)
  const onScreen = id => (T.notices && T.notices.banners && typeof T.notices.banners.has === 'function' ? T.notices.banners.has(id) : true);
  // the router's page and its params (before the first page:change reaches navigate.js)
  const currentPage = () => {
    const id = T.router && T.router.current;
    if (!id) return null;
    const p = T.router.pages && typeof T.router.pages.get === 'function' ? T.router.pages.get(id) : null;
    return { id, params: (p && p.ctx && p.ctx.params) || {} };
  };

  // ---- presence ---------------------------------------------------------------------------
  const presence = createPresence({ store: S, on: T.on, doc });

  // ---- the title bar's window buttons (11-05) ------------------------------------------------
  const windowFrame = createFrame({
    doc, on: T.on, host,
    label: (el, id) => { if (typeof T.ti === 'function') { T.ti(el, id, null, { attr: 'aria-label' }); T.ti(el, id, null, { attr: 'data-tip' }); } },
  });

  // ---- notices ----------------------------------------------------------------------------
  const port = T.notices && T.notices.port;
  const queue = port ? createToastQueue({ view: { toast: port.toast, merge: n => port.merge(n, () => queue.dropWaiting()) } }) : null;
  if (queue && typeof T.notices.useQueue === 'function') T.notices.useQueue(queue);
  // a host:navigate never closes an open dialog or drawer, nor drops a registered draft
  const busy = () => !!((typeof T.overlayOpen === 'function' && T.overlayOpen()) || drafts.count() > 0);
  const nav = createNavigate({ go, host, current: currentPage, busy, diag: note });
  // 10-30 notices: the body of a notice (notify.js) is its detail, then on its own line the quoted
  // reason in the voice, ` · `, the fix; catalog messages through T.ti (relabelled in place), a
  // fallback detail as data (translate="no"); text nodes only (DESIGN §5.7)
  const hasId = id => !!id && !!(T.i18n && typeof T.i18n.hasAny === 'function' && T.i18n.hasAny(id));
  function noticeMessage(n, fallback) {
    if (!doc || typeof T.ti !== 'function') return fallback || null;
    const frag = doc.createDocumentFragment();
    const span = (cls, id) => { const el = doc.createElement('span'); if (cls) el.className = cls; if (id) T.ti(el, id, n.params); return el; };
    if (n.detailId && hasId(n.detailId)) frag.appendChild(span('', n.detailId));
    else if (fallback && (n.detailId || !n.titleId)) { const el = span(''); el.textContent = fallback; el.setAttribute('translate', 'no'); frag.appendChild(el); }
    const reason = hasId(n.reasonId), fix = hasId(n.fixId);
    if (reason || fix) {
      if (frag.childNodes.length) frag.appendChild(doc.createElement('br'));
      if (reason) frag.appendChild(span('t-voice', n.reasonId));
      if (reason && fix) frag.appendChild(doc.createTextNode(' · '));
      if (fix) frag.appendChild(span('', n.fixId));
    }
    return frag.childNodes.length ? frag : null;
  }
  const hostNotify = createHostNotify({
    toast: (o, hooks) => (queue ? queue.push(o, hooks) : (hooks && hooks.onShown && hooks.onShown(T.notify(o)), null)),
    banner: o => T.notify(o), dismiss: id => T.dismiss(id),
    visible: () => presence.visible, onVisible: fn => presence.onChange(fn),
    clock: T.clock || { now: () => Date.now() }, host, go: nav.goTarget, t, diag: note,
    dropToast: id => { if (queue) queue.dismissId(id); },
    page: () => (T.router && typeof T.router.current === 'string' ? T.router.current : null),
    has: hasId, message: noticeMessage,
  });

  function setPaused(hidden) {
    if (queue) { if (hidden) queue.pause('hidden'); else queue.resume('hidden'); }
    if (T.scopes) { if (hidden) T.scopes.pause(); else T.scopes.resume(); }
  }
  presence.onChange(v => setPaused(!v));
  if (!presence.visible) setPaused(true);

  // ---- banners ----------------------------------------------------------------------------
  const banners = createBanners({
    notify: o => T.notify(o), dismiss: id => T.dismiss(id), t, errorText, reasonText, store: S, call, can, host, resync, go,
    serviceState: () => topic('service.state').get(), topicSub: (name, key, fn) => topic(name, key).sub(fn),
    emit: (name, payload) => T.emit(name, payload), epoch: () => { const r = rt(); return r ? r.epoch : null; },
    onScreen,
  });

  // ---- quit, language, update -------------------------------------------------------------
  const dirtyCount = () => (T.ui && T.ui.saveBar && typeof T.ui.saveBar.dirtyCount === 'function' ? T.ui.saveBar.dirtyCount() : 0);
  const quit = createQuit({ host, dirtyCount, drafts });
  const language = createLanguage({ store: S, on: T.on, i18n: T.i18n });
  const update = createUpdateNotice({
    notify: o => T.notify(o), dismiss: id => T.dismiss(id), t, can, call,
    confirm: o => (typeof T.confirm === 'function' ? T.confirm(o) : Promise.resolve(false)), go, storage,
    commandError: err => banners.commandError(err), onScreen,
  });

  // ---- connections and the ready signal ---------------------------------------------------
  // (the page is named before host_ready can follow the same page:change)
  T.on('page:change', e => nav.pageChanged(e));
  let entered = false;
  const frame = fn => {
    let done = false;
    const once = () => { if (!done) { done = true; fn(); } };
    if (win && typeof win.requestAnimationFrame === 'function') win.requestAnimationFrame(once);
    setTimeout(once, READY_FRAME_FALLBACK_MS); // a hidden or cloaked window may skip frames
  };
  // 11-05 window-first launch: the shell shows the window while the runtime starts; until the
  // page shows real state (the same condition as host_ready's) the launch screen covers it
  const showsState = () => !!topic('service.state').get() && entered;
  const launch = opts.launch && doc && doc.body ? createLaunch({
    doc, win, ready: showsState,
    reduced: () => !!(T.motion && T.motion.reduced),
    visible: () => presence.visible,
    settle: (T.motion && T.motion.SP && T.motion.SP.settle) || undefined,
    // (never during createLaunch: the launch shows from a timer or an rt event, after `ready` exists)
    onShown: () => ready.check(),
    onGone: () => note({ kind: 'launch_gone' }),
  }) : null;
  const ready = createReady({
    host,
    // a new page connection (rt_connect): receipts and the page name start over
    onConnected: () => { hostNotify.connected(); nav.connected(); },
    hasSnapshot: () => !!topic('service.state').get(),
    onSnapshot: fn => topic('service.state').sub(fn),
    pageEntered: () => entered,
    onPage: fn => T.on('page:change', () => { entered = true; fn(); }),
    frame,
    launchShown: () => !!launch && launch.shown,
  });
  if (launch) {
    // the replay ended: without a service.state snapshot the runtime is still starting, so the
    // launch is shown and the window may be revealed (ready.check)
    T.on('rt:replayed', () => launch.replayed());
    T.on('rt:caps', () => launch.hello());
    topic('service.state').sub(() => launch.check());
    T.on('page:change', () => launch.check());
  }

  // ---- events -----------------------------------------------------------------------------
  T.on('rt:conn', e => ready.conn(e));
  // (ready.reset() declares the first rt:reset of a page connection and every rt:reset{reconnect}:
  // an rt:reset{runtime_gone} inside a connection is a new sidecar, not a new page connection; the
  // shell's page_generation is the same)
  // (a new sidecar, generation_changed: the notices of the old one go, notify.js epoch())
  T.on('rt:reset', e => { ready.reset(e); banners.reset(e); if (e && e.generation_changed) hostNotify.epoch(); });
  T.on('rt:caps', () => { ready.hello(); banners.syncRecovery(); });
  T.on('host:notify', f => hostNotify.receive(f));
  T.on('host:banner', f => banners.hostBanner(f));
  T.on('host:navigate', f => nav.navigate(f));
  T.on('host:confirmQuit', f => quit.confirm(f));
  T.on('rt:job', e => banners.job(e));
  // the shell's rt:stale{epoch,topic,key,reason} for a slot, forwarded by the rt client as
  // rt:slot-stale (rt/client.js; 8th-wave ruling T-P4-H2 item 1)
  T.on('rt:slot-stale', f => banners.slotStale(f));
  S.sub('failure', () => { banners.syncStartup(); banners.syncCleanup(); });
  S.sub('run', () => { banners.syncStartup(); banners.syncCleanup(); });
  S.sub('core', () => banners.syncCleanup());
  S.sub('recovery', () => banners.syncRecovery());
  const upd = topic('update.state');
  upd.sub(() => update.state(upd.get()));
  T.on('i18n:change', () => { banners.relocalize(); update.relocalize(); });

  // already connected (the design target's simulator replays inside T.rt.connect, before this
  // file runs): this page connection has started; state already in the store is taken over
  const r0 = rt();
  if (r0 && r0.state === 'live') ready.hello();
  banners.syncStartup(); banners.syncCleanup(); banners.syncRecovery();
  if (upd.get()) update.state(upd.get());
  if (launch) launch.check();
  ready.check();

  const api = T.shellc = T.shellc || {};
  Object.assign(api, {
    registerDraft,
    get presence() { return presence.value; },
    errorText: err => banners.errorWords(err),
    commandError: err => banners.commandError(err),
    run: (cmd, args) => banners.runCmd(cmd, args),
    openFolder: kind => banners.openFolder(kind),
    listStale: (name, key) => banners.listStale(name, key),
    reload: (name, key) => resync(name, key),
    unsaved: () => quit.unsaved(),
    diag: () => ring.slice(),
  });
  return { presence, queue, hostNotify, banners, quit, nav, language, ready, update, frame: windowFrame, host, diag: () => ring.slice() };
}
