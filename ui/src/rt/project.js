// Projection: v1 DTOs -> the DESIGN section 5.5 store keys + rt:* events (T-P4-02a; P4
// architecture section 2.6, mapping_core sections 1.2 and 1.3). This is the only place that
// writes runtime-derived store keys; pages only read them. Counts are always the runtime's
// own (no recount). ui.language is never projected: the shell strips it before the page
// cache (criticism B2); store `lang` is written by the client from hello / host:language.
//
// Every rt:* event carries `instant` (true for the first frame of a topic after a connect or
// an rt:reset: no edge notice, no light-up animation). The pure edge notices rt:tier and
// rt:mode are not emitted for an instant frame at all: their state is already in the store.
// One exception (T-P4-H, tier level reconciliation): when this page has announced a fallback
// (rt:tier fallback) and the first tiers.summary after a reconnect shows the level has moved
// on meanwhile, the missed change is announced once with instant:true, so a banner the page
// still shows can follow the level. This holds for a same-epoch reconnect and for a
// generation change alike (rt:reset runtime_gone or a new epoch): reset() keeps the announced
// level on purpose, because the banner of the lost generation is still up and nothing else
// would take it down. A consumer takes an instant recover as "bring the banner up to date"
// (take it down, no recovered toast: P4 architecture section 2.3, first frames bring no edge
// notice). Fix round 1 of T-P4-H, review S5.
//
// Known limitation (T-P4-H fix round 1, review S4): tiers.summary carries no sample time, so a
// tier.transition delivered after a summary that already showed its outcome cannot be told
// apart from a newer switch. E.g. summary shows Sentinel first (fallback sentinel announced),
// then the late edges Vanguard -> Bastion and Bastion -> Sentinel arrive: the notices go
// sentinel, bastion (from vanguard), sentinel (from bastion). A consumer that replaces its
// banner in place ends on the right tier; only the wording flips in between. Holding such
// transitions back would delay every real switch that follows a summary-announced one until
// the next summary frame, so the edges are passed on as they come.

export const TIER_IDS = Object.freeze(['vanguard', 'sentinel', 'bastion', 'gateway']);
const RUN_OF = { collecting: 'starting', core_standby: 'starting', running: 'running', stopping: 'stopping', stopped: 'stopped', failed: 'failed' };
const ACTIVE_JOB = new Set(['queued', 'running']);
const BACKUP_TIERS = new Set(['sentinel', 'bastion']);
const LIVE_MAX = 300;

// mapping_core 1.2 `run`; 'unknown' before the first snapshot of an epoch.
export function runOf(state) {
  const p = state && state.runtime && state.runtime.startup_phase;
  return RUN_OF[p] || 'unknown';
}

// mapping_core 1.2 `phase`: collecting -> 0; core_standby with readiness.validation_active
// -> 1; running -> 2; otherwise -1.
export function phaseOf(state) {
  const p = state && state.runtime && state.runtime.startup_phase;
  if (p === 'collecting') return 0;
  if (p === 'core_standby' && state.readiness && state.readiness.validation_active === true) return 1;
  if (p === 'running') return 2;
  return -1;
}

// mapping_core 1.2 `cancelling`: run = starting and intent.stop_requested.
export function cancellingOf(state) {
  return runOf(state) === 'starting' && !!(state.intent && state.intent.stop_requested);
}

// tiers[id].state from {enabled, usable, total}: off / down / partial / ok. A tier with no
// usable node (including an empty one) is down.
export function tierState(t) {
  if (!t || !t.enabled) return 'off';
  const usable = Number(t.usable) || 0, total = Number(t.total) || 0;
  if (usable <= 0) return 'down';
  if (usable < total) return 'partial';
  return 'ok';
}

// mapping_core 1.2 killswitch row (PROTOCOL section 11 killswitch.state, T-P4-C1 item 5;
// 5th-wave ruling C1-#3). `active` / `triggered` = the block rules are installed and blocking
// (KillSwitchManager.active); a frozen sidecar has no `triggered` field, so `triggered ??
// active` is the fact. PROTOCOL section 11 (killswitch.state) defines armed on the DTO alone
// (enabled && admin && error == null && !triggered); the page adds mapping_core 1.2's run
// condition on top: a service that is not running arms nothing, so it reads inactive (the
// PROTOCOL wording is to say "armed only while running"; T-P4-H fix round 1, review S3).
// Six states:
//   !enabled -> disabled; error -> error; triggered -> triggered (rules in force: shown
//   whatever the run state, traffic is blocked); run != running -> inactive; !admin ->
//   blocked; otherwise (enabled, admin, no error, not triggered) -> armed.
export function killswitchOf(ks, run) {
  if (!ks) return null;
  const enabled = !!ks.enabled;
  const ksBlocked = enabled && !ks.admin ? 'admin' : null;
  const triggered = (ks.triggered ?? ks.active) === true;
  let killswitch;
  if (!enabled) killswitch = 'disabled';
  else if (ks.error) killswitch = 'error';
  else if (triggered) killswitch = 'triggered';
  else if (run !== 'running') killswitch = 'inactive';
  else if (ksBlocked) killswitch = 'blocked';
  else killswitch = 'armed';
  return { killswitch, ksEnabled: enabled, ksBlocked };
}

// mapping_core 3.4: v1 Update.state -> the prototype footer states.
export function updateStateOf(u) {
  if (!u) return 'latest';
  switch (u.state) {
    case 'checking': return 'checking';
    case 'downloading': return 'dl';
    case 'verified': return 'verify';
    case 'handoff': return 'ready';
    case 'failed': return 'fail';
    case 'available': return 'found';
    case 'idle': return u.update_available ? 'found' : (u.error ? 'fail' : 'latest');
    default: return 'latest';
  }
}

// mapping_core 1.2 exit row: IdentityResult status -> prototype exit state.
const EXIT_STATE = { running: 'pending', ok: 'ok', partial: 'ok', idle: 'none', failed: 'failed', cancelled: 'cancelled', superseded: 'superseded' };
export function exitOf(identity, manualIds = new Set()) {
  const cur = identity && identity.current;
  if (!cur) return { state: 'none', had: false };
  const prev = identity.previous || null;
  const good = s => s === 'ok' || s === 'partial';
  const state = EXIT_STATE[cur.status] || 'none';
  return {
    state,
    status: cur.status,
    partial: cur.status === 'partial',
    how: manualIds.has(cur.request_id) ? 'manual' : 'auto',
    at: cur.observed_at_ms == null ? null : cur.observed_at_ms,
    had: good(cur.status) || !!(prev && good(prev.status)),
    reason_code: cur.reason_code == null ? null : cur.reason_code,
    current: cur,
    previous: prev,
  };
}

// `hasTopic` (caps.topics) is accepted but no longer read: since T-P4-H tiers.summary and
// tier.transition are reconciled whether or not the sidecar publishes the latter.
export function createProjector({ store, emit, inflightArgs = () => null, hasTopic = () => true }) {
  const S = store;
  const st = {
    run: 'unknown', phase: -1, ks: null, summary: null, fallback: false, degraded: false,
    modeData: null, modeJobs: new Set(), restartJobs: new Set(), collected: {}, appVersion: null,
    update: null, manualExit: new Set(), core: {},
    // Tier level reconciliation (T-P4-H; 5th-wave ruling 02b-1). tierNotice: the fallback level
    // this page last announced with rt:tier; sumLevel: the level tiers.summary last showed
    // with a carrier (frames without one, a route-invalid window, leave it as it was).
    tierNotice: { fallback: false, carrier: null }, sumLevel: { fallback: false, carrier: null }, sumGap: false,
  };
  // The prototype chart (overview) keeps a reference to this object and needs at least two
  // buckets / three points to draw. Until the first traffic.history snapshot it holds a flat
  // zero series flagged `placeholder: true` (transitional for the app target, ticket
  // requirement 8; T-P4-05a draws the empty state itself and drops this).
  const history = { h24: [], live: [], placeholder: true };
  function placeholderHistory() {
    const now = Date.now();
    history.h24.splice(0, history.h24.length, { t: now - 300000, d: 0, u: 0, n: 0 }, { t: now, d: 0, u: 0, n: 0 });
    history.live.splice(0, history.live.length, { t: now - 2000, d: 0, u: 0 }, { t: now - 1000, d: 0, u: 0 }, { t: now, d: 0, u: 0 });
    history.placeholder = true;
  }
  const ev = (name, payload) => emit(name, payload);

  // rt:tier fallback / recover from either source (tier.transition or tiers.summary): whichever
  // reports a level change first announces it, the other one finds tierNotice already there
  // and stays silent. Both are observed changes (PROTOCOL section 5.3, tier.transition
  // authority=observed), never claimed to catch every switch; `source` says which path saw it.
  //
  // Gateway (8th-wave ruling T-P4-H2 item 3, a local revision of 5th-wave 02b-1; T-P4-05a M1):
  // a chosen Gateway is the exit FINAL-EXIT -> EXIT-SELECT -> <Gateway node>, and the tier under
  // it (its dialer-proxy PROXY-FALLBACK) is not reported. Entering Gateway is the user's choice,
  // not a recovery: the recover is `quiet` (a consumer takes its banner down and says nothing,
  // no "recovered"). Leaving Gateway for a backup tier is still a fallback, but its origin is not
  // Gateway (the Gateway was cancelled, it did not fail, and nothing switches back to it on its
  // own): `from` is left out (PROXY-FALLBACK's order says Vanguard is the one not available).
  function noticeFallback(carrier, from, source, instant = false) {
    const n = st.tierNotice;
    if (n.fallback && n.carrier === carrier) return;
    st.tierNotice = { fallback: true, carrier };
    const e = { kind: 'fallback', carrier };
    if (typeof from === 'string' && from !== carrier && from !== 'gateway') e.from = from;
    ev('rt:tier', { ...e, observed: true, source, instant });
  }
  function noticeRecover(carrier, from, source, instant = false) {
    if (!st.tierNotice.fallback) return;
    st.tierNotice = { fallback: false, carrier };
    const e = { kind: 'recover', carrier };
    if (typeof from === 'string') e.from = from;
    if (carrier === 'gateway') e.quiet = true;
    ev('rt:tier', { ...e, wasFallback: true, observed: true, source, instant });
  }
  // The level side of tiers.summary. A frame with a carrier sets sumLevel; its own edges are
  // reconciled against tierNotice:
  //   rising (into Sentinel / Bastion)  -> fallback, unless already announced (any carrier;
  //                                        from Gateway: no from)
  //   backup -> other backup            -> fallback with from, unless already at that carrier
  //   falling (to Vanguard / Gateway)   -> recover(wasFallback), unless already announced (to
  //                                        Gateway: quiet)
  // Comparing summary with its own previous level (not with tierNotice) keeps a summary sampled
  // before a transition from undoing it. A frame without a carrier (route invalid, fail-closed)
  // says nothing about the level, but it marks a gap (so does a gap frame of tier.transition,
  // see transitionGap below): the sidecar drops its transition baseline
  // while the route is invalid (bridge_tiers.transition), so changes across the gap have no
  // tier.transition edge, and the first frame with a carrier after it is reconciled level
  // against tierNotice. A stopped / stopping service resets both silently (a stop is not a
  // recovery; the shell takes the banner down on stopping). Instant frames set the level
  // without a notice, except to bring a fallback this page announced up to date.
  function summaryLevel(d, instant) {
    const c = d.carrier;
    if (!TIER_IDS.includes(c)) {
      if (d.offline_reason === 'stopped' || d.offline_reason === 'stopping') {
        st.sumLevel = { fallback: false, carrier: null };
        st.tierNotice = { fallback: false, carrier: null };
        st.sumGap = false;
      } else st.sumGap = true;
      return;
    }
    const fb = !!d.fallback, prev = st.sumLevel, n = st.tierNotice, gap = st.sumGap;
    st.sumLevel = { fallback: fb, carrier: c };
    st.sumGap = false;
    if (instant || gap) {
      if (n.fallback && !fb) noticeRecover(c, n.carrier, 'summary', instant);
      else if (fb && n.fallback) noticeFallback(c, n.carrier, 'summary', instant);
      else if (fb && !instant) noticeFallback(c, prev.carrier, 'summary');
      else st.tierNotice = { fallback: fb, carrier: c };
      return;
    }
    if (fb && !prev.fallback) { if (!n.fallback) noticeFallback(c, prev.carrier, 'summary'); }
    else if (fb && prev.carrier !== c) { if (n.fallback && n.carrier !== c) noticeFallback(c, n.carrier, 'summary'); }
    else if (!fb && prev.fallback) noticeRecover(c, n.carrier, 'summary');
  }

  // Before the first snapshot (S1 "connecting"): run is 'unknown' and the keys the prototype
  // pages read without a guard get neutral empty shapes (zero counts, nothing collected, no
  // exit), so the transitional app target throws nothing (ticket requirement 8). They are
  // placeholders, not facts: each 05x ticket renders the unknown state itself. `mode` is the
  // one key with no neutral value; the placeholder 'system' is only set while no network.mode
  // frame has arrived and is flagged by `modeKnown: false`.
  function seed() {
    placeholderHistory();
    const tiers = {};
    for (const id of TIER_IDS) tiers[id] = { ok: 0, total: 0, enabled: false, state: 'off', unknown: true };
    S.patch({ run: 'unknown', phase: -1, cancelling: false, restarting: false, modeBusy: false, collected: {},
      failure: null, readiness: null, history, uptime: 0, tiers, pool: { ok: 0, total: 0 },
      traffic: { down: 0, up: 0, rateDown: 0, rateUp: 0 }, exit: { state: 'none', had: false } });
    if (S.get('mode') === undefined) S.patch({ mode: 'system', modeKnown: false });
    if (S.get('stale') === undefined) S.set('stale', {});
  }

  function setKillswitch() {
    const k = killswitchOf(st.ks, st.run);
    if (k) S.patch(k);
  }

  function setModeBusy(instant) {
    const d = st.modeData;
    const busy = !!(d && d.switching) || st.modeJobs.size > 0;
    const was = !!S.get('modeBusy');
    S.set('modeBusy', busy);
    if (busy === was || instant) return;
    const cur = (d && d.mode) || S.get('mode');
    let target = null;
    if (busy) { const a = inflightArgs('mode.set'); target = a && a.mode; }
    const mode = target || cur;
    if (mode) ev('rt:mode', { mode, busy, instant: false });
  }

  const apply = {
    'service.state'(d, o) {
      const run = runOf(d), phase = phaseOf(d), prev = st.run, prevPhase = st.phase;
      const wasCancelling = !!S.get('cancelling');
      st.run = run; st.phase = phase;
      const core = d.core || {};
      st.core = { ...st.core, running: !!core.running, cleanup_pending: !!core.cleanup_pending,
        generation: core.core_generation == null ? st.core.generation : core.core_generation, pid: core.pid == null ? null : core.pid };
      S.patch({
        phase, cancelling: cancellingOf(d),
        uptime: d.freshness && Number.isFinite(d.freshness.uptime_s) ? d.freshness.uptime_s : 0,
        failure: (d.runtime && d.runtime.failure) || null,
        readiness: d.readiness || null,
        core: { ...st.core },
      });
      S.set('run', run);
      setKillswitch();
      if (run !== prev) {
        const cancelled = prev === 'starting' && (wasCancelling || !!(d.intent && d.intent.stop_requested));
        ev('rt:state', { run, prev, cancelled, instant: !!o.instant });
      }
      if (phase !== prevPhase) ev('rt:phase', { phase, instant: !!o.instant });
    },
    'route.published'(d, o) { S.set('route', d); ev('rt:route', { route: d, instant: !!o.instant }); },
    'tiers.summary'(d, o) {
      const prevFallback = st.fallback, prevDegraded = st.degraded;
      const prevCarrier = st.summary ? st.summary.carrier : undefined;
      st.summary = d;
      const tiers = {};
      for (const id of TIER_IDS) {
        const t = (d.tiers && d.tiers[id]) || { enabled: false, total: 0, usable: 0 };
        tiers[id] = { ok: t.usable, total: t.total, enabled: !!t.enabled, state: tierState(t) };
      }
      st.fallback = !!d.fallback; st.degraded = !!d.degraded;
      S.patch({ carrier: d.carrier == null ? null : d.carrier, order: Array.isArray(d.order) ? d.order.slice() : [],
        fallback: st.fallback, degraded: st.degraded, tiers,
        pool: { ok: d.pool ? d.pool.usable : 0, total: d.pool ? d.pool.total : 0 } });
      const b = S.get('bastion');
      if (b) S.set('bastion', { ...b, enabled: tiers.bastion.enabled });
      ev('rt:tiers', { instant: !!o.instant });
      // Observed changes only (PROTOCOL section 5.3): degraded edges always come from here;
      // fallback / recover edges are reconciled with tier.transition (summaryLevel).
      // degraded / latency recover are a level change of ONE carrier (T-P4-H2 fix round 1, M1):
      // degraded is per carrier (any unusable node of the carrier tier), so a switch between
      // carriers (e.g. a degraded Vanguard -> a healthy chosen Gateway, or back) is not "the
      // latency recovered" nor "degraded"; a carrier-less frame (route invalid) and the first
      // frame after it say nothing either. Known edge: a degradation that happens across such a
      // gap gets no toast (the tier rows still show it).
      const sameCarrier = prevCarrier != null && prevCarrier === d.carrier;
      if (!o.instant && sameCarrier) {
        if (st.degraded && !prevDegraded && !st.fallback) ev('rt:tier', { kind: 'degraded', carrier: st.summary.carrier, observed: true, source: 'summary', instant: false });
        else if (!st.degraded && prevDegraded && !st.fallback && !prevFallback) ev('rt:tier', { kind: 'recover', carrier: st.summary.carrier, wasFallback: false, observed: true, source: 'summary', instant: false });
      }
      summaryLevel(d, !!o.instant);
    },
    // mapping_core 1.3 (the prototype's tier event): tier.transition has no kind. The kind comes from the DTO's own
    // from / to (5th-wave ruling 02b-1): to Sentinel or Bastion -> fallback (between Bastion and
    // Sentinel still fallback, with from; from Gateway without from); from Sentinel or Bastion
    // back to Vanguard or Gateway -> recover(wasFallback: true; into Gateway quiet, 8th-wave
    // ruling T-P4-H2 item 3); between Vanguard and Gateway -> nothing. Not from
    // tiers.summary.order: the runtime moves the carrier to the front of that order
    // (desktop_runtime tiers_summary_status), so order[0] is the new carrier, not the primary.
    // The instant first frame is the shell's cached last edge: no notice, no level.
    'tier.transition'(d, o) {
      ev('rt:transition', { from: d.from, to: d.to, observed_at_ms: d.observed_at_ms, instant: !!o.instant });
      if (o.instant || d.from === d.to) return;
      if (BACKUP_TIERS.has(d.to)) noticeFallback(d.to, d.from, 'transition');
      else if (BACKUP_TIERS.has(d.from)) noticeRecover(d.to, d.from, 'transition');
    },
    'traffic.tick'(d, o) {
      const s = d.session || {};
      const traffic = { down: s.down_bytes || 0, up: s.up_bytes || 0, rateDown: d.down_bps || 0, rateUp: d.up_bps || 0 };
      S.set('traffic', traffic);
      const sample = { t: d.t_ms, d: d.down_bps || 0, u: d.up_bps || 0 };
      const last = history.live[history.live.length - 1];
      if (d.running && (!last || last.t !== sample.t)) {
        history.live.push(sample);
        if (history.live.length > LIVE_MAX) history.live.splice(0, history.live.length - LIVE_MAX);
      }
      ev('rt:tick', { t: d.t_ms, sample, traffic, running: !!d.running, instant: !!o.instant });
    },
    'traffic.history'(d, o) {
      history.h24.splice(0, history.h24.length, ...(d.h24 || []).map(b => ({ t: b.t_ms, d: b.down_bps, u: b.up_bps, n: b.sample_count })));
      history.live.splice(0, history.live.length, ...(d.live || []).map(p => ({ t: p.t_ms, d: p.down_bps, u: p.up_bps })));
      history.source = d.source; history.sampled_at = d.sampled_at_ms; history.gap = d.gap === true; history.placeholder = false;
      S.set('history', history);
      ev('rt:history', { history, gap: d.gap === true, instant: !!o.instant });
    },
    'core.info'(d, o) {
      st.core = { ...st.core, running: !!d.running, cleanup_pending: !!d.cleanup_pending, generation: d.generation,
        version: d.version == null ? null : d.version, pid: d.pid == null ? null : d.pid };
      S.set('core', { ...st.core });
      ev('rt:core', { core: { ...st.core }, instant: !!o.instant });
    },
    'service.collect'(d, o) {
      const next = {};
      for (const row of d.tiers || []) if (row && row.first_check === 'complete') next[row.tier] = true;
      const prev = st.collected;
      st.collected = next;
      S.set('collected', { ...next });
      for (const id of Object.keys(next)) if (!prev[id]) ev('rt:collect', { tier: id, instant: !!o.instant });
    },
    'network.mode'(d, o) {
      const prevMode = S.get('mode');
      st.modeData = d;
      S.patch({ mode: d.mode, modeKnown: true, admin: !!d.admin, systemProxy: { proxy_state: d.proxy_state, drift: !!d.drift } });
      const wasBusy = !!S.get('modeBusy');
      setModeBusy(!!o.instant);
      if (!o.instant && prevMode !== undefined && prevMode !== d.mode && wasBusy === !!S.get('modeBusy') && !S.get('modeBusy')) {
        ev('rt:mode', { mode: d.mode, busy: false, instant: false });
      }
    },
    'killswitch.state'(d, o) { st.ks = d; setKillswitch(); ev('rt:killswitch', { instant: !!o.instant }); },
    'bastion.pool'(d, o) {
      const enabled = st.summary && st.summary.tiers && st.summary.tiers.bastion ? !!st.summary.tiers.bastion.enabled : undefined;
      S.set('bastion', { enabled, ok: d.runtime_alive, user: d.user_alive, capacity: d.capacity, target: d.target_alive,
        min: d.min_alive_nodes, collecting: !!d.collecting, observed_at_ms: d.observed_at_ms });
      ev('rt:bastion', { instant: !!o.instant });
    },
    'exit.identity'(d, o) {
      const exit = exitOf(d, st.manualExit);
      S.set('exit', exit);
      ev('rt:exit', { exit, instant: !!o.instant });
    },
    'update.state'(d, o) {
      st.update = d;
      S.set('update', { state: updateStateOf(d), version: st.appVersion, latest: d.version == null ? null : d.version,
        available: !!d.update_available, installable: !!d.installable, error: d.error || null, raw: d.state });
      ev('rt:update', { instant: !!o.instant });
    },
    'protocols.mix'(d, o) { S.set('protocols', { source: d.source, counts: d.counts, observed_at_ms: d.observed_at_ms }); ev('rt:protocols', { instant: !!o.instant }); },
    'gateway.current'(d, o) { S.set('gateway', d); ev('rt:gateway', { instant: !!o.instant }); },
    'config.changed'(d, o) {
      S.set('configRev', d.rev);
      ev('rt:config', { rev: d.rev, sections: d.sections, pending: d.pending == null ? null : d.pending, instant: !!o.instant });
    },
  };

  return {
    seed,
    history,
    // hello: caps / lang / recovery / appVersion are written by the client; the projector
    // keeps the app version for the update footer.
    hello(h) {
      st.appVersion = h.app_version;
      const u = S.get('update');
      if (u) S.set('update', { ...u, version: h.app_version });
    },
    // A generation change (rt:reset runtime_gone or a new epoch): runtime keys go 'unknown',
    // announced as an instant rt:state so the shell's brand dot greys out (architecture 2.3).
    reset() {
      const prev = st.run, prevPhase = st.phase;
      st.run = 'unknown'; st.phase = -1; st.modeJobs.clear(); st.restartJobs.clear(); st.collected = {};
      st.manualExit.clear();
      S.patch({ phase: -1, cancelling: false, restarting: false, modeBusy: false, collected: {}, failure: null, readiness: null });
      S.set('run', 'unknown');
      setKillswitch();
      if (prev !== 'unknown') ev('rt:state', { run: 'unknown', prev, cancelled: false, instant: true });
      if (prevPhase !== -1) ev('rt:phase', { phase: -1, instant: true });
    },
    apply(topic, data, o = {}) {
      const fn = apply[topic];
      if (fn) fn(data, o);
      return !!fn;
    },
    // jobs edge: restarting / modeBusy and rt:restart / rt:job.
    job(d, o = {}) {
      if (!d || typeof d.op !== 'string') return;
      const active = ACTIVE_JOB.has(d.state);
      if (d.name === 'service.restart') {
        const was = st.restartJobs.size > 0;
        if (active) st.restartJobs.add(d.op); else st.restartJobs.delete(d.op);
        const now = st.restartJobs.size > 0;
        S.set('restarting', now);
        if (now !== was && !o.instant) ev('rt:restart', { state: now ? 'begin' : 'done', instant: false });
      } else if (d.name === 'mode.set') {
        if (active) st.modeJobs.add(d.op); else st.modeJobs.delete(d.op);
        setModeBusy(!!o.instant);
      } else if (d.name === 'exit.verify' && d.result && typeof d.result.request_id === 'string') {
        st.manualExit.add(d.result.request_id);
      }
      ev('rt:job', { job: d, instant: !!o.instant });
    },
    noteManualExit(requestId) { if (typeof requestId === 'string') st.manualExit.add(requestId); },
    // A gap in tier.transition (an event topic: the client sees rt:gap for it) may have lost a
    // level edge, e.g. the recovery from a fallback shorter than the summary's sampling period.
    // The next tiers.summary frame with a carrier is then reconciled by level against
    // tierNotice, as after a route-invalid window (T-P4-H fix round 1, review S6).
    transitionGap() { st.sumGap = true; },
    run: () => st.run,
    coreRunning: () => !!st.core.running,
  };
}
