// T-P4-06: the toast queue and host:notify with its receipt (shellc/notify.js), with a fake view
// and a fake clock (no DOM): 5 at once and the merge row, held countdowns while hidden, notices
// that wait while the window is hidden, the receipt only once on screen and within 3 s, none for
// an earlier connection, silent receipt errors, severity routing, duplicates.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHostNotify, createToastQueue, MAX_VISIBLE, RECEIPT_MS, WAITING_CAP } from '../../src/shellc/notify.js';
import { createFakeClock } from '../../src/rt/clock.js';

const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r)); };

// a view that records what is on screen; handles count their pauses
function fakeView() {
  const v = { shown: [], merged: 0, log: [] };
  v.toast = (o, { onClose }) => {
    const h = { o, el: { isConnected: true }, held: new Set(), closed: false,
      dismiss() { if (h.closed) return; h.closed = true; h.el.isConnected = false; v.shown.splice(v.shown.indexOf(h), 1); onClose(); },
      pause(r) { h.held.add(r); }, resume(r) { h.held.delete(r); } };
    v.shown.push(h);
    v.log.push(['toast', o.message]);
    return h;
  };
  v.merge = n => { v.merged = n; };
  return v;
}

test('toast queue: five at once, the rest wait behind one merge row, promoted as room frees', () => {
  const view = fakeView();
  const q = createToastQueue({ view });
  for (let i = 1; i <= 8; i++) q.push({ kind: 'info', message: `m${i}` });
  assert.equal(MAX_VISIBLE, 5);
  assert.deepEqual(view.shown.map(h => h.o.message), ['m1', 'm2', 'm3', 'm4', 'm5']);
  assert.equal(view.merged, 3, '「另有 3 条提醒」');
  view.shown[0].dismiss();
  assert.deepEqual(view.shown.map(h => h.o.message), ['m2', 'm3', 'm4', 'm5', 'm6']);
  assert.equal(view.merged, 2);
  q.dropWaiting();
  assert.equal(view.merged, 0, 'the merge row closed: the waiting ones are dropped');
  view.shown[0].dismiss();
  assert.equal(view.shown.length, 4, 'nothing left to promote');
});

test('toast queue: the same id replaces its notice; clear() empties shown and waiting', () => {
  const view = fakeView();
  const q = createToastQueue({ view });
  q.push({ kind: 'info', id: 'a', message: 'one' });
  q.push({ kind: 'info', id: 'a', message: 'two' });
  assert.deepEqual(view.shown.map(h => h.o.message), ['two']);
  for (let i = 0; i < 7; i++) q.push({ kind: 'info', message: `x${i}` });
  q.clear(true);
  assert.equal(view.shown.length, 0);
  assert.equal(view.merged, 0);
});

test('toast queue: countdowns hold while hidden, also for a toast shown while held', () => {
  const view = fakeView();
  const q = createToastQueue({ view });
  q.push({ kind: 'info', message: 'a' });
  q.pause('hidden');
  assert.ok(view.shown[0].held.has('hidden'));
  q.push({ kind: 'info', message: 'b' });
  assert.ok(view.shown[1].held.has('hidden'), 'a toast shown while hidden starts held');
  q.resume('hidden');
  assert.ok(view.shown.every(h => !h.held.has('hidden')));
  assert.equal(q.paused, false);
});

function hostNotifyRig({ visible = true } = {}) {
  const clock = createFakeClock(0);
  const view = fakeView();
  const q = createToastQueue({ view });
  let vis = visible;
  const listeners = new Set();
  const invokes = [], banners = [], gone = [], dismissed = [];
  let reject = null;
  const host = (name, args) => { invokes.push([name, args]); return reject ? Promise.reject(Object.assign(new Error(reject), { code: reject })) : Promise.resolve(null); };
  const go = target => { gone.push(target); return true; };
  go.can = t => ['overview', 'alerts', 'update'].includes(t);
  const hn = createHostNotify({
    toast: (o, hooks) => q.push(o, hooks),
    banner: o => { const h = { el: { isConnected: true }, o }; banners.push(h); return h; },
    dismiss: id => dismissed.push(id),
    visible: () => vis, onVisible: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    clock, host, go, t: id => id,
  });
  return {
    clock, view, q, hn, invokes, banners, gone, dismissed,
    setVisible(v) { vis = v; for (const fn of listeners) fn(v); },
    reject(code) { reject = code; },
    receipts: () => invokes.filter(([n]) => n === 'host_notify_rendered').map(([, a]) => a.delivery_id),
  };
}
const frame = (id, extra = {}) => ({ t: 'host:notify', delivery_id: id, severity: 'info', title: 'T', detail: 'D', target: 'alerts', epoch: 'e1', ...extra });

test('host:notify: info is a toast, warn a warning banner, error / critical an error banner; a receipt each, once', async () => {
  const r = hostNotifyRig();
  r.hn.receive(frame('d1'));
  r.hn.receive(frame('d2', { severity: 'warn' }));
  r.hn.receive(frame('d3', { severity: 'error' }));
  r.hn.receive(frame('d4', { severity: 'critical' }));
  r.hn.receive(frame('d1'));
  await flush();
  assert.equal(r.view.shown.length, 1);
  assert.deepEqual(r.banners.map(b => b.o.kind), ['warning', 'error', 'error']);
  assert.deepEqual(r.receipts(), ['d1', 'd2', 'd3', 'd4'], 'one receipt per delivery; the repeated d1 is ignored');
  // the text stays plain text for the renderer, the target becomes the View action
  const o = r.view.shown[0].o;
  assert.equal(o.data, true);
  assert.equal(o.title, 'T');
  assert.equal(o.message, 'D');
  assert.equal(o.actions.length, 1);
  o.actions[0].run();
  assert.deepEqual(r.gone, ['alerts']);
});

test('host:notify: untrusted title and detail reach the renderer unchanged, as strings', () => {
  const r = hostNotifyRig();
  const evil = '<img src=x onerror="window.__pwned=1"><script>alert(1)</script>';
  r.hn.receive(frame('x1', { title: evil, detail: evil, severity: 'warn' }));
  assert.equal(r.banners[0].o.title, evil);
  assert.equal(r.banners[0].o.message, evil);
  assert.equal(r.banners[0].o.data, true);
});

test('host:notify while hidden waits; shown within 3 s it is receipted, later it is shown without a receipt', async () => {
  const r = hostNotifyRig({ visible: false });
  r.hn.receive(frame('h1'));
  r.hn.receive(frame('h2', { severity: 'warn' }));
  assert.equal(r.view.shown.length + r.banners.length, 0, 'nothing rendered while hidden');
  assert.equal(r.hn.waiting, 2);
  r.clock.advance(2000);
  r.setVisible(true);
  await flush();
  assert.equal(r.view.shown.length, 1);
  assert.equal(r.banners.length, 1);
  assert.deepEqual(r.receipts(), ['h1', 'h2']);

  r.setVisible(false);
  r.hn.receive(frame('h3'));
  r.clock.advance(RECEIPT_MS + 1);
  r.setVisible(true);
  await flush();
  assert.equal(r.view.shown.length, 2, 'still shown');
  assert.deepEqual(r.receipts(), ['h1', 'h2'], 'not on screen within 3 s: no receipt (the shell records dropped)');
});

test('host:notify while hidden: at most WAITING_CAP wait, the oldest beyond are dropped (never shown, no receipt)', async () => {
  const r = hostNotifyRig({ visible: false });
  for (let i = 1; i <= WAITING_CAP + 3; i++) r.hn.receive(frame(`w${i}`, { severity: 'warn' }));
  assert.equal(r.hn.waiting, WAITING_CAP);
  r.setVisible(true);
  await flush();
  assert.equal(r.banners.length, WAITING_CAP);
  assert.equal(r.banners[0].o.id, 'host-notify:w4', 'w1..w3 were dropped');
  assert.ok(!r.receipts().includes('w1'));
  assert.ok(r.receipts().includes('w4'));
});

test('host:notify behind five toasts: receipted when it is promoted in time, never before', async () => {
  const r = hostNotifyRig();
  for (let i = 0; i < 5; i++) r.q.push({ kind: 'info', message: `local${i}` });
  r.hn.receive(frame('q1'));
  await flush();
  assert.deepEqual(r.receipts(), [], 'waiting behind the merge row is not on screen');
  r.clock.advance(1000);
  r.view.shown[0].dismiss();
  await flush();
  assert.deepEqual(r.receipts(), ['q1']);

  r.hn.receive(frame('q2'));
  r.clock.advance(RECEIPT_MS + 1);
  r.view.shown[0].dismiss();
  await flush();
  assert.deepEqual(r.receipts(), ['q1'], 'promoted after 3 s: no receipt');
});

test('host:notify: a reconnect makes the earlier deliveries another connection\'s; receipt errors are silent', async () => {
  const r = hostNotifyRig({ visible: false });
  r.hn.receive(frame('c1'));
  r.hn.connected();
  r.setVisible(true);
  await flush();
  assert.equal(r.view.shown.length, 1, 'still shown');
  assert.deepEqual(r.receipts(), [], 'no receipt for a notice of the previous connection');
  r.hn.receive(frame('c1'));
  await flush();
  assert.deepEqual(r.receipts(), ['c1'], 'the same id in a new connection is a new delivery');
  for (const code of ['not_allowed', 'unknown_delivery', 'already_receipted', 'stale_page']) {
    r.reject(code);
    r.hn.receive(frame(`e-${code}`, { severity: 'warn' }));
  }
  await flush();
  assert.equal(r.receipts().length, 5, 'tried, the rejection swallowed (no unhandled rejection)');
});

test('host:notify: malformed frames and unknown severities are dropped; an unknown target has no View action', () => {
  const r = hostNotifyRig();
  r.hn.receive({ t: 'host:notify' });
  r.hn.receive(frame('', {}));
  r.hn.receive(frame('s1', { severity: 'fatal' }));
  r.hn.receive(frame('s2', { target: 'elsewhere' }));
  assert.equal(r.view.shown.length, 1);
  assert.deepEqual(r.view.shown[0].o.actions, []);
});

// ---- 10-30 notice redesign: frames with a notice object ----------------------------------------

// the rig with the notice ports: the current page, the catalog, the body builder, toasts by id
function noticeRig({ visible = true, page = 'logs', known = id => id.startsWith('notice.') } = {}) {
  const clock = createFakeClock(0);
  const view = fakeView();
  const q = createToastQueue({ view });
  let vis = visible, current = page;
  const listeners = new Set();
  const invokes = [], banners = [], dismissed = [], dropped = [], gone = [];
  const go = target => { gone.push(target); return true; };
  go.can = t => ['overview', 'alerts', 'update'].includes(t);
  const hn = createHostNotify({
    toast: (o, hooks) => q.push(o, hooks),
    banner: o => { const h = { el: { isConnected: true }, o }; banners.push(h); return h; },
    dismiss: id => dismissed.push(id),
    visible: () => vis, onVisible: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    clock, host: (name, args) => { invokes.push([name, args]); return Promise.resolve(null); }, go, t: id => id,
    dropToast: id => { dropped.push(id); q.dismissId(id); }, page: () => current, has: known,
    message: (n, fallback) => ({ body: [n.detailId, n.reasonId, n.fixId].filter(Boolean), fallback }),
  });
  return {
    clock, view, q, hn, invokes, banners, dismissed, dropped, gone,
    setVisible(v) { vis = v; for (const fn of listeners) fn(v); },
    setPage(p) { current = p; },
    rendered: () => invokes.filter(([n]) => n === 'host_notify_rendered').map(([, a]) => a),
  };
}
const noticed = (id, incident, state = 'open', extra = {}, notice = {}) => frame(id, { severity: 'error', target: 'overview', ...extra,
  notice: { kind: 'outage', incident, state, group: 'connection', title_id: 'notice.outage.title', detail_id: 'notice.outage.detail',
    params: { minutes: 3 }, action: 'view', sticky: true, ttl_ms: 0, ...notice } });

test('notice: catalog words, one banner per incident (notice:<incident>), the action, a receipt each', async () => {
  const r = noticeRig();
  r.hn.receive(noticed('n1', 'outage-1', 'open', {}, { reason_id: 'notice.outage.reason.source_supply', fix_id: 'notice.outage.fix.source_supply' }));
  await flush();
  const o = r.banners[0].o;
  assert.equal(o.kind, 'error');
  assert.equal(o.id, 'notice:outage-1');
  assert.equal(o.titleId, 'notice.outage.title');
  assert.deepEqual(o.titleParams, { minutes: 3 });
  assert.equal(o.title, undefined, 'no fallback text when the catalog has the title');
  assert.deepEqual(o.message.body, ['notice.outage.detail', 'notice.outage.reason.source_supply', 'notice.outage.fix.source_supply']);
  assert.deepEqual(o.actions.map(a => a.labelId), ['notice.action.view']);
  o.actions[0].run();
  assert.deepEqual(r.gone, ['overview']);
  // The next frame of the incident replaces it in place (same id), and the toast of it, if any, goes.
  r.hn.receive(noticed('n2', 'outage-1', 'update', { severity: 'warn' }, { action: undefined }));
  await flush();
  assert.deepEqual(r.banners.map(b => [b.o.id, b.o.kind]), [['notice:outage-1', 'error'], ['notice:outage-1', 'warning']]);
  assert.deepEqual(r.banners[1].o.actions, [], 'no action key: no button');
  assert.deepEqual(r.dropped, ['notice:outage-1', 'notice:outage-1']);
  assert.deepEqual(r.rendered(), [{ delivery_id: 'n1' }, { delivery_id: 'n2' }]);
});

test('notice: info is a toast with the notice timing; resolved takes the banner down and says so in a success toast', async () => {
  const r = noticeRig();
  r.hn.receive(noticed('i1', 'update-0.2.0', 'open', { severity: 'info' }, { kind: 'update', group: 'update', sticky: false, ttl_ms: 8000 }));
  await flush();
  assert.deepEqual([r.view.shown[0].o.kind, r.view.shown[0].o.id, r.view.shown[0].o.timeout, r.view.shown[0].o.sticky], ['info', 'notice:update-0.2.0', 8000, undefined]);
  r.hn.receive(noticed('o1', 'outage-1'));
  r.hn.receive(noticed('o2', 'outage-1', 'resolved', { severity: 'info' }, { title_id: 'notice.outage.resolved.title', detail_id: 'notice.outage.resolved.detail', sticky: false, ttl_ms: 6000, action: undefined }));
  await flush();
  assert.ok(r.dismissed.includes('notice:outage-1'), 'the error banner goes');
  const ok = r.view.shown.find(h => h.o.kind === 'success');
  assert.deepEqual([ok.o.id, ok.o.titleId, ok.o.timeout], ['notice:outage-1', 'notice.outage.resolved.title', 6000]);
  assert.deepEqual(r.rendered().map(a => a.delivery_id), ['i1', 'o1', 'o2']);
  // A sticky info notice keeps its toast until it is closed.
  r.hn.receive(noticed('i2', 'preview', 'open', { severity: 'info' }, { kind: 'preview', group: 'preview', sticky: true }));
  await flush();
  assert.equal(r.view.shown.find(h => h.o.id === 'notice:preview').o.sticky, true);
});

test('notice: closed takes the incident down silently, at once even while hidden, and drops its waiting frame', async () => {
  const r = noticeRig();
  r.hn.receive(noticed('c1', 'capture-1'));
  r.hn.receive(noticed('c2', 'capture-1', 'closed'));
  await flush();
  assert.ok(r.dismissed.includes('notice:capture-1') && r.dropped.includes('notice:capture-1'));
  assert.deepEqual(r.rendered().map(a => a.delivery_id), ['c1'], 'a closed frame is never confirmed (the shell receipted it)');
  // The shell's own clear frame (a closed notice whose title is not needed) works the same.
  r.hn.receive(frame('shell-clear-9', { notice: { incident: 'capture-2', state: 'closed', params: {} } }));
  assert.ok(r.dismissed.includes('notice:capture-2'));
  r.setVisible(false);
  r.hn.receive(noticed('c3', 'outage-9'));
  r.hn.receive(noticed('c4', 'fallback-1', 'open', { severity: 'warn' }, { kind: 'fallback', group: 'tier_fallback' }));
  r.hn.receive(noticed('c5', 'outage-9', 'closed'));
  assert.equal(r.hn.waiting, 1, 'the closed incident\'s waiting frame never shows');
  r.setVisible(true);
  await flush();
  assert.deepEqual(r.banners.map(b => b.o.id), ['notice:capture-1', 'notice:fallback-1']);
});

test('notice: on the overview the hero already shows a connection or fallback incident: receipted silently, not shown', async () => {
  const r = noticeRig({ page: 'overview' });
  r.hn.receive(noticed('h1', 'outage-1'));
  r.hn.receive(noticed('h2', 'fallback-1', 'update', { severity: 'warn' }, { kind: 'fallback', group: 'tier_fallback' }));
  r.hn.receive(noticed('h3', 'update-0.2.0', 'open', { severity: 'info' }, { kind: 'update', group: 'update', sticky: false }));
  r.hn.receive(noticed('h4', 'outage-1', 'resolved', { severity: 'info' }, { sticky: false, title_id: 'notice.outage.resolved.title' }));
  await flush();
  assert.equal(r.banners.length, 0, 'nothing in banners on the overview');
  assert.deepEqual(r.view.shown.map(h => h.o.id), ['notice:update-0.2.0', 'notice:outage-1'], 'other groups and the recovery still show');
  assert.deepEqual(r.rendered(), [{ delivery_id: 'h1', shown: false }, { delivery_id: 'h2', shown: false }, { delivery_id: 'h3' }, { delivery_id: 'h4' }]);
  r.setPage('logs');
  r.hn.receive(noticed('h5', 'outage-2'));
  await flush();
  assert.equal(r.banners.length, 1);
});

test('notice: a new sidecar takes every notice down; a missing catalog id falls back to the text; a malformed notice renders as before', async () => {
  const r = noticeRig({ known: id => id === 'notice.outage.detail' });
  r.hn.receive(noticed('m1', 'outage-1'));
  await flush();
  assert.equal(r.banners[0].o.title, 'T', 'the title id the catalogs lack: the frame\'s title, as data');
  assert.equal(r.banners[0].o.data, true);
  assert.equal(r.banners[0].o.titleId, undefined);
  r.hn.receive(noticed('m2', 'update-1', 'open', { severity: 'info' }, { kind: 'update', group: 'update', sticky: false }));
  r.setVisible(false);
  r.hn.receive(noticed('m3', 'outage-3'));
  r.hn.epoch();
  assert.ok(r.dismissed.includes('notice:outage-1') && r.dropped.includes('notice:update-1'));
  assert.equal(r.hn.waiting, 0, 'the old sidecar\'s waiting notice never shows');
  r.setVisible(true);
  // Malformed notices: the old rendering from title / detail (host-notify:<id>).
  r.hn.receive(frame('x1', { severity: 'warn', notice: { incident: '', state: 'open', title_id: 'notice.outage.title' } }));
  r.hn.receive(frame('x2', { severity: 'warn', notice: { incident: 'a', state: 'gone', title_id: 'notice.outage.title' } }));
  r.hn.receive(frame('x3', { severity: 'warn', notice: { incident: 'a', state: 'open', title_id: '<b>x</b>' } }));
  r.hn.receive(frame('x4', { severity: 'warn', notice: 'text' }));
  await flush();
  assert.deepEqual(r.banners.slice(-4).map(b => [b.o.id, b.o.title, b.o.message]), [['host-notify:x1', 'T', 'D'], ['host-notify:x2', 'T', 'D'], ['host-notify:x3', 'T', 'D'], ['host-notify:x4', 'T', 'D']]);
});
