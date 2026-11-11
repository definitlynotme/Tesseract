// Shell-level banners (T-P4-06; P4 architecture sections 2.3, 2.9; mapping_core N5-N9, S2, S3,
// S5, S6, S9, S17; PROTOCOL sections 3, 13, 14; criticism B8, B9, D5, D11).
//
//   sidecar_crashed      host:banner{kind:'sidecar_crashed', state, active, exit_code, restarts,
//                        limit}: restarting -> warning banner (attempt n of limit); restarted ->
//                        a one-time success toast, the banner goes; unrecoverable -> error banner
//                        with "Open the log folder" (host.openFolder{kind:'logs'}, the scope's
//                        data/logs: shell/app/src/host.rs folder()).
//   sidecar_unresponsive host:banner{active:true, actions:['force_restart']} -> error banner with
//                        "Force restart" (invoke host_force_restart; not_unresponsive or any
//                        other error takes the banner down silently); active:false takes it down.
//                        The shell keeps both across a new epoch and replays the active one.
//   startup              service.state runtime.failure (not superseded: a stop that overtook the
//                        start is no start failure): error banner, Retry (service.start) and
//                        View logs (logs page, issues of this run). Closed by the user, it stays
//                        closed until a failure with another identity (runtime generation,
//                        lifecycle revision, code, reason_code, message_key, params), or until a
//                        start of this page (runCmd) fails again: the user tried again.
//   stop                 a service.stop job that failed (live edge): error banner, Retry.
//   cleanup              core.cleanup_pending while the service is not running: warning banner,
//                        Stop the service (kept available while the cleanup is pending).
//   recovery             hello.network_recovery with a domain other than not_needed / restored:
//                        unknown / failed / permission_required -> warning "not confirmed" (never
//                        "restored"), with "Open the data folder" (restore_proxy.bat, unkill.bat);
//                        proxy not_owned with a firewall that needs nothing -> one sentence of its
//                        own: the system proxy was taken over by another program (the overview's
//                        drift wording), not a failure. A firewall that is not_owned is told by the
//                        generic "not confirmed" body (its select names not_owned).
//   list-stale           the shell's rt:stale{topic,key,reason} for a list slot: warning "may be
//                        out of date" with Reload (invoke rt_resync per slot); a slot leaves when a
//                        complete value of it arrives. Pages may read stale slots via
//                        listStale(topic, key) and the event 'shellc:list-stale'. Only the list
//                        topics count: a state topic (bastion.pool has no value while the service
//                        is stopped) is marked where the page shows it (rt/stale.js), never as a
//                        list (F-04 follow-up, 11-01).
//   cmd-error            a command's error (S17): runtime_unavailable / job_capacity / not_allowed
//                        / internal and any other code with a message, as one error banner that
//                        replaces itself and goes after 9 s; a host.* error with a reason_code says
//                        shell.host_reason.<code> (criticism B8). cancelled / superseded /
//                        owner_revoked / runtime_gone are silent (the busy control or the crash
//                        banner says it).
// Only a generation change (rt:reset{generation_changed:true}) takes down the banners that depend
// on the old epoch (startup, stop, cleanup, recovery, list-stale, cmd-error); a same-epoch
// reconnect keeps them (criticism B9). The shell's own banners stay (it replays them), so does the
// tier banner (core/app.js; the rt layer reconciles it, T-P4-H).
// Every banner still on screen is reworded in place on a language switch (same id). A banner that
// left the screen in any way (its close button, its timeout, an action without keep, T.dismiss)
// is forgotten then, not shown again (onScreen(id), the same check as core/app.js's tier banner).
// A start (service.start / service.restart) of this page that fails with a start failure
// (errors.start.*) is told by the startup banner alone (PROTOCOL section 14: the job's error and
// runtime.failure are one Error DTO), no second, generic error banner.
//
// Ports: notify(o) -> handle (T.notify), dismiss(id) (T.dismiss), t(id, params), errorText(err),
// reasonText(ns, code), store (T.store), call(cmd, args) -> Promise (T.rt.call), can(cmd) -> bool,
// host(name, args) -> Promise, resync(topic, key) -> Promise (T.rt.requestResync via
// installShellc; default host('rt_resync')), go(page, params), serviceState() -> raw service.state | null,
// topicSub(topic, key, fn) -> off, emit(name, payload), epoch() -> the current epoch,
// onScreen(id) -> bool (T.notices.banners.has(id); default: always true).

export const CMD_ERROR_MS = 9000;
export const OK_RECOVERY = Object.freeze(['not_needed', 'restored']);
export const UNCONFIRMED_RECOVERY = Object.freeze(['unknown', 'failed', 'permission_required']);
const SILENT_CODES = new Set(['cancelled', 'superseded', 'owner_revoked', 'runtime_gone']);
const EPOCH_BANNERS = ['startup', 'stop', 'cleanup', 'recovery', 'list-stale', 'cmd-error'];
// the ID-list topics of PROTOCOL section 11 (the shell's LIST_TOPICS): the only slots the
// list-stale banner speaks of
const LIST_TOPICS = new Set(['bastion.sources', 'config.items', 'nodes.list', 'subscriptions.list']);

const json = v => { try { return JSON.stringify(v === undefined ? null : v); } catch (e) { return ''; } };

export function failureIdentity(failure, state) {
  if (!failure) return null;
  const intent = (state && state.intent) || {};
  return json([intent.runtime_generation, intent.lifecycle_revision, failure.code, failure.reason_code, failure.message_key, failure.params]);
}

// network_recovery -> null (nothing to say) | {kind:'unconfirmed', proxy, firewall} | {kind:'not_owned', proxy, firewall}
export function recoveryNotice(rec) {
  if (!rec || typeof rec !== 'object') return null;
  const st = d => (rec[d] && typeof rec[d].state === 'string' ? rec[d].state : 'unknown');
  const proxy = st('proxy'), firewall = st('firewall');
  const states = [proxy, firewall];
  if (states.every(s => OK_RECOVERY.includes(s))) return null;
  // the not_owned sentence speaks of the system proxy only
  if (proxy === 'not_owned' && OK_RECOVERY.includes(firewall)) return { kind: 'not_owned', proxy, firewall };
  return { kind: 'unconfirmed', proxy, firewall };
}

// a start failure of this page's own start: the startup banner (runtime.failure) says it
const START_CMDS = new Set(['service.start', 'service.restart']);
const isStartFailure = err => !!(err && typeof err === 'object' && typeof err.message_key === 'string' && err.message_key.startsWith('errors.start.'));

export function createBanners(p) {
  const { notify, dismiss, t, errorText, reasonText, store, call, can = () => true, host, resync = null, go, serviceState = () => null, topicSub = () => () => {}, emit = () => {}, epoch = () => null, onScreen = () => true } = p;
  const shown = new Map(); // id -> () => notify options (reworded on a language switch)
  let startupClosed = null; // identity of the startup failure the user closed
  let recoveryClosed = null;
  let recoveryKey = null;
  let stopErr = null; // the error of the stop job the stop banner shows
  const stale = new Map(); // "topic\0key" -> {topic, key, reason, off}

  function put(id, build) {
    shown.set(id, build);
    const o = build();
    return notify(Object.assign({ id }, o));
  }
  function take(id) {
    if (!shown.has(id)) return;
    shown.delete(id);
    dismiss(id);
  }
  // still on screen: a banner that left in a way this module was not told of (its timeout, an
  // action without keep) is forgotten here
  function up(id) {
    if (!shown.has(id)) return false;
    if (onScreen(id)) return true;
    shown.delete(id);
    return false;
  }
  // the user closed it (the close button): forget the builder, remember what was closed
  const closedBy = (id, after) => () => { shown.delete(id); if (after) after(); };

  // ---- sidecar (host:banner) ----------------------------------------------------------
  function hostBanner(f) {
    if (!f || typeof f.kind !== 'string') return;
    if (f.kind === 'sidecar_crashed') {
      if (f.state === 'restarting' && f.active !== false) {
        const n = Number.isInteger(f.restarts) ? f.restarts : 0, limit = Number.isInteger(f.limit) ? f.limit : 0;
        put('sidecar', () => ({ kind: 'warning', title: t('shell.sidecar.restarting.title'), message: t('shell.sidecar.restarting.body', { n, limit }),
          onDismiss: closedBy('sidecar') }));
      } else if (f.state === 'restarted') {
        take('sidecar');
        notify({ kind: 'success', message: t('shell.sidecar.restarted'), messageId: 'shell.sidecar.restarted' });
      } else if (f.state === 'unrecoverable') {
        const code = Number.isInteger(f.exit_code) ? String(f.exit_code) : 'none';
        put('sidecar', () => ({ kind: 'error', title: t('shell.sidecar.unrecoverable.title'), message: t('shell.sidecar.unrecoverable.body', { code }),
          actions: [{ label: t('shell.action.open_logs'), keep: true, run: btn => openFolder('logs', btn) }],
          onDismiss: closedBy('sidecar') }));
      } else if (f.active === false) take('sidecar');
    } else if (f.kind === 'sidecar_unresponsive') {
      if (f.active === true) {
        const force = Array.isArray(f.actions) && f.actions.includes('force_restart');
        // (F-04: no body; the title says it, the button is the way out)
        put('unresponsive', () => ({ kind: 'error', title: t('shell.sidecar.unresponsive.title'),
          actions: force ? [{ label: t('shell.sidecar.force_restart'), primary: true, keep: true, run: forceRestart }] : [],
          onDismiss: closedBy('unresponsive') }));
      } else take('unresponsive');
    }
  }
  function forceRestart() {
    Promise.resolve(host('host_force_restart')).then(() => {}, () => take('unresponsive'));
  }

  // ---- host commands with their errors -------------------------------------------------
  function openFolder(kind) {
    return Promise.resolve(call('host.openFolder', { kind })).then(() => true, err => { commandError(err); return false; });
  }

  // ---- startup failure ------------------------------------------------------------------
  function syncStartup() {
    const failure = store.get('failure');
    const run = store.get('run');
    const real = failure && failure.code !== 'superseded' && failure.reason_code !== 'superseded' && failure.message_key !== 'errors.start.superseded';
    if (!real || run === 'starting' || run === 'running') { take('startup'); return; }
    const id = failureIdentity(failure, serviceState());
    if (id === startupClosed) return;
    if (up('startup') && shown.get('startup').identity === id) return;
    // (View logs is the user dealing with it, as the close button: this failure stays closed)
    const build = () => ({ kind: 'error', title: t('shell.startup.title'), message: errorText(failure),
      actions: [
        { label: t('common.action.retry'), primary: true, run: btn => runCmd('service.start', {}, btn) },
        { label: t('common.action.view_logs'), keep: true, run: () => { if (goThen('startup', 'logs', { level: 'issue', runOnly: true })) startupClosed = id; } },
      ],
      onDismiss: closedBy('startup', () => { startupClosed = id; }) });
    build.identity = id;
    put('startup', build);
  }

  // ---- stop failure and pending cleanup -------------------------------------------------
  function stopArgs() {
    const s = serviceState();
    const g = s && s.intent && s.intent.runtime_generation;
    return Number.isInteger(g) ? { expected_generation: g } : {};
  }
  function job(e) {
    const j = e && e.job;
    if (!j || j.name !== 'service.stop') return;
    if (j.state === 'failed' && !e.instant) {
      const err = j.error || { code: 'internal' };
      stopErr = err;
      put('stop', () => ({ kind: 'error', title: t('shell.stop.failed.title'), message: errorText(err),
        actions: [{ label: t('common.action.retry'), primary: true, run: btn => runCmd('service.stop', stopArgs(), btn) }],
        onDismiss: closedBy('stop') }));
    } else if (j.state === 'done') take('stop');
  }
  function syncCleanup() {
    const core = store.get('core') || {};
    const run = store.get('run');
    if (run === 'stopped' && !core.cleanup_pending) take('stop');
    if (core.cleanup_pending && run !== 'running' && run !== 'starting' && run !== 'unknown') {
      if (up('cleanup')) return;
      put('cleanup', () => ({ kind: 'warning', title: t('shell.stop.cleanup.title'), message: t('shell.stop.cleanup.body'),
        actions: can('service.stop') ? [{ label: t('shell.stop.action'), run: btn => runCmd('service.stop', stopArgs(), btn) }] : [],
        onDismiss: closedBy('cleanup') }));
    } else take('cleanup');
  }

  // ---- network recovery (hello) ---------------------------------------------------------
  function syncRecovery() {
    const rec = store.get('recovery');
    const n = recoveryNotice(rec);
    const key = json([rec, epoch()]);
    if (!n) { recoveryKey = key; take('recovery'); return; }
    if (key === recoveryKey && (up('recovery') || recoveryClosed === key)) return;
    recoveryKey = key;
    if (recoveryClosed === key) return;
    if (n.kind === 'not_owned') {
      put('recovery', () => ({ kind: 'warning', title: t('shell.recovery.not_owned.title'), message: t('shell.recovery.not_owned.body'),
        onDismiss: closedBy('recovery', () => { recoveryClosed = key; }) }));
    } else {
      put('recovery', () => ({ kind: 'warning', title: t('shell.recovery.title'), message: t('shell.recovery.body', { proxy: n.proxy, firewall: n.firewall }),
        actions: [{ label: t('shell.action.open_data'), keep: true, run: btn => openFolder('data', btn) }],
        onDismiss: closedBy('recovery', () => { recoveryClosed = key; }) }));
    }
  }

  // ---- list slots the shell marked stale (rt:stale) ---------------------------------------
  const slotKey = (topic, key) => topic + '\u0000' + (key || '');
  function listStaleBanner() {
    if (!stale.size) { take('list-stale'); return; }
    put('list-stale', () => ({ kind: 'warning', title: t('shell.stale.title'), message: t('shell.stale.body', { count: stale.size, exhausted: [...stale.values()].some(s => s.reason === 'exhausted') ? 'yes' : 'no' }),
      actions: [{ label: t('shell.stale.reload'), primary: true, keep: true, run: reloadStale }],
      onDismiss: closedBy('list-stale') }));
  }
  function slotStale(f) {
    if (!f || typeof f.topic !== 'string' || !LIST_TOPICS.has(f.topic)) return;
    const key = typeof f.key === 'string' && f.key ? f.key : null;
    const k = slotKey(f.topic, key);
    const prev = stale.get(k);
    if (prev) { prev.reason = f.reason; }
    else {
      const entry = { topic: f.topic, key, reason: f.reason, off: null };
      entry.off = topicSub(f.topic, key, () => slotFresh(k));
      stale.set(k, entry);
    }
    emit('shellc:list-stale', { topic: f.topic, key, reason: f.reason || null, stale: true });
    listStaleBanner();
  }
  function slotFresh(k) {
    const e = stale.get(k);
    if (!e) return;
    stale.delete(k);
    if (typeof e.off === 'function') e.off();
    emit('shellc:list-stale', { topic: e.topic, key: e.key, reason: null, stale: false });
    listStaleBanner();
  }
  function reloadStale() {
    for (const e of stale.values()) {
      const p = typeof resync === 'function' ? resync(e.topic, e.key) : host('rt_resync', e.key ? { topic: e.topic, key: e.key } : { topic: e.topic });
      Promise.resolve(p).catch(() => {});
    }
  }
  function clearStale() {
    for (const e of stale.values()) if (typeof e.off === 'function') e.off();
    stale.clear();
  }

  // ---- command errors (S17) ---------------------------------------------------------------
  function errorWords(err) {
    const e = err && typeof err === 'object' ? err : { code: String(err || 'internal') };
    if (typeof e.reason_code === 'string' && e.reason_code && typeof e.message_key === 'string' && e.message_key.startsWith('errors.host.')) {
      return reasonText('shell.host_reason', e.reason_code);
    }
    return errorText(e);
  }
  function commandError(err) {
    const code = err && typeof err === 'object' ? err.code : err;
    if (SILENT_CODES.has(code)) return false;
    put('cmd-error', () => ({ kind: 'error', title: t('shell.command_error.title'), message: errorWords(err), timeout: CMD_ERROR_MS, onDismiss: closedBy('cmd-error') }));
    return true;
  }
  function runCmd(cmd, args, btn) {
    const job = Promise.resolve(call(cmd, args));
    // a stop of this page that failed as a job already has its own banner (the live jobs edge
    // reaches job() before the call settles): no second, generic one
    const same = err => !!(stopErr && err && err.code === stopErr.code && err.message_key === stopErr.message_key);
    job.catch(err => {
      if (cmd === 'service.stop' && up('stop') && same(err)) return;
      // a start failure of this page's start: runtime.failure carries the same Error, the
      // startup banner says it (PROTOCOL section 14). The user tried again, so the banner comes
      // back even when the failure has the identity the user closed (a start refused before it
      // began, e.g. a cleanup not done, keeps the lifecycle revision: T-P4-06 review cont. 1
      // SHOULD-1, 8th-wave ruling T-P4-H2 item 2)
      if (START_CMDS.has(cmd) && isStartFailure(err)) { startupClosed = null; syncStartup(); return; }
      commandError(err);
    });
    return job;
  }

  // ---- navigation that waits for the page to change (the settings page may hold the way) ---
  // -> whether the page went
  function goThen(id, page, params) {
    if (go(page, params) === false) return false;
    take(id);
    return true;
  }

  // ---- generation change ---------------------------------------------------------------------
  function reset(e) {
    if (!e || e.generation_changed !== true) return;
    for (const id of EPOCH_BANNERS) take(id);
    clearStale();
    startupClosed = null;
    recoveryClosed = null;
    recoveryKey = null;
  }

  // only the banners still on screen are worded again; the others are forgotten
  function relocalize() {
    for (const [id, build] of Array.from(shown)) {
      if (!up(id)) continue;
      const o = build();
      notify(Object.assign({ id }, o));
    }
  }

  return {
    hostBanner, syncStartup, job, syncCleanup, syncRecovery, slotStale, slotFresh, reset, relocalize,
    commandError, errorWords, runCmd, openFolder,
    listStale(topic, key) { const e = stale.get(slotKey(topic, key)); return e ? (e.reason || 'resync') : null; },
    get active() { return [...shown.keys()].filter(id => onScreen(id)); },
  };
}
