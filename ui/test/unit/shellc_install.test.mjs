// T-P4-06: installShellc on the real rt client and FakeTransport (fake clock, no DOM):
// connect-time declarations (host_notify_ready, then host_set_page) on every connection, a
// same-epoch reconnect included; host_ready once, after the first service.state snapshot and the
// first page enter, in the next frame, without host:caps too, errors silent; host frames reach
// their modules; presence pauses the scope timers and the toast queue.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installShellc } from '../../src/shellc/index.js';
import { boot, E1, E2, evt, flush, hello, svc } from './rt_helpers.mjs';

const HOSTS = ['host_notify_ready', 'host_set_page', 'host_ready', 'host_notify_rendered', 'host_confirm_quit', 'host_force_restart', 'rt_resync'];

function page({ scenario = { steps: [], allow_host: HOSTS }, live = false } = {}) {
  const b = boot({ scenario });
  const { T } = b;
  const frames = [];
  const win = { requestAnimationFrame: fn => { frames.push(fn); return frames.length; } };
  const docFns = [];
  const doc = { hidden: false, visibilityState: 'visible', addEventListener: (t, fn) => docFns.push(fn) };
  const notices = [], dismissed = [], gone = [];
  T.notify = o => { notices.push(o); return { el: { isConnected: true }, dismiss() {} }; };
  T.dismiss = id => dismissed.push(id);
  T.router = { current: null };
  T.go = (id, params) => { gone.push([id, params]); T.router.current = id; T.emit('page:change', { id, from: null, params: params || {} }); return true; };
  T.t = (id, p) => (p ? `${id}${JSON.stringify(p)}` : id);
  T.i18n = { lang: 'zh_CN', set: l => { T.i18n.lang = l; return Promise.resolve(l); } };
  T.ui = { saveBar: { dirtyCount: () => 2 } };
  const scopes = { paused: false, log: [] };
  T.scopes = { get paused() { return scopes.paused; }, pause() { scopes.paused = true; scopes.log.push('pause'); }, resume() { scopes.paused = false; scopes.log.push('resume'); } };
  if (live) return { ...b, frames, notices, dismissed, gone, scopes, win, doc, docFns };
  const sc = installShellc(T, { win, doc, storage: null });
  return { ...b, sc, frames, notices, dismissed, gone, scopes, win, doc, docFns };
}
const hostCalls = fake => fake.invocations.filter(i => i.kind === 'host').map(i => (i.name === 'host_set_page' ? `host_set_page:${i.args.page}` : i.name));
const runFrames = p => { while (p.frames.length) p.frames.shift()(); };
const replayStart = (p, epoch = E1, reason = 'reconnect') => { p.fake.deliver({ t: 'rt:reset', epoch, reason }); p.fake.deliver(hello({ epoch })); };

test('every connection declares host_notify_ready, then names its page; host_ready once after snapshot + enter + a frame', async () => {
  const p = page();
  await flush();
  replayStart(p);
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready'], 'no page shown yet: nothing to name');
  p.T.go('profiles', {});
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready', 'host_set_page:nodes']);
  runFrames(p);
  assert.ok(!hostCalls(p.fake).includes('host_ready'), 'no service.state snapshot yet');
  p.fake.deliver(evt('service.state', 1, 'snapshot', svc()));
  assert.ok(!hostCalls(p.fake).includes('host_ready'), 'waits for the next frame');
  runFrames(p);
  await flush();
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready', 'host_set_page:nodes', 'host_ready']);
  assert.ok(!p.T.rt.hostHas('host_ready'), 'no host:caps was sent: host_ready is still called');

  // a same-epoch reconnect (an order fault): declared again, the page named again, no second host_ready
  p.fake.reconnect('order_jump');
  await flush();
  replayStart(p);
  p.fake.deliver(evt('service.state', 2, 'snapshot', svc()));
  runFrames(p);
  await flush();
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready', 'host_set_page:nodes', 'host_ready', 'host_notify_ready', 'host_set_page:nodes']);

  // a new generation inside the same page connection (rt:reset{runtime_gone}: a new sidecar, the
  // shell's page_generation is unchanged): nothing is declared again, receipts stay valid
  replayStart(p, E2, 'runtime_gone');
  await flush();
  assert.deepEqual(hostCalls(p.fake).slice(5), []);

  // a new page connection whose first frame is rt:reset{runtime_gone}: declared once more
  p.fake.reconnect('order_jump');
  await flush();
  replayStart(p, E2, 'runtime_gone');
  await flush();
  assert.deepEqual(hostCalls(p.fake).slice(5), ['host_notify_ready', 'host_set_page:nodes']);
});

test('receipts: an rt:reset{runtime_gone} inside the page connection keeps a waiting notice receiptable; a new connection does not', async () => {
  const p = page();
  await flush();
  replayStart(p);
  const rendered = () => p.fake.invocations.filter(i => i.name === 'host_notify_rendered').map(i => i.args.delivery_id);
  p.fake.deliver({ t: 'host:visibility', visible: false, epoch: E1 });
  p.fake.deliver({ t: 'host:notify', delivery_id: 'g-1', severity: 'warn', title: 't', detail: 'd', epoch: E1 });
  await flush();
  replayStart(p, E2, 'runtime_gone');          // the sidecar was replaced; same page connection
  p.clock.advance(1000);
  p.fake.deliver({ t: 'host:visibility', visible: true, epoch: E2 });
  await flush();
  assert.deepEqual(rendered(), ['g-1'], 'shown within 3 s on the same page connection: receipted');

  // a new page connection (rt_connect) in between: the waiting notice is shown, not receipted
  p.fake.deliver({ t: 'host:visibility', visible: false, epoch: E2 });
  p.fake.deliver({ t: 'host:notify', delivery_id: 'g-2', severity: 'warn', title: 't', detail: 'd', epoch: E2 });
  await flush();
  p.fake.reconnect('order_jump');
  await flush();
  replayStart(p, E2);
  p.fake.deliver({ t: 'host:visibility', visible: true, epoch: E2 });
  await flush();
  assert.ok(p.notices.some(o => o.id === 'host-notify:g-2'));
  assert.deepEqual(rendered(), ['g-1']);
});

test('host_ready: called with host:caps too (never gated by hostHas); its error is silent', async () => {
  const p = page({ scenario: { steps: [{ on_invoke: { host: 'host_ready' }, reject: 'not_allowed' }], allow_host: HOSTS.filter(h => h !== 'host_ready') } });
  await flush();
  p.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  p.fake.deliver({ t: 'host:caps', epoch: E1, invokes: ['rt', 'rt_connect'], events: ['host:caps', 'rt:reset'] });
  p.fake.deliver(hello());
  p.fake.deliver(evt('service.state', 1, 'snapshot', svc()));
  p.T.go('overview', {});
  runFrames(p);
  await flush(6);
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready', 'host_set_page:overview', 'host_ready']);
  assert.deepEqual(p.fake.failures, []);
  assert.ok(p.sc.diag().some(d => d.kind === 'host_error' && d.name === 'host_ready' && d.code === 'not_allowed'));
});

test('a page installed after its connection is live (the design simulator replays inside connect) declares at once', async () => {
  const p = page({ live: true });
  await flush();
  replayStart(p);
  p.fake.deliver(evt('service.state', 1, 'snapshot', svc()));
  assert.equal(p.T.rt.state, 'live');
  installShellc(p.T, { win: p.win, doc: p.doc, storage: null });
  p.T.go('logs', {});
  runFrames(p);
  await flush();
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready', 'host_set_page:logs', 'host_ready']);
});

test('host frames reach their modules: confirmQuit answers with the unsaved count, navigate goes, notify is receipted', async () => {
  const p = page();
  await flush();
  replayStart(p);
  p.fake.deliver({ t: 'host:navigate', page: 'update', epoch: E1 });
  const off = p.T.shellc.registerDraft(() => 3);
  p.fake.deliver({ t: 'host:confirmQuit', token: 9, epoch: E1 });
  // with a draft registered, a host:navigate does not leave (it would drop the draft): noted
  p.fake.deliver({ t: 'host:navigate', page: 'overview', epoch: E1 });
  p.fake.deliver({ t: 'host:notify', delivery_id: 'd-1', severity: 'warn', title: '<b>node</b>', detail: 'x', target: 'alerts', epoch: E1 });
  await flush();
  off();
  const q = p.fake.invocations.find(i => i.name === 'host_confirm_quit');
  assert.deepEqual(q.args, { token: 9, unsaved: 5 }, 'saveBar.dirtyCount() 2 + draft 3');
  assert.deepEqual(p.gone, [['settings', { focus: 'update' }]]);
  assert.ok(p.sc.diag().some(d => d.kind === 'navigate_held' && d.page === 'overview'));
  const n = p.notices.find(o => o.id === 'host-notify:d-1');
  assert.equal(n.title, '<b>node</b>');
  assert.deepEqual(p.fake.invocations.filter(i => i.name === 'host_notify_rendered').map(i => i.args), [{ delivery_id: 'd-1' }]);
});

test('presence: host:visibility false pauses the scope timers, true resumes them; a notice waits while hidden', async () => {
  const p = page();
  await flush();
  replayStart(p);
  p.fake.deliver({ t: 'host:visibility', visible: false, epoch: E1 });
  assert.deepEqual(p.T.store.get('presence'), { visible: false, host: false, doc: true });
  assert.deepEqual(p.scopes.log, ['pause']);
  p.fake.deliver({ t: 'host:notify', delivery_id: 'd-2', severity: 'error', title: 't', detail: 'd', target: 'overview', epoch: E1 });
  await flush();
  assert.equal(p.notices.filter(o => o.id === 'host-notify:d-2').length, 0, 'not rendered while hidden');
  p.clock.advance(1000);
  p.fake.deliver({ t: 'host:visibility', visible: true, epoch: E1 });
  await flush();
  assert.deepEqual(p.scopes.log, ['pause', 'resume']);
  assert.equal(p.notices.filter(o => o.id === 'host-notify:d-2').length, 1);
  assert.deepEqual(p.fake.invocations.filter(i => i.name === 'host_notify_rendered').map(i => i.args.delivery_id), ['d-2']);
});

test('fixture mode narrowing: shell invokes the scenario did not register are skipped, never failures', async () => {
  const b = boot({ scenario: { steps: [] } });
  const T = b.T;
  Object.assign(T, { notify: () => ({}), dismiss() {}, router: { current: null }, t: id => id, i18n: { lang: 'zh_CN', set: () => Promise.resolve() }, ui: {} });
  T.go = (id) => { T.router.current = id; T.emit('page:change', { id, params: {} }); return true; };
  const sc = installShellc(T, { win: { requestAnimationFrame: fn => fn() }, doc: null, storage: null, allowHost: () => false });
  await flush();
  b.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  b.fake.deliver(hello());
  b.fake.deliver(evt('service.state', 1, 'snapshot', svc()));
  T.go('overview');
  await flush();
  assert.deepEqual(b.fake.failures, []);
  assert.deepEqual(b.fake.invocations.filter(i => i.kind === 'host'), []);
  assert.deepEqual(sc.diag().filter(d => d.kind === 'host_skipped').map(d => d.name), ['host_notify_ready', 'host_set_page', 'host_ready']);
});

// ---- T-P4-H2 (8th-wave ruling item 2) ------------------------------------------------------
test('T-P4-H2: a second rt:reset{reconnect} while the client is still reconnecting declares host_notify_ready again', async () => {
  const p = page();
  await flush();
  replayStart(p);
  p.T.go('profiles', {});
  await flush();
  assert.deepEqual(hostCalls(p.fake), ['host_notify_ready', 'host_set_page:nodes']);
  // the replay of the first reconnect has not ended (no hello yet) when the next one starts:
  // the client stays reconnecting, so no second rt:conn arrives
  p.fake.reconnect('order_jump');
  await flush();
  p.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  p.fake.reconnect('order_jump');
  await flush();
  assert.equal(p.T.rt.state, 'reconnecting');
  p.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  p.fake.deliver(hello());
  await flush();
  assert.deepEqual(hostCalls(p.fake).slice(2), ['host_notify_ready', 'host_set_page:nodes', 'host_notify_ready', 'host_set_page:nodes'],
    'each page connection (each rt:reset{reconnect}) declares; the hello after it does not add one');
  // an rt:reset{runtime_gone} inside the connection is still no new page connection
  replayStart(p, E2, 'runtime_gone');
  await flush();
  assert.equal(hostCalls(p.fake).length, 6);
});

test('T-P4-H2: a shell invoke outside the ten names of PROTOCOL section 13 is refused (host_unknown), never sent or skipped', async () => {
  const p = page();
  await flush();
  replayStart(p);
  const code = await p.sc.host('host_notify_redy').then(() => 'resolved', e => e && e.code);
  assert.equal(code, 'not_allowed');
  assert.ok(p.sc.diag().some(d => d.kind === 'host_unknown' && d.name === 'host_notify_redy'));
  assert.ok(!p.fake.invocations.some(i => i.name === 'host_notify_redy'));
  // the fixture narrowing cannot hide it either: refused before allowHost
  const b = boot({ scenario: { steps: [] } });
  Object.assign(b.T, { notify: () => ({}), dismiss() {}, router: { current: null }, t: id => id, i18n: { lang: 'zh_CN', set: () => Promise.resolve() }, ui: {} });
  const sc = installShellc(b.T, { win: null, doc: null, storage: null, allowHost: () => false });
  assert.equal(await sc.host('rt_resynk', { topic: 'nodes.list' }).then(() => 'resolved', e => e && e.code), 'not_allowed');
  assert.deepEqual(sc.diag().filter(d => d.name === 'rt_resynk').map(d => d.kind), ['host_unknown']);
  // the ten names pass (skipped here by the narrowing, as before; 11-05 host_window too)
  assert.equal(await sc.host('rt_resync', { topic: 'nodes.list', key: 'vanguard' }), null);
  assert.ok(sc.diag().some(d => d.kind === 'host_skipped' && d.name === 'rt_resync'));
  assert.equal(await sc.host('host_window', { action: 'minimize' }), null);
  assert.ok(sc.diag().some(d => d.kind === 'host_skipped' && d.name === 'host_window'));
});
