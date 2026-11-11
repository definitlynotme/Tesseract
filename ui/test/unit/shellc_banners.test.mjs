// T-P4-06: shell-level banners and the new-version notice (shellc/banners.js, update.js), with
// fake ports (no DOM): sidecar tri-state and unresponsive, start failure closed until a new
// failure, superseded is no failure, stop failure and pending cleanup, network recovery
// (unconfirmed vs not_owned, never "restored"), stale list slots, command errors with host
// reasons, generation change vs same-epoch reconnect; one announcement per version.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBanners, recoveryNotice, failureIdentity } from '../../src/shellc/banners.js';
import { createUpdateNotice, STORAGE_KEY } from '../../src/shellc/update.js';
import { makeT, flush } from './rt_helpers.mjs';

function rig({ can = () => true, hostReject = null, callReject = null } = {}) {
  const T = makeT();
  const shown = new Map(), log = [], calls = [], hosts = [], gone = [];
  const subs = new Map();
  let state = null, epoch = 'e1';
  const notify = o => {
    log.push(['notify', o.id || null, o.kind, o.title || null, o.message || null]);
    if (o.kind === 'warning' || o.kind === 'error') shown.set(o.id, o);
    return { el: {}, dismiss() {} };
  };
  const b = createBanners({
    notify, dismiss: id => { log.push(['dismiss', id]); shown.delete(id); },
    t: (id, p) => (p ? `${id}${JSON.stringify(p)}` : id),
    errorText: e => `err:${e.message_key || e.code}`, reasonText: (ns, code) => `${ns}.${code}`,
    store: T.store, can,
    call: (cmd, args) => { calls.push([cmd, args]); return callReject ? Promise.reject(callReject) : Promise.resolve({}); },
    host: (n, a) => { hosts.push([n, a]); return hostReject ? Promise.reject(Object.assign(new Error(hostReject), { code: hostReject })) : Promise.resolve(null); },
    go: (p, o) => { gone.push([p, o]); return true; },
    serviceState: () => state,
    topicSub: (topic, key, fn) => { const k = `${topic}/${key || ''}`; subs.set(k, fn); return () => subs.delete(k); },
    emit: (n, p) => T.emit(n, p), epoch: () => epoch,
    onScreen: id => shown.has(id),   // the fake screen (core.js: T.notices.banners)
  });
  return { T, b, shown, log, calls, hosts, gone, subs, setState(s) { state = s; }, setEpoch(e) { epoch = e; } };
}
// a click on a banner action as core.js wires it: an action without keep closes the banner first
function click(shown, id, i) {
  const o = shown.get(id);
  const a = o.actions[i];
  if (!a.keep) shown.delete(id);
  return a.run({});
}
const failure = (extra = {}) => ({ code: 'runtime_listener_conflict', message_key: 'errors.start.listener_conflict', params: { kind: 'mixed', port: 7890 }, reason_code: 'RuntimeListenerPreflightError', ...extra });
const svcState = (gen, rev) => ({ intent: { runtime_generation: gen, lifecycle_revision: rev } });

test('sidecar_crashed: restarting warns (attempt n of limit), restarted is a one-time toast, unrecoverable offers the log folder', async () => {
  const r = rig();
  r.b.hostBanner({ t: 'host:banner', kind: 'sidecar_crashed', state: 'restarting', active: true, exit_code: 3, restarts: 1, limit: 3 });
  assert.equal(r.shown.get('sidecar').kind, 'warning');
  assert.match(r.shown.get('sidecar').message, /"n":1,"limit":3/);
  r.b.hostBanner({ t: 'host:banner', kind: 'sidecar_crashed', state: 'restarted', active: false, exit_code: null, restarts: 1, limit: 3 });
  assert.equal(r.shown.has('sidecar'), false);
  assert.deepEqual(r.log.filter(l => l[2] === 'success').map(l => l[4]), ['shell.sidecar.restarted']);
  r.b.hostBanner({ kind: 'sidecar_crashed', state: 'unrecoverable', active: true, exit_code: null, restarts: 3, limit: 3 });
  const u = r.shown.get('sidecar');
  assert.equal(u.kind, 'error');
  assert.match(u.message, /"code":"none"/);
  assert.equal(u.actions[0].label, 'shell.action.open_logs');
  await u.actions[0].run();
  assert.deepEqual(r.calls, [['host.openFolder', { kind: 'logs' }]]);
});

test('sidecar_unresponsive: force restart; not_unresponsive takes the banner down silently; active:false takes it down', async () => {
  const r = rig({ hostReject: 'not_unresponsive' });
  r.b.hostBanner({ kind: 'sidecar_unresponsive', active: true, actions: ['force_restart'] });
  const o = r.shown.get('unresponsive');
  assert.equal(o.actions[0].label, 'shell.sidecar.force_restart');
  o.actions[0].run();
  await flush();
  assert.deepEqual(r.hosts, [['host_force_restart', undefined]]);
  assert.equal(r.shown.has('unresponsive'), false);
  r.b.hostBanner({ kind: 'sidecar_unresponsive', active: true, actions: [] });
  assert.deepEqual(r.shown.get('unresponsive').actions, []);
  r.b.hostBanner({ kind: 'sidecar_unresponsive', active: false, actions: [] });
  assert.equal(r.shown.has('unresponsive'), false);
});

test('startup failure: shown with retry / view logs; closed by the user it stays closed until a failure with another identity', async () => {
  const r = rig();
  r.setState(svcState(4, 10));
  r.T.store.patch({ run: 'failed', failure: failure() });
  r.b.syncStartup();
  const o = r.shown.get('startup');
  assert.equal(o.message, 'err:errors.start.listener_conflict');
  assert.deepEqual(o.actions.map(a => a.label), ['common.action.retry', 'common.action.view_logs']);
  o.actions[1].run();
  assert.deepEqual(r.gone, [['logs', { level: 'issue', runOnly: true }]]);
  assert.equal(r.shown.has('startup'), false, 'View logs took it down');
  r.b.syncStartup();                                  // the same failure again (a replay)
  assert.equal(r.shown.has('startup'), false, 'View logs counts as dealt with: the same failure does not come back');
  r.T.store.set('failure', failure());                // an equal copy
  r.b.syncStartup();
  r.shown.set('startup', o); o.onDismiss(); r.shown.delete('startup');   // the user closes it
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), false, 'the same failure does not come back');
  r.setState(svcState(4, 11));                        // a new start attempt failed the same way
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), true, 'a new failure identity shows again');
  r.T.store.set('run', 'starting');
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), false, 'starting again takes it down');
  r.T.store.patch({ run: 'stopped', failure: failure({ code: 'superseded', message_key: 'errors.start.superseded', reason_code: null }) });
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), false, 'a stop that overtook the start is no start failure');
  assert.notEqual(failureIdentity(failure(), svcState(1, 1)), failureIdentity(failure({ params: { kind: 'mixed', port: 7891 } }), svcState(1, 1)));
});

test('stop failure (live edge only) with retry; pending cleanup keeps 停止服务; both go once stopped and clean', async () => {
  const r = rig();
  r.setState(svcState(5, 2));
  r.b.job({ job: { op: 'o1', name: 'service.stop', state: 'failed', error: { code: 'io', message_key: 'errors.io' } }, instant: true });
  assert.equal(r.shown.has('stop'), false, 'a replayed failure is history');
  r.b.job({ job: { op: 'o2', name: 'service.stop', state: 'failed', error: { code: 'io', message_key: 'errors.io' } }, instant: false });
  const o = r.shown.get('stop');
  assert.equal(o.message, 'err:errors.io');
  o.actions[0].run();
  assert.deepEqual(r.calls, [['service.stop', { expected_generation: 5 }]]);
  r.T.store.patch({ run: 'failed', core: { cleanup_pending: true } });
  r.b.syncCleanup();
  assert.equal(r.shown.get('cleanup').actions[0].label, 'shell.stop.action');
  r.T.store.patch({ run: 'stopped', core: { cleanup_pending: false } });
  r.b.syncCleanup();
  assert.equal(r.shown.has('cleanup'), false);
  assert.equal(r.shown.has('stop'), false);
});

test('a stop of this page that fails as a job has one banner (stop), not a second generic one', async () => {
  const io = { code: 'io', message_key: 'errors.io' };
  const r = rig({ callReject: io });
  r.b.job({ job: { op: 'o3', name: 'service.stop', state: 'failed', error: io }, instant: false });
  await r.b.runCmd('service.stop', {}).catch(() => {});
  await flush();
  assert.deepEqual([...r.shown.keys()], ['stop']);
  // an immediate refusal with another error still says so
  const r2 = rig({ callReject: { code: 'job_capacity', message_key: 'errors.job_capacity' } });
  r2.b.job({ job: { op: 'o4', name: 'service.stop', state: 'failed', error: io }, instant: false });
  await r2.b.runCmd('service.stop', {}).catch(() => {});
  await flush();
  assert.deepEqual([...r2.shown.keys()].sort(), ['cmd-error', 'stop']);
});

test('sidecar_unresponsive: the force-restart label follows the language (built with the banner)', () => {
  const r = rig();
  r.b.hostBanner({ kind: 'sidecar_unresponsive', active: true, actions: ['force_restart'] });
  assert.equal(r.shown.get('unresponsive').actions[0].label, 'shell.sidecar.force_restart');
  r.b.relocalize();
  assert.equal(r.shown.get('unresponsive').actions.length, 1);
});

test('network recovery: unconfirmed domains warn with the data folder; only not_owned has its own sentence; never "restored"', async () => {
  assert.equal(recoveryNotice({ proxy: { state: 'restored' }, firewall: { state: 'not_needed' } }), null);
  assert.deepEqual(recoveryNotice({ proxy: { state: 'not_owned' }, firewall: { state: 'not_needed' } }), { kind: 'not_owned', proxy: 'not_owned', firewall: 'not_needed' });
  assert.equal(recoveryNotice({ proxy: { state: 'not_owned' }, firewall: { state: 'failed' } }).kind, 'unconfirmed');
  // the not_owned sentence is about the system proxy: a firewall not_owned is "not confirmed"
  assert.deepEqual(recoveryNotice({ proxy: { state: 'restored' }, firewall: { state: 'not_owned' } }), { kind: 'unconfirmed', proxy: 'restored', firewall: 'not_owned' });
  assert.equal(recoveryNotice({ proxy: { state: 'not_owned' }, firewall: { state: 'not_owned' } }).kind, 'unconfirmed');
  assert.equal(recoveryNotice({ proxy: { state: 'permission_required' } }).kind, 'unconfirmed', 'a missing domain is unknown');
  const r = rig();
  r.T.store.set('recovery', { proxy: { state: 'failed', error_code: 'x', remaining_count: 1 }, firewall: { state: 'restored', error_code: null, remaining_count: 0 } });
  r.b.syncRecovery();
  const o = r.shown.get('recovery');
  assert.equal(o.title, 'shell.recovery.title');
  assert.match(o.message, /"proxy":"failed","firewall":"restored"/);
  await o.actions[0].run();
  assert.deepEqual(r.calls, [['host.openFolder', { kind: 'data' }]]);
  o.onDismiss(); r.shown.delete('recovery');
  r.T.store.set('recovery', { proxy: { state: 'failed', error_code: 'x', remaining_count: 1 }, firewall: { state: 'restored', error_code: null, remaining_count: 0 } });
  r.b.syncRecovery();
  assert.equal(r.shown.has('recovery'), false, 'the same report replayed (same epoch) stays closed');
  r.T.store.set('recovery', { proxy: { state: 'not_owned', error_code: null, remaining_count: null }, firewall: { state: 'not_needed', error_code: null, remaining_count: 0 } });
  r.b.syncRecovery();
  assert.equal(r.shown.get('recovery').title, 'shell.recovery.not_owned.title');
  assert.equal(r.shown.get('recovery').actions, undefined);
  r.T.store.set('recovery', { proxy: { state: 'restored' }, firewall: { state: 'restored' } });
  r.b.syncRecovery();
  assert.equal(r.shown.has('recovery'), false);
});

test('generation change takes the epoch banners down; a same-epoch reconnect keeps them; shell banners stay', () => {
  const r = rig();
  r.setState(svcState(1, 1));
  r.T.store.patch({ run: 'failed', failure: failure() });
  r.b.syncStartup();
  r.b.hostBanner({ kind: 'sidecar_unresponsive', active: true, actions: ['force_restart'] });
  r.b.commandError({ code: 'job_capacity', message_key: 'errors.job_capacity' });
  r.b.reset({ reason: 'reconnect', epoch: 'e1', generation_changed: false });
  assert.deepEqual([...r.shown.keys()].sort(), ['cmd-error', 'startup', 'unresponsive']);
  r.b.reset({ reason: 'runtime_gone', epoch: 'e2', generation_changed: true });
  assert.deepEqual([...r.shown.keys()], ['unresponsive'], 'only the shell\'s own banner is left');
});

test('stale list slots (rt:stale): one banner, Reload resyncs each slot, a fresh complete value clears it', async () => {
  const r = rig();
  const events = [];
  r.T.on('shellc:list-stale', e => events.push(e));
  r.b.slotStale({ t: 'rt:stale', epoch: 'e1', topic: 'nodes.list', key: 'sentinel', reason: 'resync' });
  r.b.slotStale({ t: 'rt:stale', epoch: 'e1', topic: 'config.items', key: null, reason: 'exhausted' });
  assert.equal(r.b.listStale('nodes.list', 'sentinel'), 'resync');
  assert.equal(r.b.listStale('config.items'), 'exhausted');
  const o = r.shown.get('list-stale');
  assert.match(o.message, /"count":2,"exhausted":"yes"/);
  o.actions[0].run();
  await flush();
  assert.deepEqual(r.hosts, [['rt_resync', { topic: 'nodes.list', key: 'sentinel' }], ['rt_resync', { topic: 'config.items' }]]);
  r.subs.get('nodes.list/sentinel')();
  assert.equal(r.b.listStale('nodes.list', 'sentinel'), null);
  r.subs.get('config.items/')();
  assert.equal(r.shown.has('list-stale'), false);
  assert.deepEqual(events.map(e => [e.topic, e.stale]), [['nodes.list', true], ['config.items', true], ['nodes.list', false], ['config.items', false]]);
});

test('a stale state topic is not a list: bastion.pool out of attempts (no value while stopped) raises no banner', () => {
  const r = rig();
  const events = [];
  r.T.on('shellc:list-stale', e => events.push(e));
  r.b.slotStale({ t: 'rt:stale', epoch: 'e1', topic: 'bastion.pool', key: null, reason: 'exhausted' });
  r.b.slotStale({ t: 'rt:stale', epoch: 'e1', topic: 'network.mode', key: null, reason: 'resync' });
  assert.equal(r.shown.has('list-stale'), false);
  assert.equal(r.b.listStale('bastion.pool'), null);
  assert.deepEqual(events, []);
  // a list slot next to it still counts alone
  r.b.slotStale({ t: 'rt:stale', epoch: 'e1', topic: 'bastion.sources', key: null, reason: 'exhausted' });
  assert.match(r.shown.get('list-stale').message, /"count":1,"exhausted":"yes"/);
});

test('command errors: one replacing banner for 9 s; host reasons are shell.host_reason.<code>; ends of a job are silent', () => {
  const r = rig();
  assert.equal(r.b.commandError({ code: 'cancelled' }), false);
  assert.equal(r.b.commandError({ code: 'runtime_gone' }), false);
  for (const code of ['runtime_unavailable', 'job_capacity', 'not_allowed', 'internal']) {
    assert.equal(r.b.commandError({ code, message_key: `errors.${code}` }), true);
    assert.equal(r.shown.get('cmd-error').message, `err:errors.${code}`);
    assert.equal(r.shown.get('cmd-error').timeout, 9000);
  }
  r.b.commandError({ code: 'conflict', message_key: 'errors.host.conflict', reason_code: 'dialog_open' });
  assert.equal(r.shown.get('cmd-error').message, 'shell.host_reason.dialog_open');
  assert.equal(r.b.errorWords({ code: 'io', message_key: 'errors.host.io', reason_code: 'folder_missing' }), 'shell.host_reason.folder_missing');
  assert.equal(r.b.errorWords({ code: 'conflict', message_key: 'errors.exit.in_progress', reason_code: 'in_progress' }), 'err:errors.exit.in_progress', 'a sidecar reason stays with its message');
});

test('relocalize rewords every banner on screen in place (same ids)', () => {
  const r = rig();
  r.b.hostBanner({ kind: 'sidecar_crashed', state: 'restarting', active: true, exit_code: 1, restarts: 2, limit: 3 });
  r.b.commandError({ code: 'internal', message_key: 'errors.internal' });
  const before = r.log.length;
  r.b.relocalize();
  assert.deepEqual(r.log.slice(before).map(l => l[1]).sort(), ['cmd-error', 'sidecar']);
});

test('relocalize: a banner that left the screen without its close button (timeout, an action without keep) does not come back', () => {
  const r = rig();
  r.b.hostBanner({ kind: 'sidecar_crashed', state: 'restarting', active: true, exit_code: 1, restarts: 2, limit: 3 });
  r.b.commandError({ code: 'internal', message_key: 'errors.internal' });
  r.shown.delete('cmd-error');                        // its 9 s ran out (core.js: handle.dismiss, no onDismiss)
  const before = r.log.length;
  r.b.relocalize();
  assert.deepEqual(r.log.slice(before).map(l => l[1]), ['sidecar']);
  assert.deepEqual([...r.shown.keys()], ['sidecar']);
  assert.deepEqual(r.b.active, ['sidecar']);
  r.b.relocalize();
  assert.deepEqual([...r.shown.keys()], ['sidecar'], 'a second switch neither');
});

test('relocalize: startup Retry and stop Retry (no keep) do not bring their banners back on a language switch', async () => {
  const r = rig();
  r.setState(svcState(2, 3));
  r.T.store.patch({ run: 'failed', failure: failure() });
  r.b.syncStartup();
  r.b.job({ job: { op: 'o9', name: 'service.stop', state: 'failed', error: { code: 'io', message_key: 'errors.io' } }, instant: false });
  click(r.shown, 'startup', 0);
  click(r.shown, 'stop', 0);
  await flush();
  r.b.relocalize();
  assert.deepEqual([...r.shown.keys()], []);
});

test('cleanup: Stop the service closes the banner; while the cleanup is still pending the next sync brings it back', async () => {
  const r = rig();
  r.T.store.patch({ run: 'stopped', core: { cleanup_pending: true } });
  r.b.syncCleanup();
  click(r.shown, 'cleanup', 0);
  await flush();
  assert.deepEqual(r.calls, [['service.stop', {}]]);
  assert.equal(r.shown.has('cleanup'), false);
  r.b.relocalize();
  assert.equal(r.shown.has('cleanup'), false, 'not reworded back on a language switch');
  r.T.store.patch({ run: 'failed', core: { cleanup_pending: true } });   // the stop ended, still pending
  r.b.syncCleanup();
  assert.equal(r.shown.has('cleanup'), true, 'the cleanup is still pending: the banner is back');
  r.T.store.patch({ run: 'stopped', core: { cleanup_pending: false } });
  r.b.syncCleanup();
  assert.equal(r.shown.has('cleanup'), false);
});

test('a start of this page that fails with a start failure: only the startup banner, no generic error banner', async () => {
  const startErr = { code: 'runtime_listener_conflict', message_key: 'errors.start.listener_conflict', params: { kind: 'mixed', port: 7890 }, reason_code: 'RuntimeListenerPreflightError' };
  const r = rig({ callReject: startErr });
  r.setState(svcState(2, 3));
  r.T.store.patch({ run: 'failed', failure: failure() });
  r.b.syncStartup();
  // Retry on the startup banner: the job fails the same way; service.state says it again
  await click(r.shown, 'startup', 0).catch(() => {});
  r.setState(svcState(2, 4));
  r.T.store.patch({ run: 'failed', failure: failure() });
  r.b.syncStartup();
  await flush();
  assert.deepEqual([...r.shown.keys()], ['startup']);
  // the command palette's start and a restart: the same
  await r.b.runCmd('service.start', {}).catch(() => {});
  await r.b.runCmd('service.restart', {}).catch(() => {});
  await flush();
  assert.deepEqual([...r.shown.keys()], ['startup']);
  assert.equal(r.log.filter(l => l[1] === 'cmd-error').length, 0);
  // a start refused for another reason still says so
  const r2 = rig({ callReject: { code: 'job_capacity', message_key: 'errors.job_capacity' } });
  await r2.b.runCmd('service.start', {}).catch(() => {});
  await flush();
  assert.deepEqual([...r2.shown.keys()], ['cmd-error']);
  // a stop that fails with an errors.start.* key is not a start: the generic banner
  const r3 = rig({ callReject: startErr });
  await r3.b.runCmd('service.stop', {}).catch(() => {});
  await flush();
  assert.deepEqual([...r3.shown.keys()], ['cmd-error']);
});

test('T-P4-H2: the user closed the startup banner, then a start of this page fails with the same identity: the banner comes back', async () => {
  // a start refused before it began (cleanup not done) keeps the lifecycle revision
  // (desktop_runtime start path): the failure identity does not change
  const startErr = { code: 'runtime_listener_conflict', message_key: 'errors.start.listener_conflict', params: { kind: 'mixed', port: 7890 }, reason_code: 'RuntimeListenerPreflightError' };
  const r = rig({ callReject: startErr });
  r.setState(svcState(4, 10));
  r.T.store.patch({ run: 'failed', failure: failure() });
  r.b.syncStartup();
  const o = r.shown.get('startup');
  o.onDismiss(); r.shown.delete('startup');           // the user closes it
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), false, 'closed: a replay of the same failure stays closed');
  await r.b.runCmd('service.start', {}).catch(() => {}); // the palette's 启动服务, same identity
  await flush();
  assert.equal(r.shown.has('startup'), true, 'the user tried again: the banner says the result again');
  assert.deepEqual([...r.shown.keys()], ['startup'], 'still no generic error banner');
  // closed again and replayed: stays closed until the next try
  r.shown.get('startup').onDismiss(); r.shown.delete('startup');
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), false);
  // a start failure while the store still says starting: shown once the run leaves starting
  r.T.store.set('run', 'starting');
  await r.b.runCmd('service.start', {}).catch(() => {});
  await flush();
  assert.equal(r.shown.has('startup'), false, 'not while starting');
  r.T.store.set('run', 'failed');
  r.b.syncStartup();
  assert.equal(r.shown.has('startup'), true);
});

// ---- update ----------------------------------------------------------------------------------------
function storage({ broken = false } = {}) {
  const m = new Map();
  return { m, getItem: k => { if (broken) throw new Error('denied'); return m.has(k) ? m.get(k) : null; }, setItem: (k, v) => { if (broken) throw new Error('denied'); m.set(k, v); } };
}
function updateRig({ can = true, store = storage(), confirm = true } = {}) {
  const shown = new Map(), toasts = [], calls = [], gone = [];
  const u = createUpdateNotice({
    notify: o => { if (o.kind === 'success') toasts.push(o.message); else shown.set(o.id, o); },
    dismiss: id => shown.delete(id), t: (id, p) => (p ? `${id}${JSON.stringify(p)}` : id),
    can: () => can, call: (c, a) => { calls.push([c, a]); return Promise.resolve({}); }, confirm: () => Promise.resolve(confirm),
    go: (p, o) => gone.push([p, o]), storage: store, onScreen: id => shown.has(id),
  });
  return { u, shown, toasts, calls, gone, store };
}
const avail = (v, extra = {}) => ({ state: 'available', version: v, update_available: true, installable: true, error: null, current_version: '1.0.0', previous_version: null, ...extra });

test('new version: one banner per version (two actions when installable), remembered in localStorage', async () => {
  const r = updateRig();
  r.u.state(avail('1.1.0'));
  const o = r.shown.get('update');
  // (F-01: information, not a warning: a banner without a level word)
  assert.equal(o.kind, 'info'); assert.equal(o.banner, true);
  assert.deepEqual(o.actions.map(a => a.label), ['shell.update.view', 'shell.update.install']);
  o.actions[0].run();
  assert.deepEqual(r.gone, [['settings', { focus: 'update' }]]);
  await o.actions[1].run();
  assert.deepEqual(r.calls, [['app.update.install', {}]]);
  r.u.state(avail('1.1.0'));
  assert.deepEqual(JSON.parse(r.store.m.get(STORAGE_KEY)).announced, ['1.1.0']);
  // a reload: a new page with the same storage does not announce it again
  const again = updateRig({ store: r.store });
  again.u.state(avail('1.1.0'));
  assert.equal(again.shown.has('update'), false);
  again.u.state(avail('1.2.0'));
  assert.equal(again.shown.has('update'), true, 'a newer version is announced');
  again.u.state({ state: 'idle', version: null, update_available: false, installable: false, error: null });
  assert.equal(again.shown.has('update'), false, 'no longer available: the banner goes');
});

test('new version: only "View update" when it cannot be installed in the app or the command is not offered', () => {
  for (const [rigOpts, extra] of [[{ can: false }, {}], [{}, { installable: false }], [{}, { enabled: false }]]) {
    const r = updateRig(rigOpts);
    r.u.state(avail('2.0.0', extra));
    assert.deepEqual(r.shown.get('update').actions.map(a => a.label), ['shell.update.view']);
  }
});

test('new version: storage that throws -> once per page session; a declined confirmation installs nothing', async () => {
  const r = updateRig({ store: storage({ broken: true }), confirm: false });
  r.u.state(avail('3.0.0'));
  assert.equal(r.shown.has('update'), true);
  await r.u.install();
  assert.deepEqual(r.calls, []);
  r.shown.delete('update');
  r.u.state(avail('3.0.0'));
  assert.equal(r.shown.has('update'), false, 'remembered for this page session');
});

test('new version: after "View update" (no keep) or a close, a language switch does not bring the banner back', () => {
  const r = updateRig();
  r.u.state(avail('1.4.0'));
  const before = r.shown.get('update');
  r.u.relocalize();
  assert.notEqual(r.shown.get('update'), before, 'on screen: reworded in place');
  click(r.shown, 'update', 0);
  assert.deepEqual(r.gone, [['settings', { focus: 'update' }]]);
  r.u.relocalize();
  assert.equal(r.shown.has('update'), false);
  assert.equal(r.u.shownFor, null);
  r.u.state(avail('1.4.0', { installable: false }));
  assert.equal(r.shown.has('update'), false, 'a change of installability does not bring it back either');
});

test('update finished: previous_version shows a one-time notice per current version', () => {
  const store = storage();
  const r = updateRig({ store });
  const done = { state: 'idle', version: null, update_available: false, installable: false, error: null, current_version: '1.1.0', previous_version: '1.0.0' };
  r.u.state(done);
  r.u.state(done);
  assert.equal(r.toasts.length, 1);
  assert.match(r.toasts[0], /"previous":"1.0.0","current":"1.1.0"/);
  const again = updateRig({ store });
  again.u.state(done);
  assert.equal(again.toasts.length, 0, 'not again after a reload');
});
