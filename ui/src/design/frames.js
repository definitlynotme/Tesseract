/* ==========================================================================
   design/frames.js — the design target's frame generator (T-P4-02b; P4
   architecture §2.6 last item, §2.7; PROTOCOL §2, §4, §5, §6, §11, §16)
   --------------------------------------------------------------------------
   The simulator (design/sim.js) keeps its own facts; this file turns them into
   v1 sidecar frames — the same frames a real sidecar would send through the
   shell — and FakeTransport delivers them to T.rt, which projects them onto the
   store keys and rt:* events exactly as in the app target.

   Fields: closed sets of PROTOCOL §5, §11, §16 only. Prototype facts the contract
   has no place for are dropped here (node region / host / port / extra protocols,
   the exit's city / time zone / ASN / organisation / network type, subscription
   URLs and counts beyond the loaded nodes, Bastion's fill phase and rotation time,
   log fix hints and metadata); the T-P4-02b report lists them.
   Never produced: the shell-only topics (alerts.desktop, ui.language, shutdown,
   update.prepareExit); validate_ui_fixtures.py rejects them in a page stream.

   Publisher
     pub.state(topic, data, {edge, key})   state / keyed-list topics: queued when the
                                           data changed (volatile time fields ignored)
     pub.list(topic, key, items)           list topics: snapshot first, then patches
     pub.event(topic, data)                edge-only topics (tier.transition, config.changed)
     pub.job(dto, {before})                a jobs edge (continuous seq)
     pub.log(records)                      v1 Log rows -> one logs.batch edge per flush
     pub.take()                            the queued frames, one batch (or one evt)
     pub.replay(hello)                     what the shell replays on rt_connect
   ========================================================================== */

export const EPOCH = 'design-1';
export const TIER_IDS = ['vanguard', 'sentinel', 'bastion', 'gateway'];

/* hello.caps (PROTOCOL §3, §10 页面白名单): every v1 page runtime command and query, and every
   topic a page can receive. host.* run in the shell (never in caps); the shell-only commands
   and topics are not for pages. Unmapped commands answer not_allowed (design/sim.js). */
export const PAGE_COMMANDS = Object.freeze([
  'alerts.onboarding.cancel', 'alerts.onboarding.complete', 'alerts.onboarding.poll', 'alerts.onboarding.start',
  'alerts.test', 'app.update.check', 'app.update.install', 'exit.verify', 'gateway.select', 'geo.update',
  'import.apply', 'import.preview', 'logs.exportBundle', 'logs.exportFiltered', 'logs.setMihomoLevel', 'mode.set',
  'nodes.cancelDiagnose', 'nodes.delete', 'nodes.diagnose', 'nodes.reveal', 'nodes.update', 'service.restart',
  'service.start', 'service.stop', 'settings.apply', 'settings.validate', 'subscriptions.refresh',
]);
export const PAGE_QUERIES = Object.freeze([
  'alerts.catalog', 'alerts.deliveries', 'app.info', 'autostart.state', 'config.snapshot', 'geo.status',
  'import.specs', 'logs.query', 'logs.tail', 'nodes.detail',
]);
export const PAGE_TOPICS = Object.freeze([
  'alerts.delivery', 'alerts.history', 'alerts.onboarding', 'bastion.pool', 'bastion.sources', 'config.changed',
  'config.items', 'core.info', 'exit.identity', 'gateway.current', 'geo.status', 'jobs', 'killswitch.state',
  'logs.batch', 'logs.feed', 'network.mode', 'nodes.list', 'protocols.mix', 'route.published', 'service.collect',
  'service.state', 'subscriptions.list', 'tier.transition', 'tiers.summary', 'traffic.history', 'traffic.tick',
  'update.state',
]);

/* Delivery order inside one flush: the facts a page reads in an rt:state handler (kill switch,
   tiers, exit, traffic) are settled before service.state announces the change, as the
   prototype simulator settled them before sim:state. */
const ORDER = [
  'network.mode', 'killswitch.state', 'bastion.pool', 'bastion.sources', 'tiers.summary', 'nodes.list',
  'subscriptions.list', 'config.items', 'service.collect', 'route.published', 'core.info', 'gateway.current',
  'protocols.mix', 'exit.identity', 'traffic.history', 'traffic.tick', 'service.state', 'logs.feed', 'update.state',
  'geo.status', 'alerts.history', 'alerts.delivery', 'alerts.onboarding',
];
const rank = topic => { const i = ORDER.indexOf(topic); return i < 0 ? ORDER.length : i; };
const VOLATILE = new Set(['sampled_at_ms', 'observed_at_ms', 'updated_at_ms', 'snapshot_revision']);
const cmpOf = data => JSON.stringify(data, (k, v) => (VOLATILE.has(k) ? undefined : v));
const clone = v => JSON.parse(JSON.stringify(v));

export function createPublisher({ epoch = EPOCH, now = () => Date.now() } = {}) {
  const cur = new Map();          // topic \0 key -> {topic, key, rev, seq, data, cmp, list}
  let queued = new Map();         // topic \0 key -> evt (the newest of this flush)
  let before = [], after = [], logRows = [];
  let tickSeq = 0, jobsRev = 0, jobsSeq = 0, logsRev = 0, logsSeq = 0;
  const jobsLog = [];             // replayed after a reconnect (the shell caches job history)
  const logRing = [];             // every Log row of this epoch (logs.tail / logs.query / replay)
  const LOG_RING_MAX = 4000;

  const env = t => { const at = now(); return { t, epoch, produced_at_ms: at, sent_at_ms: at }; };
  const evt = (topic, rev, kind, data, extra) => ({ ...env('evt'), topic, rev, kind, data: clone(data), ...(extra || {}) });
  const slot = (topic, key) => topic + '\u0000' + (key || '');

  function state(topic, data, { edge = false, key = null, force = false } = {}) {
    const k = slot(topic, key);
    const c = cur.get(k) || { topic, key, rev: 0, seq: 0, data: null, cmp: null, list: false };
    const cmp = cmpOf(data);
    if (!force && c.cmp === cmp) return false;
    c.rev += 1; c.data = clone(data); c.cmp = cmp;
    const extra = key ? { key } : {};
    if (edge) { c.seq += 1; extra.seq = c.seq; }
    cur.set(k, c);
    queued.set(k, evt(topic, c.rev, edge ? 'edge' : 'snapshot', data, extra));
    return true;
  }

  // List topics (nodes.list / subscriptions.list keyed, bastion.sources, config.items): the
  // first value is a snapshot, later ones a patch against the previous rev (changed rows are
  // complete rows, removed are ids; PROTOCOL §4, §11). A patch queued in the same flush as the
  // snapshot it follows folds into that snapshot.
  function list(topic, key, items) {
    const k = slot(topic, key);
    const c = cur.get(k);
    const rows = items.map(clone);
    if (!c) {
      cur.set(k, { topic, key, rev: 1, seq: 0, data: { items: rows }, cmp: cmpOf({ items: rows }), list: true });
      queued.set(k, evt(topic, 1, 'snapshot', { items: rows }, key ? { key } : {}));
      return true;
    }
    const prev = new Map(c.data.items.map(r => [r.id, r]));
    const next = new Map(rows.map(r => [r.id, r]));
    const changed = rows.filter(r => !prev.has(r.id) || cmpOf(prev.get(r.id)) !== cmpOf(r));
    const removed = [...prev.keys()].filter(id => !next.has(id));
    if (!changed.length && !removed.length && c.data.items.map(r => r.id).join() === rows.map(r => r.id).join()) return false;
    const pending = queued.get(k);
    c.data = { items: rows }; c.cmp = cmpOf(c.data);
    if (pending && pending.kind === 'snapshot') {
      queued.set(k, evt(topic, c.rev, 'snapshot', c.data, key ? { key } : {}));
      return true;
    }
    if (!changed.length && !removed.length) {
      // only the order moved: a patch cannot say that, a snapshot can
      c.rev += 1;
      queued.set(k, evt(topic, c.rev, 'snapshot', c.data, key ? { key } : {}));
      return true;
    }
    const base = pending ? pending.base_rev : c.rev;
    const merged = pending ? mergePatch(pending.data, changed, removed) : { changed, removed };
    c.rev += 1;
    queued.set(k, evt(topic, c.rev, 'patch', merged, { ...(key ? { key } : {}), base_rev: base }));
    return true;
  }

  function mergePatch(p, changed, removed) {
    const ch = new Map(p.changed.map(r => [r.id, r]));
    const rm = new Set(p.removed);
    for (const r of changed) { ch.set(r.id, r); rm.delete(r.id); }
    for (const id of removed) { ch.delete(id); rm.add(id); }
    return { changed: [...ch.values()], removed: [...rm] };
  }

  function event(topic, data) {
    const k = slot(topic, null);
    const c = cur.get(k) || { topic, key: null, rev: 0, seq: 0, data: null, cmp: null, list: false, event: true };
    c.rev += 1; c.seq += 1; c.event = true;
    cur.set(k, c);
    before.push(evt(topic, c.rev, 'edge', data, { seq: c.seq }));
  }

  function job(dto, { before: first = false } = {}) {
    jobsRev += 1; jobsSeq += 1;
    const f = evt('jobs', jobsRev, 'edge', dto, { seq: jobsSeq });
    jobsLog.push(f); if (jobsLog.length > 256) jobsLog.shift();
    (first ? before : after).push(f);
  }

  function log(rows) {
    for (const r of rows) {
      logRows.push(r);
      logRing.push(r); if (logRing.length > LOG_RING_MAX) logRing.shift();
    }
  }

  function logsFrame(rows) {
    logsRev += 1; logsSeq += 1;
    return evt('logs.batch', logsRev, 'edge', { from_seq: rows[0].seq, records: rows, dropped: 0, dropped_total: 0, gap: false }, { seq: logsSeq });
  }

  // The frames of one flush: transitions and "before" jobs, the changed state topics in ORDER,
  // the "after" jobs, then the new log rows. More than one goes out as a batch (§2).
  function take() {
    const states = [...queued.values()].sort((a, b) => rank(a.topic) - rank(b.topic) || String(a.key || '').localeCompare(String(b.key || '')));
    const items = [...before, ...states, ...after];
    if (logRows.length) for (let i = 0; i < logRows.length; i += 1000) items.push(logsFrame(logRows.slice(i, i + 1000)));
    queued = new Map(); before = []; after = []; logRows = [];
    if (!items.length) return [];
    if (items.length === 1) return items;
    tickSeq += 1;
    return [{ ...env('batch'), tick_seq: tickSeq, items }];
  }

  // What the shell replays on every rt_connect (shell/README P3): rt:reset{reconnect}, hello,
  // the cached snapshot of every state / list topic, the log tail, the job history. The design
  // "shell" also announces and sends the end-of-replay marker T-P4-C3 asks of the real shell
  // (host:caps.events rt:replayed; the client supports it since T-P4-02a): frames before it are
  // replay (instant), every frame after it is live, so the first live tier.transition after a
  // connect is a real edge and not mistaken for a replayed one.
  // events also lists rt:stale, as the real shell's host:caps does (PROTOCOL section 13,
  // T-P3-H2; 8th-wave ruling T-P4-H2 item 1): the design shell never sends one (nothing goes stale).
  function replay(hello) {
    const out = [{ t: 'rt:reset', epoch, reason: 'reconnect' }, { t: 'host:caps', invokes: [], events: ['rt:replayed', 'rt:stale'] }, hello];
    const entries = [...cur.values()].filter(c => !c.event && c.data)
      .sort((a, b) => rank(a.topic) - rank(b.topic) || String(a.key || '').localeCompare(String(b.key || '')));
    for (const c of entries) out.push(evt(c.topic, c.rev, 'snapshot', c.data, c.key ? { key: c.key } : {}));
    const tail = logRing.slice(-1000);
    if (tail.length && logsRev) {
      out.push(evt('logs.batch', logsRev, 'edge', { from_seq: tail[0].seq, records: tail, dropped: 0, dropped_total: 0, gap: false }, { seq: logsSeq }));
    }
    for (const f of jobsLog) out.push(clone(f));
    out.push({ t: 'rt:replayed', epoch });
    return out;
  }

  return {
    epoch, env, state, list, event, job, log, take, replay,
    current: (topic, key) => { const c = cur.get(slot(topic, key)); return c ? clone(c.data) : undefined; },
    logs: () => logRing,
  };
}

/* ==========================================================================
   DTO builders: the simulator's facts F -> v1 payloads.
   F: run ('stopped'|'starting'|'running'|'stopping'), step (startup step 0 collect,
      1 verify, 2 ready), stopRequested, gen, lifeRev, coreGen, coreRunning, pid,
      uptime, carrier, order, collect {tier: unknown|running|complete}, mode,
      switching, admin, ksEnabled, latCeiling, bp {enabled, ok, capacity, min,
      phase}, exit {current, previous}, traffic {down, up, rateDown, rateUp},
      pubRev, routeEpoch, logLevel, geo, update, refreshing (Set), alerts.
   D: T.data (the demo nodes, subscriptions, tiers).
   ========================================================================== */

export const usableNode = (n, ceiling) => (n.state === 'ok' || n.state === 'slow') && !(n.lat != null && n.lat > ceiling);
const realNodes = list => (list || []).filter(n => n && !n.draft);

export function startupPhase(F) {
  if (F.run === 'stopped') return 'stopped';
  if (F.run === 'stopping') return 'stopping';
  if (F.run === 'running') return 'running';
  return F.step <= 0 ? 'collecting' : 'core_standby';
}

export function serviceState(F, at) {
  const phase = startupPhase(F), running = F.run === 'running';
  return {
    intent: { desired_running: (F.run === 'starting' && !F.stopRequested) || running, stop_requested: !!F.stopRequested,
      lifecycle_revision: F.lifeRev, runtime_generation: F.gen },
    runtime: { runtime_running: F.run !== 'stopped', startup_phase: phase, shutdown_cleanup_pending: false, failure_code: null, failure: null },
    core: { running: !!F.coreRunning, pid: F.coreRunning ? F.pid : null, process_generation: F.coreGen, core_generation: F.coreGen, cleanup_pending: false },
    // the third startup step (就绪) has no v1 phase of its own: it stays core_standby with the
    // validation running, so the page keeps its step bar until startup_phase is running
    readiness: { state: running ? 'ready' : 'activation_hold', validation_active: F.run === 'starting' && F.step >= 1 && !F.stopRequested,
      reason: running ? '' : phase, remaining_ms: null, publication_revision: F.pubRev, lifecycle_revision: F.lifeRev,
      route_epoch: F.routeEpoch, network_context_epoch: 1 },
    freshness: { sampled_at_ms: at, snapshot_revision: F.snapRev, stale: false, uptime_s: running ? Math.max(0, Math.round(F.uptime)) : null },
  };
}

// The chosen Gateway exit: the current, non-draft Gateway node of an enabled Gateway tier.
export function chosenGateway(D) {
  const gw = (D.nodes.gateway || []).find(n => n && n.current && !n.draft) || null;
  return gw && D.tier('gateway') && D.tier('gateway').enabled ? gw : null;
}

// The carrier the runtime reports (T-P4-H2 item 3; desktop_runtime published route and
// tiers_summary_status, clash_formatter's chain): a chosen Gateway is the exit FINAL-EXIT ->
// EXIT-SELECT -> <Gateway node>, so managed_tier and the carrier are gateway, and the tier under
// it (the node's dialer-proxy PROXY-FALLBACK, the simulator's F.carrier) is not reported. Without
// a chosen Gateway the carrier is PROXY-FALLBACK's tier, F.carrier. null while not running.
export function reportedCarrier(F, D) {
  if (F.run !== 'running' || !F.carrier) return null;
  return chosenGateway(D) ? 'gateway' : F.carrier;
}

// Tier counts with the prototype's rule (sim.js countTiers): a node is usable when ok or slow
// and within the latency ceiling; Bastion counts its automatic pool; pool = the enabled tiers
// other than Bastion. degraded / fallback / order as the runtime builds them
// (desktop_runtime tiers_summary_status): degraded = the carrier has an unusable node,
// fallback = Sentinel or Bastion carries, the carrier moves to the front of the order. The
// order lists all four tiers, gateway included, as the runtime does (PROTOCOL 5.3; T-P4-05a).
// The carrier is reportedCarrier (gateway when a Gateway is chosen; T-P4-H2). `underGateway`
// gives PROXY-FALLBACK's own level instead (the simulator's scenarios act on it; never published).
export function tiersSummary(F, D, { underGateway = false } = {}) {
  const tiers = {};
  let pu = 0, pt = 0;
  const on = F.run === 'running';
  for (const t of D.tiers) {
    if (t.id === 'bastion') {
      const n = on && F.bp.enabled ? F.bp.ok : 0;
      tiers.bastion = { enabled: !!F.bp.enabled, total: n, usable: n };
      continue;
    }
    const list = realNodes(D.nodes[t.id]);
    const good = list.filter(n => usableNode(n, F.latCeiling)).length;
    tiers[t.id] = { enabled: !!t.enabled, total: list.length, usable: good };
    if (t.enabled) { pu += good; pt += list.length; }
  }
  const carrier = !on ? null : underGateway ? F.carrier : reportedCarrier(F, D);
  return {
    carrier, offline_reason: carrier ? null : F.run === 'stopped' ? 'stopped' : F.run === 'stopping' || F.stopRequested ? 'stopping' : 'unmanaged_route',
    order: carrier === 'gateway' ? ['gateway', ...TIER_IDS.filter(t => t !== 'gateway')] : F.order.slice(),
    degraded: !!(carrier && tiers[carrier] && tiers[carrier].usable < tiers[carrier].total),
    fallback: carrier === 'sentinel' || carrier === 'bastion', tiers, pool: { usable: pu, total: pt },
  };
}

export function serviceCollect(F, at) {
  return {
    generation: F.gen,
    tiers: TIER_IDS.map(tier => {
      const s = F.collect[tier] || 'unknown';
      const row = { tier, generation: F.gen, first_check: s, source_count: null, healthy: null, required: null, observed_at_ms: null };
      if (s === 'complete') Object.assign(row, F.collectFacts[tier] || { source_count: 1, healthy: 1, required: 1 }, { observed_at_ms: at });
      return row;
    }),
    observed_at_ms: at,
  };
}

const currentNode = list => realNodes(list).find(n => n.current) || realNodes(list)[0] || null;

// The published route in the runtime's shape (T-P4-H2 item 3): a chosen Gateway is the chain
// FINAL-EXIT -> EXIT-SELECT -> <Gateway node>, leaf the node, managed_tier gateway (the tier under
// it is not on the chain); otherwise the carrying tier's current node, managed_tier that tier.
export function routePublished(F, D, at) {
  const carrier = reportedCarrier(F, D);
  const gw = carrier === 'gateway' ? chosenGateway(D) : null;
  const via = carrier && !gw ? currentNode(D.nodes[carrier]) : null;
  if (!carrier || (!gw && !via)) {
    return { publication_revision: F.pubRev, owner: null, selected_chain: [], leaf: null, managed_tier: null, observed_at_ms: at,
      valid: false, invalid_reason: 'no_published_route', fail_closed_confirmed: false };
  }
  const chain = gw ? ['FINAL-EXIT', 'EXIT-SELECT', gw.name] : [via.name];
  return {
    publication_revision: F.pubRev,
    owner: { process_pid: F.pid, process_generation: F.coreGen, core_generation: F.coreGen, semantic_digest: `design:${chain.join('>')}`,
      route_epoch: F.routeEpoch, network_context_epoch: 1 },
    selected_chain: chain, leaf: chain[chain.length - 1], managed_tier: carrier, observed_at_ms: at,
    valid: true, invalid_reason: null, fail_closed_confirmed: false,
  };
}

export function coreInfo(F, version) {
  return { running: !!F.coreRunning, pid: F.coreRunning ? F.pid : null, version: F.coreRunning ? version : null, generation: F.coreGen, cleanup_pending: false };
}

export function networkMode(F) {
  const owns = F.run === 'running' && (F.mode === 'system' || F.mode === 'mixed');
  return { mode: F.mode, admin: !!F.admin, switching: !!F.switching, proxy_state: owns ? 'owned' : 'disabled', drift: false };
}

// PROTOCOL §11 (T-P4-C1 item 5): active / triggered = the block rules are installed and
// blocking. The design simulator never blocks, so both are false; "armed" (enabled, admin, no
// error, not triggered) is derived by the projection (rt/project.js killswitchOf).
export function killswitchState(F) {
  return { enabled: !!F.ksEnabled, active: false, admin: !!F.admin, error: null, triggered: false };
}

export function bastionPool(F, fillTo, at) {
  const on = F.run === 'running' && F.bp.enabled;
  return { runtime_alive: on ? F.bp.ok : 0, user_alive: 0, capacity: F.bp.capacity, min_alive_nodes: F.bp.min,
    target_alive: fillTo(), collecting: on && F.bp.phase === 'filling', observed_at_ms: at,
    // §8u: the totals beside the alive counts (no user nodes in the simulator's pool)
    runtime_total: on ? (Number.isInteger(F.bp.total) ? F.bp.total : F.bp.ok) : 0, user_total: 0 };
}

// selected: the saved choice; effective: only from the published route (the leaf while the
// Gateway carries), so never while stopped or while the Gateway tier is switched off
// (bridge_gateway_status; T-P4-H2 item 3).
export function gatewayCurrent(F, D) {
  const gw = (D.nodes.gateway || []).find(n => n.current && !n.draft) || null;
  const live = !!gw && reportedCarrier(F, D) === 'gateway';
  return { selected: gw ? gw.name : null, effective: live ? gw.name : null, missing: false, applied: live, pending: null };
}

export function protocolsMix(F, D, at) {
  const mix = D.protocolMix();
  const counts = {};
  for (const p of mix.list) counts[p.name] = p.count;
  return { source: F.run === 'running' ? 'loaded' : 'configured', counts, observed_at_ms: at };
}

export function nodeRow(n, tier, ceiling) {
  const healthy = usableNode(n, ceiling);
  const status = n.disabled ? 'disabled' : healthy ? 'healthy' : n.state === 'pending' ? 'pending' : 'failed';
  const lat = n.lat != null ? n.lat : null;
  return {
    id: n.id, name: n.name, tier, kind: tier === 'sentinel' ? 'subscription' : tier === 'bastion' ? 'automatic' : 'manual',
    enabled: !n.disabled, status, reason_code: status === 'failed' ? 'node_unavailable' : status === 'disabled' ? 'disabled' : null,
    protocol: (n.protocols && n.protocols[0]) || 'unknown',
    latency_ms: (status === 'healthy' || status === 'failed') && lat != null ? Math.max(0, Math.round(lat)) : null,
  };
}

export function nodesList(D, tier, ceiling) { return realNodes(D.nodes[tier]).map(n => nodeRow(n, tier, ceiling)); }

export function subscriptionsList(F, D, tier) {
  return (D.subscriptions || []).filter(s => s.tier === tier).map(s => {
    const kids = realNodes(D.nodes[tier]).filter(n => n.group === s.id);
    return { id: s.id, name: s.name, tier, enabled: true, display: s.name, total: kids.length,
      healthy: kids.filter(n => usableNode(n, F.latCeiling)).length, visibility: 'current', refreshing: F.refreshing.has(s.id) };
  });
}

export function configItems(D) {
  const out = [];
  for (const tier of ['vanguard', 'gateway']) {
    for (const n of realNodes(D.nodes[tier])) out.push({ id: `cfg-${n.id}`, name: n.name, kind: 'node', section: tier, enabled: !n.disabled, secret_set: true });
  }
  for (const s of D.subscriptions || []) out.push({ id: `cfg-${s.id}`, name: s.name, kind: 'subscription', section: s.tier, enabled: true, secret_set: true });
  return out;
}

export function logsFeed(F, at) {
  const state = F.run === 'running' ? 'connected' : F.run === 'starting' ? 'stdout' : 'stopped';
  return { state, level: F.logLevel, updated_at_ms: at,
    dropped: { debug: 0, info: 0, warn: 0, error: 0, critical: 0, total: 0 }, run_id: F.run === 'stopped' ? null : `run-${F.gen}` };
}

export function emptyLeg() { return { status: 'unknown', address: null, country: null, reason_code: null, observed_at_ms: null }; }

// trigger (T-P4-C2 item 2): who asked, manual (exit.verify) or auto; never on an idle result.
// geo {v4, v6} (T-P4-C2 item 1): the source report of an ok leg (city, timezone, asn, org, type).
export function identity({ request_id, generation, status, at, v4, v6, binding, trigger, geo }) {
  const ok = status === 'ok' || status === 'partial';
  const leg = (s, a, g) => (s === 'ok'
    ? { status: 'ok', address: a, country: 'JP', reason_code: null, observed_at_ms: at, ...(g || {}) }
    : { status: s, address: null, country: null, reason_code: null, observed_at_ms: null });
  const out = {
    request_id, generation, binding_id: ok ? binding : null, status, reason_code: null,
    v4: status === 'idle' ? emptyLeg() : leg(status === 'running' ? 'running' : 'ok', v4, geo && geo.v4),
    v6: status === 'idle' ? emptyLeg() : leg(status === 'running' ? 'running' : 'ok', v6, geo && geo.v6),
    fresh: ok, observed_at_ms: ok ? at : null,
    native_fallback_verified: false, native_fallback_verified_commit_rejected: false,
    native_fallback_unverified_budget: false, manual_execution_binding_stale: false,
  };
  if (trigger && status !== 'idle') out.trigger = trigger;
  return out;
}

/* ---- logs: prototype records -> v1 Log rows (PROTOCOL §5.5, §16 Log) ---- */
const CAT_V1 = { 运行状态: 'runtime', 更新: 'update', 验证: 'routing', 路由: 'routing', 配置: 'configuration', 熔断: 'circuit_breaker',
  层级健康: 'health', 常规: 'other', 连接: 'connection', 运行告警: 'alert' };
const SRC_V1 = { Tesseract: 'TESSERACT', Mihomo: 'MIHOMO' };
const LEVEL_V1 = { ok: 'info', debug: 'debug', info: 'info', warn: 'warn', error: 'error', critical: 'critical' };

// A page-seed record ({ts, level, src, cat, key, title, detail, raw}): a record without a key is
// a raw line (key "log.raw", title = the raw text, as bridge_logs does for an unclassified
// message). Since T-P4-C1 item 2 every row also says whether it is a key event and, from the
// first run start on, which run it belongs to (PROTOCOL §5.5; T-P4-05c, 决定_第五波裁定 02b-6).
export function pageRecordToLog(r, seq, runId) {
  const raw = String(r.raw || '');
  const row = {
    seq, ts_ms: Math.round(r.ts), rx_ms: Math.round(r.ts) + 9 + (seq * 7) % 37, key: r.key || 'log.raw',
    source: SRC_V1[r.src] || 'UNKNOWN', level: LEVEL_V1[r.level] || 'info', category: CAT_V1[r.cat] || 'other',
    title: r.key ? String(r.title || '') : raw, detail: r.key ? String(r.detail || '') : '', raw,
    is_key_event: !!r.key,
  };
  if (typeof runId === 'string' && runId) row.run_id = runId;
  return row;
}

/* The simulator's own records ({ts, level, source, category, title, detail, raw} with the sim
   categories) take their event key, page category and English raw line from the logs page's
   mapping of the same titles (pages/logs.js SIM), so the design stream reads like the real one. */
const SIM_CAT = { runtime: '运行状态', source: '更新', exit: '验证', proxy: '配置', killswitch: '熔断', health: '层级健康', traffic: '常规', dns: '连接', tier: '路由' };
const latOf = s => { const m = /(\d+) ms/.exec(s || ''); return m ? m[1] : null; };
const nodeOf = s => { const p = String(s || '').split(' · '); return p.length > 1 ? p[1] : p[0]; };
export const SIM_LOG = {
  正在启动服务: { cat: '运行状态', key: 'runtime.start_requested', raw: (s, F) => `Desktop runtime start requested (generation=${F.gen})` },
  节点来源刷新完成: { cat: '更新', key: 'update.source_completed', raw: s => { const m = /(\w+) · (\d+)\/(\d+)/.exec(s.detail) || []; return `Node source refresh finished: tier=${(m[1] || 'unknown').toLowerCase()} ok=${m[2] || 0} total=${m[3] || 0}`; } },
  正在验证最终出口: { event: false, cat: '验证', raw: s => `Verifying final exit via "${s.detail}"` },
  代理出口已可用: { level: 'ok', cat: '路由', key: 'tier.egress_ready', raw: () => 'Egress ready: exit="东京 Gateway 01" members=3/3 probe=203.0.113.24:443' },
  代理核心已启动: { cat: '运行状态', key: 'runtime.mihomo_ready', src: 'Mihomo', raw: () => 'RESTful API listening at: 127.0.0.1:25500' },
  系统代理已接管: { cat: '配置', key: 'proxy.system_set', raw: s => `System proxy set: ${s.detail} bypass=localhost;127.*;10.*;192.168.*` },
  全局模式已接管: { cat: '配置', key: 'proxy.tun_set', raw: () => 'TUN mode set: device=Tesseract stack=mixed auto_route=true' },
  混合模式已接管: { cat: '配置', key: 'proxy.mixed_set', raw: () => 'Mixed mode set: TUN device=Tesseract + system proxy 127.0.0.1:7890' },
  熔断保护已布防: { cat: '熔断', key: 'killswitch.armed', raw: () => 'Killswitch armed (policy=block-on-total-failure)' },
  熔断保护没有生效: { cat: '熔断', key: 'killswitch.blocked', raw: () => 'Killswitch not effective: administrator rights required for firewall rules' },
  出口已确认: { level: 'ok', cat: '验证', key: 'egress.verified', raw: () => 'Exit identity: 203.0.113.24 AS64500 JP/Tokyo (auto, via runtime readiness probe)' },
  出口已验证: { level: 'ok', cat: '验证', key: 'egress.verified', raw: () => 'Exit verified: 203.0.113.24 AS64500 JP/Tokyo (manual full check)' },
  服务已停止: { cat: '运行状态', key: 'runtime.stopped', raw: () => 'Desktop runtime stopped (reason=user)' },
  已取消启动: { cat: '运行状态', key: 'runtime.start_cancelled', raw: () => 'Desktop runtime start cancelled by user' },
  正在重启代理核心: { cat: '运行状态', key: 'runtime.core_restarting', raw: () => 'Restarting core (reason=user)' },
  代理核心已重启: { cat: '运行状态', key: 'runtime.core_restarted', src: 'Mihomo', raw: () => 'Initial configuration complete, total time: 1.12s' },
  'Vanguard 延迟升高': { cat: '层级健康', key: 'tier.vanguard_slow', raw: () => 'Latency over ceiling: proxy="香港 IEPL 03" delay=820ms ceiling=800ms' },
  '流量由 Sentinel 承载': { cat: '路由', key: 'tier.sentinel_active', raw: () => 'Tier mode changed: tier1_degraded -> tier2_active (tier1=0/2 tier2=4)' },
  'Vanguard 已恢复': { level: 'ok', cat: '路由', key: 'tier.vanguard_restored', raw: () => 'Tier mode changed: tier2_active -> tier1_healthy (tier1=2/2 tier2=4)' },
  'Vanguard 延迟已恢复': { level: 'ok', cat: '层级健康', key: 'tier.vanguard_healthy', raw: () => 'Latency back under ceiling: proxy="香港 IEPL 03" delay=44ms ceiling=800ms' },
  正在切换网络模式: { cat: '配置', key: 'proxy.mode_switching', raw: s => `Switching mode: ${s.detail}` },
  网络模式已切换: { cat: '配置', key: 'proxy.mode_switched', raw: s => `Mode switched: ${s.detail}` },
  健康检查通过: { event: false, cat: '层级健康', level: s => (latOf(s.detail) ? 'info' : 'warn'),
    raw: s => { const g = String(s.detail).split(' · ')[0].toUpperCase(), n = nodeOf(s.detail), l = latOf(s.detail);
      return l ? `health check ok: group=${g} proxy="${n}" delay=${l}ms` : `health check failed: group=${g} proxy="${n}" error: i/o timeout`; } },
  流量采样正常: { event: false, cat: '常规', raw: (s, F) => `[Stats] down=${Math.round(F.traffic.rateDown)}B/s up=${Math.round(F.traffic.rateUp)}B/s` },
  复检通过: { event: false, cat: '层级健康', raw: () => 'recheck ok: group=SENTINEL proxy="首尔 01" delay=49ms' },
  'DNS 解析正常': { event: false, cat: '连接', raw: () => '[DNS] resolver ok: dns.example.net avg=24ms' },
  节点暂不可用: { cat: '层级健康', key: 'tier.node_suspended', raw: () => 'health check failed: proxy="法兰克福 04" dial tcp 198.51.100.44:8388: i/o timeout; suspended' },
  'Bastion 应急池已启用': { cat: '路由', key: 'tier3.pool_enabled', raw: () => 'Tier3 pool enabled (public nodes; emergency only)' },
  'Bastion 应急池已关闭': { cat: '路由', key: 'tier3.pool_disabled', raw: () => 'Tier3 pool disabled' },
};

export function simRecordToLog(s, seq, F, runId) {
  const m = SIM_LOG[s.title] || {};
  const ev = m.event !== false;
  const lv = typeof m.level === 'function' ? m.level(s) : m.level || s.level;
  const cat = m.cat || SIM_CAT[s.category] || '常规';
  const raw = m.raw ? m.raw(s, F) : String(s.raw || '');
  return pageRecordToLog({ ts: s.ts, level: lv, src: m.src || s.source, cat, key: ev ? m.key || `design.${s.category}` : null,
    title: s.title, detail: s.detail, raw }, seq, runId);
}
