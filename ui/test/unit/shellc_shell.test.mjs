// T-P4-06: quit answer and drafts, navigation, language, presence and reduced motion
// (shellc/quit.js, navigate.js, language.js, presence.js), no DOM.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDrafts, createQuit } from '../../src/shellc/quit.js';
import { createNavigate, presencePage, NAV_TARGETS } from '../../src/shellc/navigate.js';
import { createLanguage } from '../../src/shellc/language.js';
import { createPresence, createReducedMotion } from '../../src/shellc/presence.js';
import { createFakeClock } from '../../src/rt/clock.js';
import { makeT, flush } from './rt_helpers.mjs';

// ---- quit ----------------------------------------------------------------------------------
test('confirmQuit: unsaved = dirtyCount() + registered drafts (true = 1, false / 0 / junk = 0), answered at once', async () => {
  const clock = createFakeClock(0);
  const drafts = createDrafts();
  let dirty = 3;
  const sent = [];
  const quit = createQuit({ host: (n, a) => { sent.push([n, a, clock.now()]); return Promise.resolve(null); }, dirtyCount: () => dirty, drafts });
  const off1 = drafts.register(() => 2);
  drafts.register(() => true);
  drafts.register(() => false);
  drafts.register(() => 'x');
  drafts.register(() => -4);
  assert.equal(quit.confirm({ t: 'host:confirmQuit', token: 7, epoch: 'e1' }), 6);
  assert.deepEqual(sent[0].slice(0, 2), ['host_confirm_quit', { token: 7, unsaved: 6 }]);
  assert.equal(sent[0][2], 0, 'answered before any time passed (the shell waits at most 3 s)');
  off1(); off1();
  dirty = 0;
  assert.equal(quit.confirm({ token: 8 }), 1, 'unregister is idempotent; the bar is clean');
  assert.equal(quit.confirm({ token: 8 }), null, 'one answer per token');
  assert.equal(quit.confirm({ token: -1 }), null);
  assert.equal(quit.confirm({ token: 'x' }), null);
  assert.equal(sent.length, 2);
});

test('confirmQuit: a hook that throws counts 0; the shell\'s rejection (stale_token / not_confirming) is silent', async () => {
  const drafts = createDrafts();
  drafts.register(() => { throw new Error('draft broken'); });
  drafts.register(() => 4);
  const thrown = [];
  const keep = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => { try { fn(); } catch (e) { thrown.push(e.message); } return 0; };
  try {
    const quit = createQuit({ host: () => Promise.reject(Object.assign(new Error('stale_token'), { code: 'stale_token' })), dirtyCount: () => { throw new Error('bar broken'); }, drafts });
    assert.equal(quit.confirm({ token: 1 }), 4);
  } finally { globalThis.setTimeout = keep; }
  await flush();
  assert.deepEqual(thrown.sort(), ['bar broken', 'draft broken'], 'reported asynchronously, not swallowed');
  assert.throws(() => drafts.register('x'), TypeError);
});

test('confirmQuit through the event bus: the page opens no dialog of its own', async () => {
  const T = makeT();
  const drafts = createDrafts();
  const sent = [];
  const quit = createQuit({ host: (n, a) => { sent.push([n, a]); return Promise.resolve(); }, dirtyCount: () => 1, drafts });
  T.on('host:confirmQuit', f => quit.confirm(f));
  T.emit('host:confirmQuit', { t: 'host:confirmQuit', token: 3, epoch: 'e1' });
  assert.deepEqual(sent, [['host_confirm_quit', { token: 3, unsaved: 1 }]]);
});

// ---- navigation ------------------------------------------------------------------------------
test('host:navigate maps overview / alerts / update; unknown pages do nothing', () => {
  const gone = [];
  const nav = createNavigate({ go: (p, o) => gone.push([p, o]), host: () => Promise.resolve() });
  for (const page of ['overview', 'alerts', 'update', 'nodes', '__proto__']) nav.navigate({ t: 'host:navigate', page });
  assert.deepEqual(gone, [['overview', {}], ['settings', { focus: 'alerts' }], ['settings', { focus: 'update' }]]);
  assert.equal(Object.isFrozen(NAV_TARGETS.alerts.params), true);
  gone[1][1].focus = 'x';
  assert.equal(NAV_TARGETS.alerts.params.focus, 'alerts', 'each navigation gets its own params');
});

test('host:navigate while an overlay is open or a draft is registered: no T.go (the draft is kept), noted; afterwards it goes', () => {
  const gone = [], notes = [];
  let busy = true;
  const nav = createNavigate({ go: (p, o) => gone.push([p, o]), host: () => Promise.resolve(), busy: () => busy, diag: e => notes.push(e) });
  assert.equal(nav.navigate({ page: 'alerts' }), false);
  assert.equal(nav.navigate({ page: 'overview' }), false);
  assert.deepEqual(gone, []);
  assert.deepEqual(notes, [{ kind: 'navigate_held', page: 'alerts' }, { kind: 'navigate_held', page: 'overview' }]);
  nav.navigate({ page: 'nodes' });                       // unknown: nothing, no note either
  assert.equal(notes.length, 2);
  // the notice's View action (goTarget) is the page's own click: not held (the overlay's scrim covers it)
  assert.equal(nav.goTarget('update'), true);
  busy = false;
  assert.equal(nav.navigate({ page: 'alerts' }), true);
  assert.deepEqual(gone, [['settings', { focus: 'update' }], ['settings', { focus: 'alerts' }]]);
});

test('host_set_page: profiles -> nodes, settings focused on alerts -> alerts, unknown -> none; resent on each connection', async () => {
  assert.equal(presencePage('profiles'), 'nodes');
  assert.equal(presencePage('tiers'), 'nodes');
  assert.equal(presencePage('settings', { focus: 'alerts' }), 'alerts');
  assert.equal(presencePage('settings', { focus: 'update' }), 'settings');
  assert.equal(presencePage('overview'), 'overview');
  assert.equal(presencePage('logs'), 'logs');
  assert.equal(presencePage('t04a_blank'), 'none');
  const sent = [];
  const nav = createNavigate({ go: () => {}, host: (n, a) => { sent.push([n, a.page]); return Promise.reject(Object.assign(new Error('invalid_page'), { code: 'invalid_page' })); } });
  nav.connected();
  assert.deepEqual(sent, [], 'no page shown yet: nothing to name');
  nav.pageChanged({ id: 'profiles', params: {} });
  nav.pageChanged({ id: 'profiles', params: { tier: 'bastion' } });
  nav.pageChanged({ id: 'settings', params: { focus: 'alerts' } });
  nav.connected();
  await flush();
  assert.deepEqual(sent, [['host_set_page', 'nodes'], ['host_set_page', 'alerts'], ['host_set_page', 'alerts']]);
});

test('host_set_page: a page shown before the first connection is named by that connection, not before', async () => {
  const sent = [];
  const nav = createNavigate({ go: () => {}, host: (n, a) => { sent.push([n, a.page]); return Promise.resolve(null); } });
  nav.pageChanged({ id: 'logs', params: {} });
  nav.pageChanged({ id: 'profiles', params: {} });
  assert.deepEqual(sent, [], 'no connection yet: the shell has no page to name');
  nav.connected();
  assert.deepEqual(sent, [['host_set_page', 'nodes']]);
  nav.pageChanged({ id: 'overview', params: {} });
  assert.deepEqual(sent, [['host_set_page', 'nodes'], ['host_set_page', 'overview']]);
});

// ---- language --------------------------------------------------------------------------------
function fakeI18n({ fail = null } = {}) {
  const i = { lang: 'zh_CN', calls: [] };
  i.set = lang => { i.calls.push(lang); if (fail === lang) return Promise.reject(Object.assign(new Error('load_failed'), { code: 'load_failed' })); i.lang = lang; return Promise.resolve(lang); };
  return i;
}

test('language: hello.lang and host:language with the same value load once; a different one switches', async () => {
  const T = makeT();
  const i18n = fakeI18n();
  i18n.lang = null;
  createLanguage({ store: T.store, on: T.on, i18n });
  T.store.set('lang', 'en_US');                                   // hello
  T.emit('host:language', { t: 'host:language', language: 'en_US' });
  await flush();
  assert.deepEqual(i18n.calls, ['en_US']);
  T.emit('host:language', { t: 'host:language', language: 'ja_JP' });
  T.store.set('lang', 'ja_JP');                                   // the client writes the store too
  await flush();
  assert.deepEqual(i18n.calls, ['en_US', 'ja_JP']);
});

test('language: the language already applied is not loaded again; a failed switch can be retried', async () => {
  const T = makeT();
  const i18n = fakeI18n({ fail: 'de_DE' });
  createLanguage({ store: T.store, on: T.on, i18n });
  T.store.set('lang', 'zh_CN');
  await flush();
  assert.deepEqual(i18n.calls, [], 'zh_CN is already applied');
  T.emit('host:language', { language: 'de_DE' });
  await flush();
  T.emit('host:language', { language: 'de_DE' });
  await flush();
  assert.deepEqual(i18n.calls, ['de_DE', 'de_DE'], 'the failure is forgotten, the same value retries');
});

test('language: after a finished switch, a language changed by another path can be asked for again', async () => {
  const T = makeT();
  const i18n = fakeI18n();
  const lang = createLanguage({ store: T.store, on: T.on, i18n });
  T.emit('host:language', { language: 'en_US' });
  await flush();
  assert.equal(lang.asked, null, 'a finished switch is forgotten');
  i18n.lang = 'zh_CN';                                   // e.g. the settings page called T.i18n.set
  T.emit('host:language', { language: 'en_US' });
  await flush();
  assert.deepEqual(i18n.calls, ['en_US', 'en_US']);
  T.emit('host:language', { language: 'en_US' });        // now applied: not again
  await flush();
  assert.deepEqual(i18n.calls, ['en_US', 'en_US']);
});

// ---- presence and reduced motion ---------------------------------------------------------------
function fakeDoc() {
  const d = { hidden: false, visibilityState: 'visible', fns: [] };
  d.addEventListener = (type, fn) => { if (type === 'visibilitychange') d.fns.push(fn); };
  d.set = hidden => { d.hidden = hidden; d.visibilityState = hidden ? 'hidden' : 'visible'; d.fns.forEach(fn => fn()); };
  return d;
}

test('presence: host:visibility and document.visibilityState make store presence.visible; changes are announced', () => {
  const T = makeT();
  const doc = fakeDoc();
  const p = createPresence({ store: T.store, on: T.on, doc });
  const seen = [];
  p.onChange(v => seen.push(v));
  assert.deepEqual(T.store.get('presence'), { visible: true, host: null, doc: true }, 'unknown host = visible until the shell says otherwise');
  T.emit('host:visibility', { t: 'host:visibility', visible: false });
  assert.equal(T.store.get('presence').visible, false);
  T.emit('host:visibility', { visible: true });
  doc.set(true);
  assert.equal(p.visible, false, 'the document is hidden');
  doc.set(false);
  T.emit('host:visibility', { visible: 'yes' });
  assert.deepEqual(seen, [false, true, false, true]);
});

function fakeWin({ injected, media }) {
  const mq = { matches: media, fns: [], addEventListener(t, fn) { this.fns.push(fn); } };
  const w = { matchMedia: () => mq, mq };
  if (injected !== undefined) w.__TESSERACT_MOTION__ = Object.freeze({ reduced: injected });
  return w;
}

test('reduced motion = the shell\'s __TESSERACT_MOTION__.reduced OR prefers-reduced-motion (followed live)', () => {
  assert.equal(createReducedMotion(fakeWin({ injected: true, media: false })).reduced, true);
  assert.equal(createReducedMotion(fakeWin({ injected: false, media: true })).reduced, true);
  assert.equal(createReducedMotion(fakeWin({ injected: false, media: false })).reduced, false);
  assert.equal(createReducedMotion(fakeWin({ media: false })).reduced, false, 'no injection (shell before C3)');
  assert.equal(createReducedMotion({ __TESSERACT_MOTION__: { reduced: 'yes' }, matchMedia: () => ({ matches: false }) }).reduced, false, 'only a true boolean counts');
  const w = fakeWin({ injected: false, media: false });
  const rm = createReducedMotion(w);
  const seen = [];
  rm.onChange(v => seen.push(v));
  w.mq.fns.forEach(fn => fn({ matches: true }));
  assert.equal(rm.reduced, true);
  w.mq.fns.forEach(fn => fn({ matches: false }));
  assert.deepEqual(seen, [true, false]);
  const w2 = fakeWin({ injected: true, media: false });
  const rm2 = createReducedMotion(w2);
  const seen2 = [];
  rm2.onChange(v => seen2.push(v));
  w2.mq.fns.forEach(fn => fn({ matches: true }));
  w2.mq.fns.forEach(fn => fn({ matches: false }));
  assert.equal(rm2.reduced, true, 'the shell flag holds whatever the media query says');
  assert.deepEqual(seen2, [], 'no change announced');
});

// ---- T-P4-H2 (8th-wave ruling item 2) ------------------------------------------------------
test('T-P4-H2: a shell target that stays on the same page (settings -> settings{focus:alerts}) names the page again; a held go names nothing', async () => {
  const sent = [];
  let cur = { id: 'settings', params: { focus: 'update' } };
  let hold = false;
  // T.go of the same page runs enter() again and emits no page:change (core.js)
  const go = (id, params) => { if (hold) return false; cur = { id, params }; return true; };
  const nav = createNavigate({ go, host: (n, a) => { sent.push([n, a.page]); return Promise.resolve(null); }, current: () => cur });
  nav.pageChanged({ id: 'settings', params: { focus: 'update' } });
  nav.connected();
  assert.deepEqual(sent, [['host_set_page', 'settings']]);
  nav.navigate({ page: 'alerts' });
  assert.deepEqual(sent, [['host_set_page', 'settings'], ['host_set_page', 'alerts']], 'settings -> alerts without a page:change');
  nav.navigate({ page: 'alerts' });
  assert.equal(sent.length, 2, 'unchanged: not sent again');
  hold = true;
  nav.navigate({ page: 'update' });
  assert.equal(sent.length, 2, 'the page held the way (go returned false): nothing named');
  hold = false;
  nav.goTarget('update');            // the "View" action of a host:notify
  assert.deepEqual(sent.at(-1), ['host_set_page', 'settings']);
  // before the first connection nothing is named
  const sent2 = [];
  const nav2 = createNavigate({ go: () => true, host: (n, a) => { sent2.push([n, a.page]); return Promise.resolve(null); } });
  nav2.navigate({ page: 'alerts' });
  assert.deepEqual(sent2, []);
  nav2.connected();
  assert.deepEqual(sent2, [['host_set_page', 'alerts']], 'the connection names the page the target landed on');
});
