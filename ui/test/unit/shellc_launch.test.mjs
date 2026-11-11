// 11-05 window-first launch: the launch screen (shellc/launch.js) on a small fake DOM with a
// manual clock, and its wiring in installShellc (launch: true) on the real rt client and
// FakeTransport: the shell's replay without a service.state snapshot shows the launch and lets the
// shell reveal the window (host_ready); real state then hands over to the sidebar's mark.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createLaunch, LAUNCH_AFTER_HELLO_MS, LAUNCH_LIMIT_MS, LAUNCH_MIN_SHOWN_MS, LAUNCH_SETTLE_MS, LAUNCH_SHOW_FALLBACK_MS } from '../../src/shellc/launch.js';
import { installShellc } from '../../src/shellc/index.js';
import { boot, E1, evt, flush, hello, svc } from './rt_helpers.mjs';

// ---- a fake DOM: just what launch.js touches ------------------------------------------------
function node(tag, rect = { left: 0, top: 0, width: 0, height: 0 }) {
  const cls = new Set();
  const n = {
    tag, children: [], parent: null, attrs: {}, style: {}, rect, animations: [], textContent: '', inert: false,
    classList: { add: (...c) => c.forEach(x => cls.add(x)), remove: (...c) => c.forEach(x => cls.delete(x)), contains: c => cls.has(c), toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)) },
    get className() { return [...cls].join(' '); }, set className(v) { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(c => cls.add(c)); },
    setAttribute(k, v) { n.attrs[k] = String(v); if (k === 'class') n.className = v; },
    appendChild(c) { c.parent = n; n.children.push(c); return c; },
    append(...cs) { cs.forEach(c => n.appendChild(c)); },
    remove() { if (n.parent) { n.parent.children = n.parent.children.filter(c => c !== n); n.parent = null; } },
    getBoundingClientRect: () => n.rect,
    querySelector: () => null,
    animate(keyframes, opts) {
      let resolve; const finished = new Promise(r => { resolve = r; });
      const a = { keyframes, opts, finished, finish: () => resolve() };
      n.animations.push(a); return a;
    },
  };
  return n;
}
function fakeDoc({ sidebar = true } = {}) {
  const root = node('html'), body = node('body'), app = node('div');
  const dot = node('circle');
  const mark = node('svg', { left: 18, top: 16, width: 29, height: 29 });
  mark.querySelector = sel => (sel === '.brand-dot' ? dot : null);
  const wordmark = node('span', { left: 57, top: 22, width: 92, height: 17 });
  const created = [];
  const doc = {
    documentElement: root, body,
    getElementById: id => (id === 'app' ? app : null),
    createElement: tag => { const n = node(tag); created.push(n); return n; },
    createElementNS: (ns, tag) => { const n = node(tag); created.push(n); return n; },
    querySelector: sel => (!sidebar ? null : sel === '.side .brand-mark' ? mark : sel === '.side .wordmark' ? wordmark : null),
  };
  // the launch mark lays out at 58 px, centred in a 1280 x 800 window
  const launchMark = () => created.find(n => n.tag === 'svg');
  const launchBrand = () => created.find(n => n.className === 'launch-brand');
  const cover = () => body.children.find(n => n.className.split(' ').includes('launch')) || null;
  return { doc, root, body, app, dot, mark, created, launchMark, launchBrand, cover };
}
function clock() {
  let t = 1000; const q = new Map(); let id = 0;
  return {
    now: () => t,
    timer: (fn, ms) => { q.set(++id, { at: t + ms, fn }); return id; },
    clear: h => q.delete(h),
    advance(ms) {
      const end = t + ms;
      for (;;) {
        const due = [...q.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        q.delete(due[0]); t = due[1].at; due[1].fn();
      }
      t = end;
    },
    get pending() { return q.size; },
  };
}
const win = { getComputedStyle: () => ({ fill: win.fill || 'rgb(167, 172, 181)' }) };
function make(over = {}) {
  const d = fakeDoc(over.dom);
  const c = clock();
  const state = { ready: false, reduced: false, visible: true, shown: 0, gone: 0 };
  const L = createLaunch({ doc: d.doc, win, ready: () => state.ready, reduced: () => state.reduced, visible: () => state.visible,
    settle: { easing: 'linear', duration: 460 }, now: c.now, timer: c.timer, clear: c.clear,
    onShown: () => state.shown++, onGone: () => state.gone++, ...over.ports });
  // layout: the launch mark at 58 px, its row (mark + gap + wordmark) centred
  d.launchMark().rect = { left: 520, top: 340, width: 58, height: 58 };
  d.launchBrand().rect = { left: 520, top: 340, width: 240, height: 58 };
  return { ...d, c, state, L };
}

test('mounts a cover over an inert app; the replay without state shows the draw and lets the window reveal', () => {
  const m = make();
  assert.equal(m.L.phase, 'cover');
  assert.ok(m.cover(), 'the cover is in the body');
  assert.ok(m.root.classList.contains('launching'));
  assert.equal(m.app.inert, true);
  assert.equal(m.L.shown, false, 'nothing drawn yet: the window stays cloaked');
  m.L.replayed();
  assert.equal(m.L.phase, 'shown');
  assert.ok(m.cover().classList.contains('is-drawing'));
  assert.equal(m.state.shown, 1);
  assert.equal(m.L.shown, true);
  // the mark is the sidebar's: two strokes (both subpaths open at once) and the dot, no words but the brand
  const paths = m.created.filter(n => n.tag === 'path');
  assert.equal(paths.length, 2);
  assert.ok(paths.every(p => p.attrs.pathLength === '1'));
  assert.equal(m.created.find(n => n.className === 'launch-word').textContent, 'Tesseract');
  assert.equal(m.cover().attrs['aria-hidden'], 'true');
});

test('real state before the cover was shown (reload, recreated window): it goes in the same frame, nothing drawn', () => {
  const m = make();
  m.state.ready = true;
  m.L.replayed();
  assert.equal(m.L.phase, 'gone');
  assert.equal(m.cover(), null);
  assert.equal(m.app.inert, false);
  assert.ok(!m.root.classList.contains('launching'));
  assert.equal(m.state.shown, 0);
  assert.equal(m.c.pending, 0, 'no timer left behind');
});

test('ready while drawing: waits until the draw has had its time, then travels onto the sidebar mark', async () => {
  const m = make();
  m.L.replayed();
  m.c.advance(300);
  m.state.ready = true;
  m.L.check();
  assert.equal(m.L.phase, 'shown', 'the draw is not cut');
  m.c.advance(LAUNCH_MIN_SHOWN_MS - 300 - 1);
  assert.equal(m.L.phase, 'shown');
  m.c.advance(1);
  assert.equal(m.L.phase, 'leaving');
  assert.ok(m.cover().classList.contains('is-leaving') && !m.cover().classList.contains('is-drawing'));
  const fly = m.launchBrand().animations[0];
  // one transform: the 58 px mark onto the 29 px sidebar mark (scale .5), from its own top-left
  assert.deepEqual(fly.keyframes[1], { transform: 'translate(-502px, -324px) scale(0.5)' });
  assert.equal(m.launchBrand().style.transformOrigin, '0px 0px');
  assert.equal(fly.opts.duration, 460);
  // the light takes the sidebar dot's colour (and follows it once more halfway); the ground fades
  const light = m.created.find(n => n.tag === 'circle');
  assert.equal(light.style.fill, 'rgb(167, 172, 181)');
  win.fill = 'rgb(43, 58, 96)';
  m.c.advance(230);
  assert.equal(light.style.fill, 'rgb(43, 58, 96)');
  delete win.fill;
  const ground = m.created.find(n => n.className === 'launch-ground');
  assert.deepEqual(ground.animations[0].keyframes, [{ opacity: 1 }, { opacity: 0 }]);
  fly.finish();
  await flush();
  assert.equal(m.L.phase, 'gone');
  assert.equal(m.cover(), null);
  assert.equal(m.app.inert, false);
  assert.ok(!m.root.classList.contains('launching'), 'the sidebar shows its own mark again');
  assert.equal(m.state.gone, 1);
  assert.equal(m.c.pending, 0);
});

test('a ready page after the draw leaves a frame later (its state applied first); a held animation clock still ends the cover', () => {
  const m = make();
  m.L.replayed();
  m.c.advance(LAUNCH_MIN_SHOWN_MS + 500);
  m.state.ready = true;
  m.L.check();
  m.L.check(); // a second snapshot in the same task schedules nothing more
  assert.equal(m.L.phase, 'shown');
  assert.equal(m.c.pending, 2, 'the 30 s limit and one settle timer only');
  m.c.advance(LAUNCH_SETTLE_MS);
  assert.equal(m.L.phase, 'leaving');
  m.c.advance(460 + 200);
  assert.equal(m.L.phase, 'gone', 'the timer backstop ends it without the animation');
});

test('reduced motion: drawn at once (is-still), and the leave is a removal', () => {
  const m = make();
  m.state.reduced = true;
  m.L.replayed();
  assert.ok(m.cover().classList.contains('is-still') && !m.cover().classList.contains('is-drawing'));
  m.state.ready = true;
  m.L.check();
  assert.equal(m.L.phase, 'gone');
  assert.equal(m.launchBrand().animations.length, 0);
});

test('a hidden window (autostart to the tray) never animates: the leave is a removal', () => {
  const m = make();
  m.L.replayed();
  m.state.visible = false;
  m.state.ready = true;
  m.L.check();
  assert.equal(m.L.phase, 'gone');
  assert.equal(m.launchBrand().animations.length, 0);
});

test('fallbacks: no replay → shown at 700 ms; hello without state → leaves after 4 s; never longer than 30 s', () => {
  let m = make();
  m.c.advance(LAUNCH_SHOW_FALLBACK_MS - 1);
  assert.equal(m.L.phase, 'cover');
  m.c.advance(1);
  assert.equal(m.L.phase, 'shown');
  assert.equal(m.state.shown, 1);
  m.L.hello();
  m.c.advance(LAUNCH_AFTER_HELLO_MS - 1);
  assert.equal(m.L.phase, 'shown');
  m.c.advance(1);
  assert.equal(m.L.phase, 'leaving', 'a runtime that said hello but sent no state does not hold the app');

  m = make();
  m.L.replayed();
  m.c.advance(LAUNCH_LIMIT_MS);
  assert.notEqual(m.L.phase, 'shown');
});

test('no sidebar mark to land on (or no WAAPI): the cover is simply removed', () => {
  const m = make({ dom: { sidebar: false } });
  m.L.replayed();
  m.c.advance(LAUNCH_MIN_SHOWN_MS);
  m.state.ready = true;
  m.L.check();
  m.c.advance(LAUNCH_SETTLE_MS);
  assert.equal(m.L.phase, 'gone');
});

// ---- wiring: installShellc({ launch: true }) on the real rt client --------------------------
test('installShellc launch: the shell replay without state reveals (host_ready) with the launch; real state hands over', async () => {
  const b = boot({ scenario: { steps: [], allow_host: ['host_notify_ready', 'host_set_page', 'host_ready'] } });
  const { T, fake } = b;
  const d = fakeDoc();
  T.router = { current: null };
  T.go = (id, params) => { T.router.current = id; T.emit('page:change', { id, from: null, params: params || {} }); return true; };
  T.t = id => id;
  T.i18n = { lang: 'zh_CN', set: l => Promise.resolve(l) };
  T.motion = { reduced: true, SP: { settle: { easing: 'linear', duration: 460 } } };
  const frames = [];
  const w = { requestAnimationFrame: fn => { frames.push(fn); return frames.length; }, getComputedStyle: () => ({ fill: 'none' }) };
  const docFns = [];
  const doc = Object.assign(d.doc, { hidden: false, visibilityState: 'visible', addEventListener: (t, fn) => docFns.push(fn) });
  const sc = installShellc(T, { win: w, doc, storage: null, launch: true });
  const hosts = () => fake.invocations.filter(i => i.kind === 'host').map(i => i.name);
  await flush();
  T.go('overview', {});
  // the shell's replay before the first hello (transport.rs replay, !ready): reset, caps, replayed
  fake.deliver({ t: 'rt:reset', epoch: '', reason: 'runtime_gone' });
  fake.deliver({ t: 'host:caps', epoch: '', invokes: ['rt', 'rt_connect', 'rt_resync', 'host_ready'], events: ['rt:replayed', 'rt:stale'] });
  fake.deliver({ t: 'rt:replayed', epoch: '' });
  assert.ok(d.cover() && d.cover().classList.contains('is-still'), 'the launch is shown (reduced motion: drawn at once)');
  while (frames.length) frames.shift()();
  await flush();
  assert.ok(hosts().includes('host_ready'), 'the window may be revealed before real state: ' + hosts().join());
  assert.equal(d.app.inert, true);
  // the runtime says hello and its seed brings service.state: the launch leaves
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'runtime_gone' });
  fake.deliver(hello());
  assert.ok(d.cover(), 'hello alone is not real state');
  fake.deliver(evt('service.state', 1, 'snapshot', svc({ phase: 'stopped' })));
  assert.equal(d.cover(), null);
  assert.equal(d.app.inert, false);
  assert.equal(hosts().filter(n => n === 'host_ready').length, 1, 'host_ready stays once per page load');
  assert.ok(sc.diag().some(e => e.kind === 'launch_gone'));
});

test('installShellc without launch (design target default): no cover, host_ready still waits for state', async () => {
  const b = boot({ scenario: { steps: [], allow_host: ['host_notify_ready', 'host_set_page', 'host_ready'] } });
  const { T, fake } = b;
  const d = fakeDoc();
  T.router = { current: null };
  T.go = id => { T.router.current = id; T.emit('page:change', { id, from: null, params: {} }); return true; };
  T.t = id => id;
  T.i18n = { lang: 'zh_CN', set: l => Promise.resolve(l) };
  const frames = [];
  const doc = Object.assign(d.doc, { hidden: false, visibilityState: 'visible', addEventListener: () => {} });
  installShellc(T, { win: { requestAnimationFrame: fn => frames.push(fn) }, doc, storage: null });
  await flush();
  T.go('overview');
  fake.deliver({ t: 'rt:reset', epoch: '', reason: 'runtime_gone' });
  fake.deliver({ t: 'rt:replayed', epoch: '' });
  while (frames.length) frames.shift()();
  await flush();
  assert.equal(d.cover(), null);
  assert.ok(!fake.invocations.some(i => i.kind === 'host' && i.name === 'host_ready'));
});
