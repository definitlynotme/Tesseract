// FakeTransport (T-P4-02a; P4 architecture section 2.2, 2.7). Same shape as TauriTransport.
// T-P4-02a implements the interface and the fixture mode; T-P4-02b adds the sim mode.
//
// Sim mode (design target; new FakeTransport({mode: 'sim', sim, lang})): `sim` is a backend
// port that stands where the shell and the sidecar stand in the app target:
//   sim.replay() -> [frame...]   what the shell replays on every rt_connect (rt:reset,
//                                hello, the cached snapshots, the log tail, the job history)
//   sim.attach(push, diag)       the backend pushes its live frames through push(frame);
//                                diag(entry) records a backend diagnostic (T.rt.diag())
//   sim.invoke(payload)          one page command or query {id, req_id, cmd, args, owner}; the
//                                answer (ack + jobs, res, or error) comes back through push,
//                                echoing `id` (the id invoke() resolved with)
// Delivery is synchronous: the replay inside connect(), a live frame at the moment the backend
// pushes it. host_* invokes resolve null (the design target has no shell); rt_resync is not
// offered (host:caps is never sent), so a refetch reconnects and the replay recovers it.
//
// Scenario (ui/test/fixtures/scenarios/<name>.json):
//   { "name", "desc", "lang"?, "allow_host"?: [invoke names answered with null],
//     "steps": [
//       {"deliver": <frame>, "order"?: n}        one Channel delivery (order defaults to +1)
//       {"advance_ms": n}                         drive the injected fake clock
//       {"jump_ms": n}                            the machine slept: time moves, no timer ran
//       {"on_invoke": {"cmd", "args"?} | {"host", "args"?},
//        "reply"?: [<frame>...], "reject"?: "<code>", "result"?: <value>}
//     ],
//     "replay"?: [<frame>...]                     delivered on every later rt_connect }
// on_invoke entries are registrations, not waits: each answers the first matching call
// (args match as a subset) once. "$req_id", "$owner", "$cmd" in a reply are replaced by the
// call's values; ack / res / error replies get req_id / owner filled in when absent. A call
// that matches nothing is recorded in `failures` (tests assert it stays empty) and rejected
// with not_allowed, like an unknown command at the shell.
// Design target: `lang` (from ?lang) rewrites hello.lang in delivered hello frames (criticism
// E7), and host:language frames too so the shell's language push cannot undo it; otherwise
// the scenario's own "lang" rewrites hello.lang, when it has one.
import { RtError } from './errors.js';
import { createOrderGate } from './transport-tauri.js';

const clone = v => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
const SIM_INVOCATIONS = 200;

function subsetMatch(want, got) {
  if (want === undefined) return true;
  if (want === null || typeof want !== 'object') return want === got;
  if (!got || typeof got !== 'object') return false;
  if (Array.isArray(want)) return Array.isArray(got) && want.length === got.length && want.every((w, i) => subsetMatch(w, got[i]));
  return Object.keys(want).every(k => subsetMatch(want[k], got[k]));
}

function substitute(v, vars) {
  if (typeof v === 'string') return Object.prototype.hasOwnProperty.call(vars, v) ? vars[v] : v;
  if (Array.isArray(v)) return v.map(x => substitute(x, vars));
  if (v && typeof v === 'object') { const o = {}; for (const k of Object.keys(v)) o[k] = substitute(v[k], vars); return o; }
  return v;
}

export class FakeTransport {
  constructor({ scenario = null, mode = 'fixture', clock = null, lang = null, sim = null } = {}) {
    if (mode === 'sim' && !(sim && typeof sim.replay === 'function' && typeof sim.attach === 'function' && typeof sim.invoke === 'function')) {
      throw new Error('FakeTransport: mode sim requires a sim backend with replay(), attach() and invoke()');
    }
    if (mode !== 'fixture' && mode !== 'sim') throw new Error(`FakeTransport: unknown mode ${mode} (expected fixture or sim)`);
    this.sim = mode === 'sim' ? sim : null;
    const sc = Array.isArray(scenario) ? { steps: scenario } : (scenario || { steps: [] });
    this.kind = 'fake';
    this.mode = mode;
    this.scenario = sc;
    this.clock = clock;
    this.lang = lang || sc.lang || null;
    this.langParam = lang || null;
    this.allowHost = new Set(Array.isArray(sc.allow_host) ? sc.allow_host : []);
    this.handlers = [];
    for (const step of sc.steps || []) if (step && step.on_invoke) this.handlers.push({ ...step, used: false });
    this.failures = [];
    this.invocations = [];
    this.order = Number.isInteger(sc.order_start) ? sc.order_start - 1 : 0;
    this.gate = createOrderGate();
    this.connects = 0;
    this.gen = 0;
    this.onDelivery = null;
    this.hooks = {};
    this.nextId = 1;
    this.idle = Promise.resolve();
  }

  connect(onDelivery, hooks = {}) {
    this.onDelivery = onDelivery;
    this.hooks = hooks || {};
    return this.open();
  }

  reconnect(reason = 'reconnect') {
    if (!this.onDelivery) return Promise.resolve(false);
    if (this.hooks.onReconnect) this.hooks.onReconnect(reason);
    return this.open();
  }

  open() {
    const gen = ++this.gen;
    const first = this.connects++ === 0;
    this.gate.reset();
    if (this.sim) {
      this.sim.attach(frame => this.push(this.gen, frame), d => { if (this.hooks.onDiag) this.hooks.onDiag(d); });
      for (const f of this.sim.replay()) this.push(gen, f);
      this.idle = Promise.resolve();
      return this.idle.then(() => true);
    }
    const run = () => {
      if (gen !== this.gen) return;
      if (first) this.runSteps(gen, this.scenario.steps || []);
      else for (const f of this.scenario.replay || []) this.push(gen, f);
    };
    this.idle = Promise.resolve().then(run);
    return this.idle.then(() => true);
  }

  runSteps(gen, steps) {
    for (const step of steps) {
      if (gen !== this.gen) return; // a fault reconnected meanwhile
      if (!step || typeof step !== 'object' || step.on_invoke) continue;
      if (step.deliver) this.push(gen, step.deliver, step.order);
      else if (Number.isFinite(step.advance_ms)) { if (this.clock && this.clock.advance) this.clock.advance(step.advance_ms); }
      else if (Number.isFinite(step.jump_ms)) { if (this.clock && this.clock.jump) this.clock.jump(step.jump_ms); }
    }
  }

  // Programmatic delivery (tests, the sim mode of T-P4-02b). An explicit order simulates a
  // broken Channel.
  deliver(frame, order) { this.push(this.gen, frame, order); }

  push(gen, frame, order) {
    if (gen !== this.gen || !this.onDelivery) return;
    let f = clone(frame);
    if (f && f.t === 'hello' && this.lang) f = { ...f, lang: this.lang };
    if (f && f.t === 'host:language' && this.langParam) f = { ...f, language: this.langParam };
    const o = Number.isInteger(order) ? order : this.order + 1;
    this.order = Math.max(this.order, o);
    const fault = this.gate.check(o);
    if (fault) {
      if (this.hooks.onDiag) this.hooks.onDiag({ kind: 'transport_fault', code: fault, order: o, last: this.gate.last });
      this.reconnect(fault);
      return;
    }
    this.onDelivery({ order: o, frame: f });
  }

  match(kind, name, args) {
    for (const h of this.handlers) {
      if (h.used) continue;
      const on = h.on_invoke;
      if (kind === 'rt' ? on.cmd !== name : on.host !== name) continue;
      if (!subsetMatch(on.args, args)) continue;
      h.used = true;
      return h;
    }
    return null;
  }

  invoke(name, payload) {
    if (name !== 'rt') return this.invokeHost(name, payload);
    const p = payload || {};
    this.record({ kind: 'rt', cmd: p.cmd, args: clone(p.args), owner: p.owner, req_id: p.req_id });
    if (this.sim) {
      if (!this.onDelivery) return Promise.reject(new RtError('runtime_unavailable'));
      // the id handed back here is the one the backend echoes in its ack / res / error
      const id = this.nextId++;
      this.sim.invoke(clone({ id, req_id: p.req_id, cmd: p.cmd, args: p.args || {}, owner: p.owner }));
      return Promise.resolve({ queued: true, id });
    }
    const h = this.match('rt', p.cmd, p.args || {});
    if (!h) {
      this.failures.push({ kind: 'unmatched_invoke', cmd: p.cmd, args: clone(p.args) });
      return Promise.reject(new RtError('not_allowed'));
    }
    if (h.reject) return Promise.reject(new RtError(h.reject));
    const id = this.nextId++;
    const gen = this.gen;
    const vars = { $req_id: p.req_id, $owner: p.owner, $cmd: p.cmd };
    const replies = (h.reply || []).map(f => {
      const r = substitute(clone(f), vars);
      if (r && (r.t === 'ack' || r.t === 'res' || r.t === 'error')) {
        if (r.req_id === undefined) r.req_id = p.req_id;
        if (r.owner === undefined && p.owner !== undefined) r.owner = p.owner;
      }
      return r;
    });
    this.idle = this.idle.then(() => { for (const r of replies) this.push(gen, r); });
    return Promise.resolve({ queued: true, id });
  }

  // Every invoke is kept for the tests; the sim mode (a design page left open for hours) keeps
  // only the latest SIM_INVOCATIONS.
  record(entry) {
    this.invocations.push(entry);
    if (this.sim && this.invocations.length > SIM_INVOCATIONS) this.invocations.splice(0, this.invocations.length - SIM_INVOCATIONS);
  }

  invokeHost(name, args) {
    this.record({ kind: 'host', name, args: clone(args) });
    if (this.sim) return name === 'rt_resync' ? Promise.reject(new RtError('not_allowed')) : Promise.resolve(null);
    const h = this.match('host', name, args || {});
    if (!h) {
      if (this.allowHost.has(name)) return Promise.resolve(null);
      this.failures.push({ kind: 'unmatched_host', name, args: clone(args) });
      return Promise.reject(new RtError('not_allowed'));
    }
    if (h.reject) return Promise.reject(new RtError(h.reject));
    const gen = this.gen;
    const replies = (h.reply || []).map(clone);
    this.idle = this.idle.then(() => { for (const r of replies) this.push(gen, r); });
    return Promise.resolve(h.result === undefined ? null : clone(h.result));
  }
}
