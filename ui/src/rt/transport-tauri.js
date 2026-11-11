// TauriTransport (T-P4-02a; P4 architecture section 2.2, shell/README P3 section,
// shell/app/src/main.rs rt / rt_connect, transport.rs Hub::connect).
//
//   connect(onDelivery, hooks?)   new Channel + invoke('rt_connect', {channel}); repeatable
//   reconnect(reason)             drop the current Channel and connect again (full replay)
//   invoke('rt', {req_id, cmd, args, owner?}) -> {queued:true, id} | rejects RtError{code}
//   invokeHost(name, args?)       any other shell invoke; string rejections -> RtError{code}
//   kind = 'tauri'
//
// Order check: within one Channel `order` must grow by exactly 1 (the shell numbers every
// delivery from one hub counter, so the first delivery of a Channel sets the baseline). A
// step back or a jump is a transport fault: the Channel is dropped and rt_connect runs again;
// the shell then replays hello, its caches, the job history and the log tail.
// The shell also emits rt:reset as a Tauri event; it is used only to notice a Channel that
// stopped delivering (the Channel copy is the one that counts).
import { RtError } from './errors.js';

export function createOrderGate() {
  let last = null;
  return {
    reset() { last = null; },
    // -> null when fine, otherwise the fault reason
    check(order) {
      if (!Number.isInteger(order) || order < 0) return 'order_invalid';
      if (last !== null && order !== last + 1) return order <= last ? 'order_backward' : 'order_jump';
      last = order;
      return null;
    },
    get last() { return last; },
  };
}

const asError = e => (e instanceof RtError ? e : new RtError(typeof e === 'string' ? e : (e && typeof e.code === 'string' ? e.code : 'internal')));

export class TauriTransport {
  constructor({ tauri = (typeof window !== 'undefined' ? window.__TAURI__ : null), clock = null, stuckMs = 1500, retryMs = 1000 } = {}) {
    if (!tauri || !tauri.core || typeof tauri.core.invoke !== 'function') throw new RtError('runtime_unavailable');
    this.kind = 'tauri';
    this.core = tauri.core;
    this.event = tauri.event || null;
    this.clock = clock;
    this.stuckMs = stuckMs;
    this.retryMs = retryMs;
    this.gate = createOrderGate();
    this.gen = 0;
    this.onDelivery = null;
    this.hooks = {};
    this.stuckCancel = null;
    this.unlisten = null;
    this.lastChannelReset = null;
  }

  now() { return this.clock ? this.clock.now() : (typeof performance !== 'undefined' ? performance.now() : Date.now()); }

  after(ms, fn) {
    if (this.clock) return this.clock.after(ms, fn);
    const id = setTimeout(fn, ms); return () => clearTimeout(id);
  }

  connect(onDelivery, hooks = {}) {
    this.onDelivery = onDelivery;
    this.hooks = hooks || {};
    this.listenResetEvent();
    return this.open('connect');
  }

  reconnect(reason = 'reconnect') {
    if (!this.onDelivery) return Promise.resolve(false);
    if (this.hooks.onReconnect) this.hooks.onReconnect(reason);
    return this.open(reason);
  }

  open(reason, attempt = 0) {
    const gen = ++this.gen;
    this.gate.reset();
    const channel = new this.core.Channel();
    channel.onmessage = delivery => this.receive(gen, delivery);
    return Promise.resolve(this.core.invoke('rt_connect', { channel })).then(() => true, err => {
      if (gen !== this.gen) return false;
      if (this.hooks.onDiag) this.hooks.onDiag({ kind: 'rt_connect_failed', code: asError(err).code, attempt });
      // Bounded retry: 1 s, then every 5 s; a newer connect() supersedes this one.
      this.after(attempt < 3 ? this.retryMs : 5 * this.retryMs, () => { if (gen === this.gen) this.open(reason, attempt + 1); });
      return false;
    });
  }

  receive(gen, delivery) {
    if (gen !== this.gen) return; // a dropped Channel: late deliveries are ignored
    const order = delivery && delivery.order;
    const fault = this.gate.check(order);
    if (fault) {
      if (this.hooks.onDiag) this.hooks.onDiag({ kind: 'transport_fault', code: fault, order, last: this.gate.last });
      this.reconnect(fault);
      return;
    }
    const frame = delivery.frame;
    if (frame && frame.t === 'rt:reset') {
      this.lastChannelReset = this.now();
      if (this.stuckCancel) { this.stuckCancel(); this.stuckCancel = null; }
    }
    this.onDelivery({ order, frame });
  }

  // rt:reset also arrives as a Tauri event. When the Channel does not bring its own copy
  // within stuckMs the Channel is considered stuck and is replaced.
  listenResetEvent() {
    if (this.unlisten || !this.event || typeof this.event.listen !== 'function') return;
    const gen = () => this.gen;
    Promise.resolve(this.event.listen('rt:reset', () => {
      // The Channel copy may come first: a Channel that delivered rt:reset just now is alive.
      if (this.lastChannelReset != null && this.now() - this.lastChannelReset < this.stuckMs) return;
      const at = gen();
      if (this.stuckCancel) this.stuckCancel();
      this.stuckCancel = this.after(this.stuckMs, () => {
        this.stuckCancel = null;
        if (at === this.gen) this.reconnect('channel_stuck');
      });
    })).then(un => { this.unlisten = un; }, () => {});
  }

  invoke(name, args) {
    if (name !== 'rt') return this.invokeHost(name, args);
    return Promise.resolve().then(() => this.core.invoke('rt', args)).catch(e => { throw asError(e); });
  }

  invokeHost(name, args) {
    return Promise.resolve().then(() => this.core.invoke(name, args || {})).catch(e => { throw asError(e); });
  }
}
