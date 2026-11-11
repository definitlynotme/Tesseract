// Commands, queries and their answers (T-P4-02a; P4 architecture section 2.4, PROTOCOL
// sections 2, 6, 10 and 12, shell/README T-P3-06 host commands).
//
// call(cmd, args, {owner, onProgress}) -> Promise. The pending entry is registered before
// invoke('rt') (PROTOCOL section 2). Reply modes come from spec.gen.json:
//   job      ack{accepted:false} rejects with the error or {code: skipped_reason};
//            ack{accepted:true, op} links the op; jobs edges: running(progress) -> onProgress,
//            done -> resolve(result), failed -> reject RtError(error, {result}),
//            cancelled / superseded -> reject {code: state}. A job event for an op with no ack
//            yet is kept for 5 s (PROTOCOL section 2); a job DTO with req_id links directly.
//   res      ok -> resolve(data), otherwise reject the error (reason_code kept).
//   private  nodes.reveal (the only one since the 10-30 提醒 redesign): resolved only when epoch,
//            req_id, owner and cmd all match and the owner is still live; never written to the
//            store, never emitted, and the diagnostics ring keeps only cmd + result code. A
//            mismatching answer is dropped and ends the waiter (runtime_gone / internal /
//            owner_revoked); a private call before the epoch is known is refused locally.
//   host     host.saveFile / openFolder / openUrl / reveal: a res made by the shell; its
//            reason_code (e.g. conflict + dialog_open) goes into RtError.reason_code.
// owner(label, {onRevoke}) is a page-local owner (criticism B4): revoke() rejects that owner's
// calls with owner_revoked, late frames are dropped, and the cancel calls registered with
// onRevoke are sent once, their outcome going to the diagnostics ring only (criticism B5).
import { RtError } from './errors.js';

export const STRANGER_MS = 5000;
const STRANGER_MAX = 256;
const TERMINAL = new Set(['done', 'failed', 'cancelled', 'superseded']);
const SETTLED_MAX = 512;
const DIAG_MAX = 200;

export function createCalls({ spec, ids, clock, getTransport, getEpoch, isLive = () => true }) {
  const pending = new Map(); // req_id -> entry
  const byOp = new Map(); // op -> req_id
  const strangers = new Map(); // op -> {events, cancel}
  const settledOps = new Set(); // ops whose waiter is gone (late frames are dropped)
  const owners = new Map(); // owner id -> {active, label, onRevoke, revoked}
  const promiseReq = new WeakMap();
  const cancelOnAck = new Map(); // req_id -> {owner}: revoked before its ack, cancel on arrival
  const ring = [];

  function diag(entry) {
    ring.push({ at: clock.now(), ...entry });
    if (ring.length > DIAG_MAX) ring.splice(0, ring.length - DIAG_MAX);
  }

  function modeOf(cmd) {
    if (spec.queries[cmd]) return { mode: 'res', page: true };
    const c = spec.commands[cmd];
    if (!c) return null;
    if (c.source !== 'page') return { mode: c.reply, page: false };
    if (cmd.startsWith('host.')) return { mode: 'host', page: true };
    return { mode: c.reply, page: true };
  }

  function forget(op) {
    if (!op) return;
    byOp.delete(op);
    settledOps.add(op);
    if (settledOps.size > SETTLED_MAX) settledOps.delete(settledOps.values().next().value);
  }

  function settle(p, ok, value) {
    if (p.settled) return;
    p.settled = true;
    pending.delete(p.req_id);
    forget(p.op);
    const code = ok ? 'ok' : (value && value.code) || 'internal';
    // cancel(p) issued before the ack waits for the op; when the call ends without one (ack
    // refused, shell error frame, invoke rejected, runtime_gone, owner revoked, or a terminal
    // job DTO that overtook the ack) that cancel ends too: the call's own code, or not_found
    // when the call succeeded (there is nothing left to cancel).
    if (p.cancelWanted || p.cancelWaiter) {
      const w = p.cancelWaiter;
      p.cancelWanted = false; p.cancelWaiter = null; p.cancelPromise = null;
      if (w) w.rej(new RtError(ok ? 'not_found' : code, ok || !value || value.reason_code == null ? {} : { reason_code: value.reason_code }));
    }
    diag(p.mode === 'private' ? { kind: 'call', cmd: p.cmd, code } : { kind: 'call', cmd: p.cmd, req_id: p.req_id, op: p.op || null, code });
    if (ok) p.resolve(value); else p.reject(value);
  }

  function ownerId(o) {
    if (o == null) return ids.pageOwner;
    if (typeof o === 'string') return o;
    if (typeof o === 'object' && typeof o.id === 'string') return o.id;
    return ids.pageOwner;
  }

  function send(cmd, args, owner, mode, extra = {}) {
    const req_id = ids.next();
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    const p = { req_id, cmd, args, owner, mode, resolve, reject, onProgress: extra.onProgress || null, op: null,
      epoch: getEpoch(), settled: false, cancelWanted: false, internal: !!extra.internal };
    pending.set(req_id, p);
    promiseReq.set(promise, req_id);
    const t = getTransport();
    if (!t) { settle(p, false, new RtError('runtime_unavailable')); return promise; }
    const payload = { req_id, cmd, args: args || {} };
    if (owner) payload.owner = owner;
    Promise.resolve().then(() => t.invoke('rt', payload)).then(r => { if (r && r.id != null) p.shellId = r.id; },
      e => settle(p, false, e instanceof RtError ? e : new RtError(typeof e === 'string' ? e : 'internal')));
    return promise;
  }

  function call(cmd, args, opts = {}) {
    const m = modeOf(cmd);
    if (!m || !m.page) return Promise.reject(new RtError('not_allowed'));
    const owner = ownerId(opts.owner);
    const o = owners.get(owner);
    if (o && !o.active) return Promise.reject(new RtError('owner_revoked'));
    if (!isLive()) return Promise.reject(new RtError('runtime_unavailable'));
    // A private answer is matched against the epoch it was asked in: none known yet (still
    // connecting) means it could never match.
    if (m.mode === 'private' && getEpoch() == null) return Promise.reject(new RtError('runtime_unavailable'));
    return send(cmd, args, owner, m.mode, { onProgress: opts.onProgress });
  }

  function cancelOp(op, owner) {
    return send('cancel', { op }, owner, 'cancel', { internal: true });
  }

  // cancel(op | promise): the cancel carries the original owner (the shell only lets the
  // same owner cancel, transport.rs). Before the ack the cancel waits for the op. A repeated
  // cancel of a call that is still waiting for its op (a double click) gets the same Promise:
  // one waiter, one cancel{op} once the ack arrives, and every caller sees the same outcome.
  // The shared Promise only merges clicks while the op is unknown; it never holds an outcome:
  // once the op is linked (the waiter handed over in onAck) every cancel sends a fresh
  // cancel{op}, so a cancel that failed (refused, invoke rejected, ended by a reconnect) can
  // be retried.
  function cancel(target) {
    let req = typeof target === 'string' ? byOp.get(target) : promiseReq.get(target);
    const p = req ? pending.get(req) : null;
    if (typeof target === 'string' && !p) return cancelOp(target, ids.pageOwner);
    if (!p) return Promise.reject(new RtError('not_found'));
    if (p.cancelWaiter && p.cancelPromise) return p.cancelPromise;
    if (p.op) return cancelOp(p.op, p.owner);
    p.cancelWanted = true;
    p.cancelPromise = new Promise((res, rej) => { p.cancelWaiter = { res, rej }; });
    return p.cancelPromise;
  }

  function onAck(f) {
    const p = pending.get(f.req_id);
    if (!p) {
      const later = cancelOnAck.get(f.req_id);
      if (later && f.accepted && typeof f.op === 'string') {
        // A revoked owner's job that had no op yet: cancel it now that the op is known.
        cancelOnAck.delete(f.req_id);
        forget(f.op);
        cancelOp(f.op, later.owner).then(() => diag({ kind: 'on_revoke_sent', cmd: 'cancel', code: 'ok' }),
          e => diag({ kind: 'on_revoke_sent', cmd: 'cancel', code: (e && e.code) || 'internal' }));
        return;
      }
      diag({ kind: 'ack_unmatched', req_id: f.req_id == null ? null : String(f.req_id) });
      return;
    }
    if (p.mode === 'cancel') {
      if (f.accepted) settle(p, true, { accepted: true }); else settle(p, false, f.error ? RtError.from(f.error) : new RtError(f.skipped_reason || 'cancelled', { reason_code: f.skipped_reason, skipped_reason: f.skipped_reason }));
      return;
    }
    if (p.mode !== 'job') { diag({ kind: 'ack_mode', cmd: p.cmd }); return; }
    if (!f.accepted) {
      const err = f.error ? RtError.from(f.error) : new RtError(f.skipped_reason || 'internal', { reason_code: f.skipped_reason, skipped_reason: f.skipped_reason });
      settle(p, false, err);
      return;
    }
    if (typeof f.op === 'string') {
      p.op = f.op;
      byOp.set(f.op, p.req_id);
      if (p.cancelWanted) {
        const w = p.cancelWaiter; p.cancelWanted = false; p.cancelWaiter = null; p.cancelPromise = null;
        cancelOp(f.op, p.owner).then(w && w.res, w && w.rej);
      }
      const s = strangers.get(f.op);
      if (s) { strangers.delete(f.op); s.cancel(); for (const d of s.events) onJob(d); }
    }
  }

  function onRes(f) {
    const p = pending.get(f.req_id);
    if (!p) { diag({ kind: 'res_unmatched', cmd: typeof f.cmd === 'string' ? f.cmd : null }); return; }
    if (p.mode === 'job' || p.mode === 'cancel') { diag({ kind: 'res_mode', cmd: p.cmd }); return; }
    if (p.mode === 'private') {
      const o = owners.get(p.owner);
      const live = !o || o.active;
      const match = f.epoch === p.epoch && f.owner === p.owner && f.cmd === p.cmd && f.epoch === getEpoch();
      if (!match || !live) {
        // The data is dropped; the waiter still ends (owner_revoked, runtime_gone for an epoch
        // mismatch, internal for an owner / cmd mismatch). Only the command name and a code
        // go to the ring.
        const why = !live ? 'owner_revoked' : (f.epoch !== p.epoch || f.epoch !== getEpoch() ? 'runtime_gone' : 'internal');
        diag({ kind: 'private_dropped', cmd: p.cmd, code: !live ? 'owner_revoked' : 'mismatch' });
        settle(p, false, new RtError(why));
        return;
      }
    } else if (f.cmd !== undefined && f.cmd !== p.cmd) diag({ kind: 'res_cmd_mismatch', cmd: p.cmd });
    if (f.ok) settle(p, true, f.data);
    else settle(p, false, RtError.from(f.error));
  }

  function onError(f) {
    const p = f.req_id != null ? pending.get(f.req_id) : null;
    if (!p) { diag({ kind: 'error_frame', code: f.error && f.error.code || null }); return; }
    settle(p, false, RtError.from(f.error));
  }

  function plausibleStranger(d) {
    if (d.owner != null && !owners.has(d.owner) && d.owner !== ids.pageOwner) return false;
    if (d.req_id != null) return false; // a req_id that is not ours is not ours
    for (const p of pending.values()) if (p.mode === 'job' && !p.op) return true;
    return false;
  }

  // A jobs edge (also replayed history after a reconnect).
  function onJob(d) {
    if (!d || typeof d.op !== 'string') return;
    let req = byOp.get(d.op);
    if (!req && typeof d.req_id === 'string' && pending.has(d.req_id)) {
      req = d.req_id;
      const p = pending.get(req);
      if (!p.op) { p.op = d.op; byOp.set(d.op, req); }
    }
    const p = req ? pending.get(req) : null;
    if (!p) {
      if (settledOps.has(d.op)) return;
      if (!plausibleStranger(d)) return;
      let s = strangers.get(d.op);
      if (!s) {
        if (strangers.size >= STRANGER_MAX) { const first = strangers.keys().next().value; strangers.get(first).cancel(); strangers.delete(first); }
        const op = d.op;
        s = { events: [], cancel: clock.after(STRANGER_MS, () => { strangers.delete(op); diag({ kind: 'stranger_expired', op }); }) };
        strangers.set(d.op, s);
      }
      s.events.push(d);
      return;
    }
    if (d.state === 'running' && d.progress != null && p.onProgress) {
      try { p.onProgress(d.progress); } catch (err) { setTimeout(() => { throw err; }); }
    }
    if (!TERMINAL.has(d.state)) return;
    if (d.state === 'done') settle(p, true, d.result);
    else if (d.state === 'failed') settle(p, false, RtError.from(d.error, d.result != null ? { result: d.result } : {}));
    else settle(p, false, new RtError(d.state));
  }

  // New generation: every waiter ends with runtime_gone.
  function rejectAll(code = 'runtime_gone') {
    for (const p of [...pending.values()]) settle(p, false, new RtError(code));
    for (const s of strangers.values()) s.cancel();
    strangers.clear(); byOp.clear(); settledOps.clear(); cancelOnAck.clear();
  }

  // The page dropped its Channel after a transport fault (an order jump or a stuck Channel):
  // a delivery was lost and the shell replays no ack / res / error (shell/README P3), so
  // waiters answered by those frames end now with a retryable code. Job waiters stay: the
  // replayed job history carries their req_id (PROTOCOL section 6).
  function endUnanswered(reason) {
    for (const p of [...pending.values()]) {
      if (p.mode === 'job') continue;
      settle(p, false, new RtError('runtime_unavailable', { reason_code: 'reconnected' }));
    }
    diag({ kind: 'reconnect_unanswered', reason: String(reason || '') });
  }

  function owner(label, { onRevoke } = {}) {
    const id = ids.owner(label);
    const o = { id, label: String(label || ''), active: true, onRevoke: onRevoke || null };
    owners.set(id, o);
    return {
      id,
      get active() { return o.active; },
      revoke() {
        if (!o.active) return false;
        o.active = false;
        let list = o.onRevoke;
        o.onRevoke = null;
        if (typeof list === 'function') { try { list = list(); } catch (err) { diag({ kind: 'on_revoke_threw' }); list = null; } }
        // Resolve cancel targets before the waiters go away (a promise maps to its op here).
        const plan = [];
        for (const item of [].concat(list || [])) {
          if (!item) continue;
          if (item.cancel != null) {
            const req = typeof item.cancel === 'string' ? byOp.get(item.cancel) : promiseReq.get(item.cancel);
            const p = req ? pending.get(req) : null;
            if (p && p.op) plan.push({ op: p.op, owner: p.owner });
            else if (p) plan.push({ req_id: p.req_id, owner: p.owner });
            else if (typeof item.cancel === 'string') plan.push({ op: item.cancel, owner: id });
            else diag({ kind: 'on_revoke_invalid' });
          } else if (typeof item.cmd === 'string' && modeOf(item.cmd) && modeOf(item.cmd).page) {
            plan.push({ cmd: item.cmd, args: item.args || {} });
          } else diag({ kind: 'on_revoke_invalid' });
        }
        for (const p of [...pending.values()]) if (p.owner === id && !p.internal) settle(p, false, new RtError('owner_revoked'));
        for (const step of plan) {
          if (step.req_id) { cancelOnAck.set(step.req_id, { owner: step.owner }); continue; }
          const pr = step.cmd ? send(step.cmd, step.args, id, modeOf(step.cmd).mode, { internal: true }) : cancelOp(step.op, step.owner);
          const cmd = step.cmd || 'cancel';
          pr.then(() => diag({ kind: 'on_revoke_sent', cmd, code: 'ok' }),
            e => diag({ kind: 'on_revoke_sent', cmd, code: (e && e.code) || 'internal' }));
        }
        return true;
      },
    };
  }

  return {
    call, cancel, owner, onAck, onRes, onError, onJob, rejectAll, endUnanswered,
    query: (cmd, args, opts = {}) => (spec.queries[cmd] ? call(cmd, args, opts) : Promise.reject(new RtError('not_allowed'))),
    // Args of this page's in-flight calls of `cmd` (the newest first): mode.set targets.
    inflightArgs(cmd) {
      const list = [...pending.values()].filter(p => p.cmd === cmd);
      return list.length ? list[list.length - 1].args : null;
    },
    // Whether a job DTO belongs to one of this page's calls in flight.
    owns: d => !!d && ((typeof d.op === 'string' && byOp.has(d.op)) || (typeof d.req_id === 'string' && pending.has(d.req_id))),
    pendingCount: () => pending.size,
    strangerCount: () => strangers.size,
    diag: () => ring.map(e => ({ ...e })),
    note: entry => diag(entry),
  };
}
