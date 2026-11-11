// Overview-only derivations (T-P4-05a; mapping_overview sections 3 and 4, criticism C10).
// Pure functions over the projected store keys (rt/project.js) and the raw v1 DTOs of the
// topic cache: no DOM, no store writes, no catalog text (src/rt carries no CJK literals,
// criticism E0). pages/overview.js imports them and turns the descriptors into words
// (overview.* messages, T.rt.errorText / T.rt.reasonText); ui/test/unit/overview_data.test.mjs
// tests them.
//
// Rules taken from the mapping and the rulings:
//   * counts are always the runtime's own (no recount);
//   * v1 tiers.summary.degraded = some node of the carrier is unusable (R2), never "latency
//     rising"; the latency-rising words are B-01 (heroOf's slow input, only when the page's
//     feature('B-01') branch passes it);
//   * the deck shows the three fallback tiers; the runtime order has four ids with the carrier
//     first (decision 4th wave 02b-8, 5th wave 02b-4): gateway never enters the deck;
//   * phase is Math.max(0, phase) (decision 4th wave 02a-3);
//   * cancelling (starting with stop_requested) reads as cancelling everywhere (5th wave 02b-5);
//   * Bastion's denominator is capacity (D9; 5th wave 02b-6);
//   * IdentityResult.trigger tells manual (verified) from auto (confirmed); without it the
//     page's own request ids decide (C2 item 2, R3; 5th wave 02b-8);
//   * fields added by T-P4-C1 / C2 (killswitch.triggered, IdentityLeg city / timezone / asn /
//     org / type / single_source, IdentityResult.trigger, tiers.summary.tiers[t].pending,
//     Node.slow) are shown when present and never inferred when absent (criticism A2).

export const DECK = Object.freeze(['vanguard', 'sentinel', 'bastion']);
// a tier with more nodes than this draws a meter instead of one dot per node (R12)
export const DOT_MAX = 24;
// protocol mix: at most this many named entries, the rest merge into "other" (G33, L5)
export const PROTO_MAX = 6;
// the order of protocols with equal counts (the prototype's fixed order)
export const PROTO_ORDER = Object.freeze(['shadowsocks', 'ss', 'vmess', 'hysteria2', 'hy2', 'trojan', 'vless', 'tuic', 'socks5', 'http']);

// readiness.state (PROTOCOL 5.1) -> the hero's kind (G06, S-H1..S-H4)
const HOLDS = new Set(['activation_hold', 'controller_hold', 'facility_hold']);
// IdentityResult reason codes that keep the address and add a warning (S-E9, bridge_exit
// WARNING_REASONS)
export const EXIT_WARN = Object.freeze(['partial_validation', 'system_proxy_capture_inactive', 'system_proxy_host_path_unverified']);
// the address must not be shown at all (S-E10, D21)
export const EXIT_HIDE = Object.freeze(['identity_conflict', 'identity_rotating']);
// the four Fallback booleans of IdentityResult (PROTOCOL 15, D23), in bridge_exit order
export const EXIT_FLAGS = Object.freeze(['native_fallback_verified', 'native_fallback_verified_commit_rejected', 'native_fallback_unverified_budget', 'manual_execution_binding_stale']);
// running progress phases (bridge_exit running reason codes)
const RUN_PHASES = new Set(['querying', 'upgrading', 'deferred', 'retrying', 'rate_limited', 'waiting_connection']);

const good = s => s === 'ok' || s === 'partial';
const num = v => (Number.isFinite(Number(v)) ? Number(v) : 0);

/* ------------------------------------------------------------------ tiers */

/** The three deck tiers in the runtime's order (gateway and unknown ids dropped, missing ones appended). */
export function deckOrder(order) {
  const out = [];
  for (const id of Array.isArray(order) ? order : []) if (DECK.includes(id) && !out.includes(id)) out.push(id);
  for (const id of DECK) if (!out.includes(id)) out.push(id);
  return out;
}

const enabledTier = (tiers, id) => !!(tiers && tiers[id] && tiers[id].enabled);

/** The enabled fallback tiers in fallback order (the stopped deck's sentence, E16). */
export function chainOf(tiers) {
  const on = DECK.filter(id => enabledTier(tiers, id));
  return on.length ? on : ['vanguard'];
}

/** The tier the service tries first: the first enabled one of the fallback order (A13, G34). */
export function firstTier(tiers) { return chainOf(tiers)[0]; }

/** The preferred tier a fallback left behind: the first enabled fallback tier ahead of the carrier (A7). */
export function leftBehind(carrier, tiers) {
  const at = DECK.indexOf(carrier);
  const before = (at < 0 ? DECK : DECK.slice(0, at)).filter(id => enabledTier(tiers, id));
  return before[0] || 'vanguard';
}

/** Dots or a meter for n nodes of which ok are usable (E8, K5, R12). */
export function dotsOf(total, ok, style = '') {
  const n = Math.max(0, num(total)), k = Math.max(0, Math.min(n, num(ok)));
  if (n > DOT_MAX) return { meter: true, v: n ? k / n : 0, style };
  return { n, ok: k, style };
}

/* ------------------------------------------------------------- run / hero */

export function readinessKind(readiness) {
  if (!readiness || typeof readiness !== 'object') return null;
  const s = readiness.state;
  if (s === 'fail_closed') return 'fail_closed';
  if (s === 'data_plane_failed') return 'data_plane_failed';
  if (HOLDS.has(s)) return 'hold';
  if (readiness.validation_active === true) return 'validating';
  if (s === 'serving' || s === 'ready') return 'ready';
  return null;
}

/** true when service.state's failure is the stop that superseded a start (S-R3): shown as stopped. */
export const supersededFailure = failure => !!(failure && (failure.code === 'superseded' || failure.reason_code === 'superseded'));

/**
 * The run pill's face (B, G05; S-R1, S-R4, S-R7).
 *   connecting · start · starting · cancelling · running · stopping · failed · failed_stop · stop_failed
 * failed_stop keeps 停止 beside 重试 while the Core still runs or still has cleanup pending.
 */
export function pillMode({ run, cancelling, core, failure, stopFailed }) {
  const c = core || {};
  if (run === 'unknown' || run == null) return 'connecting';
  if (stopFailed && (run === 'running' || run === 'stopping' || run === 'failed')) return 'stop_failed';
  if (run === 'failed') {
    if (supersededFailure(failure) && !c.running && !c.cleanup_pending) return 'start';
    return c.running || c.cleanup_pending ? 'failed_stop' : 'failed';
  }
  if (run === 'stopped') return 'start';
  if (run === 'starting') return cancelling ? 'cancelling' : 'starting';
  if (run === 'running') return 'running';
  if (run === 'stopping') return 'stopping';
  return 'connecting';
}

/** The kill switch word for the hero's suffix and the strip (projected killswitch state, J). */
export function ksKind(killswitch) {
  switch (killswitch) {
    case 'disabled': return 'off';
    case 'blocked': return 'blocked';
    case 'error': return 'error';
    case 'triggered': return 'triggered';
    case 'armed': return 'armed';
    case 'inactive': return 'inactive';
    default: return 'unknown';
  }
}
// the hero appends a suffix only when the kill switch's promise is off
const ksSuffix = k => (k === 'off' || k === 'blocked' || k === 'error' ? k : 'none');

/**
 * The hero (A, G06; S-H1..S-H6, S-R2, S-R7, S-R8).
 * in: { run, cancelling, phase, restarting, carrier, fallback, tiers, killswitch (projected word),
 *       readiness, failure, offline (tiers.summary.offline_reason), stale (service.state),
 *       stopFailed (the page's last stop failed), stopError, note (a transient note object),
 *       slow (B-01 branch only), again (a runtime generation was lost) }
 * out: { name: {kind:'tier', tier} | {kind:'state', state}, dot, hint: {kind, ...} }
 */
export function heroOf(i) {
  const run = i.run, tiers = i.tiers || {}, ks = ksKind(i.killswitch);
  if (run === 'unknown' || run == null) return { name: { kind: 'state', state: 'connecting' }, dot: 'off', hint: { kind: 'connecting', again: !!i.again } };
  if (run === 'failed' && !supersededFailure(i.failure)) {
    return { name: { kind: 'state', state: 'failed' }, dot: 'err', hint: i.stopFailed ? { kind: 'stop_failed', error: i.stopError || null } : { kind: 'failed', failure: i.failure || null } };
  }
  if (run === 'starting') {
    if (i.cancelling) return { name: { kind: 'state', state: 'cancelling' }, dot: 'busy', hint: { kind: 'stopping', cancelling: true } };
    return { name: { kind: 'state', state: 'starting' }, dot: 'busy', hint: { kind: 'phase', phase: Math.max(0, num(i.phase)) } };
  }
  if (run === 'stopping') {
    if (i.stopFailed) return { name: { kind: 'state', state: 'stopping' }, dot: 'err', hint: { kind: 'stop_failed', error: i.stopError || null } };
    return { name: { kind: 'state', state: 'stopping' }, dot: 'busy', hint: { kind: 'stopping', cancelling: false } };
  }
  if (run !== 'running') {
    const name = { kind: 'state', state: 'stopped' };
    return i.note ? { name, dot: 'warn', hint: { kind: 'note', note: i.note } } : { name, dot: 'off', hint: { kind: 'stopped', tier: firstTier(tiers) } };
  }

  // running
  if (i.stale) return { name: i.carrier ? { kind: 'tier', tier: i.carrier } : { kind: 'state', state: 'unconfirmed' }, dot: 'off', hint: { kind: 'stale' } };
  const rk = readinessKind(i.readiness);
  const blocked = rk === 'fail_closed' || ks === 'triggered' || i.offline === 'fail_closed';
  const name = blocked ? { kind: 'state', state: 'blocked' } : i.carrier ? { kind: 'tier', tier: i.carrier } : { kind: 'state', state: 'unconfirmed' };
  const suffix = ksSuffix(ks);
  const warnKs = suffix !== 'none';
  if (i.stopFailed) return { name, dot: 'err', hint: { kind: 'stop_failed', error: i.stopError || null } };
  if (i.restarting) return { name, dot: 'busy', hint: { kind: 'restarting' } };
  if (i.note) return { name, dot: blocked ? 'err' : 'warn', hint: { kind: 'note', note: i.note } };
  if (blocked) return { name, dot: 'err', hint: { kind: 'blocked' } };
  if (rk === 'data_plane_failed') return { name, dot: 'err', hint: { kind: 'data_plane_failed' } };
  if (rk === 'hold') return { name, dot: 'warn', hint: { kind: 'hold' } };
  if (rk === 'validating') return { name, dot: 'busy', hint: { kind: 'validating' } };
  if (!i.carrier) return { name, dot: 'warn', hint: { kind: 'unconfirmed' } };
  // Gateway as the exit (review 05a M1): the route resolves FINAL-EXIT -> EXIT-SELECT -> the
  // Gateway node, so the runtime's carrier is 'gateway' and fallback is always false; the
  // fallback tier the Gateway node dials through (dialer-proxy PROXY-FALLBACK) is not on that
  // chain and v1 does not report it. The hero names Gateway with its own role word (固定出口,
  // never a fallback tier) and says the tier under it is unconfirmed; no node count of Gateway
  // is passed off as the carrier's.
  if (i.carrier === 'gateway') return { name, dot: warnKs ? 'warn' : 'ok', hint: { kind: 'gateway', ks: suffix } };
  const c = tiers[i.carrier] || {};
  if (i.fallback) return { name, dot: 'warn', hint: { kind: 'fallback', tier: leftBehind(i.carrier, tiers), ks: suffix } };
  if (i.slow) return { name, dot: 'warn', hint: { kind: 'slow', tier: i.carrier, ks: suffix } };
  const total = num(c.total), ok = num(c.ok);
  // no count for the carrier (the runtime had no manager to count with): a neutral sentence,
  // never 全部节点可用 (review 05a S8)
  if (!total) return { name, dot: warnKs ? 'warn' : 'ok', hint: { kind: 'nodes_unknown', ks: suffix } };
  const count = ok < total ? 'partial' : 'all';
  return { name, dot: warnKs ? 'warn' : 'ok', hint: { kind: 'nodes', count, ok, total, ks: suffix } };
}

/** The one health word of every "running" dot (hero, run pill): heroOf's dot while running. */
export function healthOf(i) { return i.run === 'running' ? heroOf(i).dot : i.run === 'stopped' ? 'off' : 'busy'; }

/* ------------------------------------------------------------------- deck */

/** The node a tier carries traffic through: the route's chain member found in that tier's nodes.list (E11). */
export function currentNode(route, items, tier) {
  const rows = Array.isArray(items) ? items : [];
  if (!route || !Array.isArray(route.selected_chain)) return null;
  const chain = route.selected_chain;
  for (let i = chain.length - 1; i >= 0; i--) {
    const row = rows.find(r => r && r.name === chain[i]);
    if (row) return row;
  }
  if (route.managed_tier === tier && typeof route.leaf === 'string' && route.leaf) return { name: route.leaf, protocol: null, latency_ms: null };
  return null;
}

/** The most common reason_code of the unusable rows (E15): {code, mixed}. */
export function downReason(items) {
  const n = new Map();
  for (const r of Array.isArray(items) ? items : []) {
    if (!r || r.status === 'healthy' || r.status === 'disabled' || !r.reason_code) continue;
    n.set(r.reason_code, (n.get(r.reason_code) || 0) + 1);
  }
  if (!n.size) return { code: null, mixed: false };
  const all = [...n.values()].reduce((a, b) => a + b, 0);
  const [code, k] = [...n.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))[0];
  return { code, mixed: k * 2 <= all && n.size > 1 };
}

/**
 * One deck card's state line (E7, E17-E23, S-P3, S-P4).
 * in: { tier, run, cancelling, carrier, front (the deck's first tier), tiers, collected, bastion (store),
 *       bastionKnown, summaryKnown }
 * out: { stat: {kind, ...params}, tone: ''|'warn'|'err'|'mute', dots }
 */
export function deckStat({ tier: k, run, carrier, front = null, tiers, collected, bastion, bastionKnown = true, summaryKnown = true }) {
  const c = (tiers || {})[k] || { ok: 0, total: 0, enabled: false };
  const ghost = run === 'stopped' || run === 'stopping' || run === 'failed';
  const col = collected || {};
  if (run === 'unknown' || run == null || !summaryKnown) return { stat: { kind: 'unknown' }, tone: 'mute', dots: dotsOf(0, 0, 'off') };
  if (k === 'bastion') {
    if (!c.enabled) return { stat: { kind: 'bastion_off' }, tone: 'mute', dots: { meter: true, v: 0, style: 'off' } };
    const bp = bastion || {};
    const cap = num(bp.capacity), on = run === 'running', n = on ? num(bp.ok) : 0, min = num(bp.min);
    const p = { n, capacity: cap, min };
    const meter = (style) => ({ meter: true, v: cap ? Math.min(1, n / cap) : 0, style });
    if (ghost) return { stat: { kind: 'bastion_unknown' }, tone: 'mute', dots: meter('off') };
    if (run === 'starting' && !col.bastion) return { stat: { kind: 'collecting' }, tone: 'mute', dots: meter('off') };
    if (run === 'starting') return { stat: { kind: 'bastion_after_ready' }, tone: 'mute', dots: meter('off') };
    if (!bastionKnown || !cap) return { stat: { kind: 'bastion_unknown' }, tone: 'mute', dots: meter('off') };
    const carrying = carrier === 'bastion';
    if (bp.collecting) return { stat: { kind: 'bastion_filling', ...p }, tone: '', dots: meter(carrying ? 'warn' : 'ink') };
    if (n < min) return { stat: { kind: 'bastion_low', ...p }, tone: 'warn', dots: meter('warn') };
    return { stat: { kind: 'bastion_standby', ...p }, tone: '', dots: meter(carrying ? 'warn' : 'ink') };
  }
  const total = num(c.total), ok = num(c.ok);
  if (c.enabled && !total) return { stat: { kind: 'unconfigured' }, tone: 'mute', dots: dotsOf(0, 0) };
  if (!c.enabled) return { stat: { kind: 'off' }, tone: 'mute', dots: dotsOf(total, 0, 'off') };
  if (ghost) return { stat: { kind: 'nodes', total }, tone: 'mute', dots: dotsOf(total, 0, 'off') };
  if (ok <= 0) return { stat: { kind: 'down', total }, tone: 'err', dots: dotsOf(total, 0, 'fail') };
  // Gateway carries (M1): which fallback tier its node dials through is unknown; the deck's
  // front (the first tier of the fallback order) is the likely one, so it says unconfirmed
  // instead of 待命 (and never claims to carry)
  if (carrier === 'gateway' && run === 'running' && k === front) return { stat: { kind: 'unconfirmed', ok, total }, tone: '', dots: dotsOf(total, ok) };
  // the carrier; while starting (no carrier yet) the tier the start leads with, the deck's front
  if (k === carrier || (run === 'starting' && !carrier && k === front)) return { stat: { kind: 'usable', ok, total }, tone: '', dots: dotsOf(total, ok) };
  return { stat: { kind: 'standby', ok, total }, tone: '', dots: dotsOf(total, ok) };
}

/**
 * The front-only pill of a deck card (E9, E10).
 * pill: slow (B-01 only) · fallback · down · carrying · unconfirmed (Gateway carries, M1) · standby ·
 *       starting · cancelling · stopping · stopped · unknown
 */
export function deckPill({ tier: k, run, cancelling, carrier, tiers, slow = false }) {
  const c = (tiers || {})[k] || {};
  if (run === 'unknown' || run == null) return 'unknown';
  if (run === 'starting') return cancelling ? 'cancelling' : 'starting';
  if (run === 'stopping') return 'stopping';
  if (run !== 'running') return 'stopped';
  if (k === carrier && slow) return 'slow';
  if (k === carrier && (k === 'sentinel' || k === 'bastion')) return 'fallback';
  if (c.enabled && num(c.ok) <= 0 && k !== 'bastion') return 'down';
  if (carrier === 'gateway') return 'unconfirmed';
  if (k === carrier) return 'carrying';
  return 'standby';
}

/**
 * The Gateway row (F1-F7, S-G1).
 * in: { run, tiers, collected, phase, gateway (gateway.current DTO or null) }
 * out: { kind: 'unknown'|'off'|'waiting'|'verifying'|'stopped'|'missing'|'pending'|'current'|'selected', name, ok, total, dots, tone }
 * 'current' only with an effective Gateway (gateway.current.effective: the published route goes
 * through it) while running, or while starting once the Gateway was collected (the starting
 * branch above lets an effective Gateway through then: the route already goes through it, so
 * it is the current exit; fix round 1 note); a selection that is not in force (effective null: a
 * route-invalid window, where bridge_gateway_status clears effective / missing / applied, or the
 * switch-over before the chain goes through it) is 'selected', never "current" (05a review 1
 * S3; 8th-wave ruling T-P4-H2 item 3).
 */
export function gatewayRow({ run, tiers, collected, phase, gateway }) {
  const g = (tiers || {}).gateway || { ok: 0, total: 0, enabled: false };
  const total = num(g.total), ok = num(g.ok), gc = gateway || null;
  const name = gc ? (gc.effective || gc.selected || null) : null;
  if (run === 'unknown' || run == null) return { kind: 'unknown', name: null, ok, total, dots: dotsOf(0, 0, 'off'), tone: 'mute' };
  if (!g.enabled || (gc && gc.selected == null && gc.effective == null)) return { kind: 'off', name: null, ok, total, dots: dotsOf(total, 0, 'off'), tone: 'mute' };
  if (run === 'stopped' || run === 'stopping' || run === 'failed') return { kind: name ? 'stopped' : 'unknown', name, ok, total, dots: dotsOf(total, 0, 'off'), tone: 'mute' };
  if (run === 'starting' && !(collected || {}).gateway) return { kind: Math.max(0, num(phase)) >= 1 ? 'verifying' : 'waiting', name, ok, total, dots: dotsOf(total, 0, 'off'), tone: 'mute' };
  if (gc && gc.missing) return { kind: 'missing', name, ok, total, dots: dotsOf(total, ok), tone: 'warn' };
  if (gc && gc.applied === false && gc.pending) return { kind: 'pending', name, ok, total, dots: dotsOf(total, ok), tone: 'warn' };
  if (!name) return { kind: 'unknown', name: null, ok, total, dots: dotsOf(total, ok), tone: 'mute' };
  if (!gc.effective) return { kind: 'selected', name, ok, total, dots: dotsOf(total, ok), tone: '' };
  return { kind: 'current', name, ok, total, dots: dotsOf(total, ok), tone: '' };
}

/**
 * The route line under 当前出口 (D17, D18).
 * running: {kind:'via', via, exit} | {kind:'direct', via} | {kind:'gateway', exit} | {kind:'unconfirmed'}
 * not running: {kind:'plan', tier, gateway:bool} (code names only: no node name while stopped)
 * Gateway carries (M1): the route's leaf is the Gateway node and the tier under it is not on the
 * chain: {kind:'gateway', exit} (经由未确认的承载层 → <exit>), never 直接出口.
 */
export function routeLine({ run, carrier, route, items, gateway, tiers }) {
  if (run === 'running') {
    if (!route || route.valid === false || !carrier) return { kind: 'unconfirmed' };
    if (carrier === 'gateway') {
      const exit = (gateway && gateway.effective) || (route.managed_tier === 'gateway' && typeof route.leaf === 'string' && route.leaf) || null;
      return exit ? { kind: 'gateway', exit } : { kind: 'unconfirmed' };
    }
    const node = currentNode(route, items, carrier);
    const exit = gateway && gateway.effective ? gateway.effective : null;
    if (!node) return { kind: 'unconfirmed' };
    if (exit && exit !== node.name) return { kind: 'via', via: node.name, viaTier: carrier, exit };
    return { kind: 'direct', via: node.name, viaTier: carrier };
  }
  const t = tiers || {};
  const gw = !!(t.gateway && t.gateway.enabled) && !!(gateway ? gateway.selected : true);
  return { kind: 'plan', tier: firstTier(t), gateway: gw };
}

/* ------------------------------------------------------------- strip */

/** The listener the system proxy points at (I7): "host:port" of app.info.listeners[kind=mixed], or null. */
export function mixedListener(listeners) {
  const l = (Array.isArray(listeners) ? listeners : []).find(x => x && x.kind === 'mixed');
  if (!l || !l.host || !l.port) return null;
  const host = String(l.host).includes(':') && !String(l.host).startsWith('[') ? `[${l.host}]` : String(l.host);
  return `${host}:${l.port}`;
}

/**
 * The 网络模式 hint (I6-I13, G08; S-M1..S-M7) when nothing transient (switching, a gate note,
 * a command result) is shown.
 * kinds: unknown · gated · drift · proxy_unknown · tun · system · mixed (with state running|starting|other)
 */
export function modeHint({ run, mode, modeKnown, admin, gatedModes, systemProxy }) {
  if (!modeKnown) return { kind: 'unknown' };
  if (gatedModes && gatedModes.includes(mode) && !admin) return { kind: 'gated' };
  const sp = systemProxy || {};
  const usesProxy = mode === 'system' || mode === 'mixed';
  if (run === 'running' && usesProxy && (sp.drift === true || sp.proxy_state === 'other')) return { kind: 'drift' };
  if (run === 'running' && usesProxy && sp.proxy_state === 'unknown') return { kind: 'proxy_unknown' };
  const state = run === 'running' ? (usesProxy && sp.proxy_state && sp.proxy_state !== 'owned' ? 'starting' : 'running') : run === 'starting' ? 'starting' : 'other';
  if (mode === 'tun') return { kind: 'tun', state };
  return { kind: mode === 'mixed' ? 'mixed' : 'system', state };
}

/** A mode.set outcome (I11-I13, S-M3..S-M7): the note the hint shows for a while, or null. */
export function modeOutcome({ ok, result, error, run, cleanupPending }) {
  if (ok) {
    const r = result || {};
    if (r.applied === true && run !== 'running' && run !== 'starting') return { kind: 'next_start' };
    return null;
  }
  const e = error || {};
  const code = e.code, reason = e.reason_code || null, res = e.result || {};
  if (code === 'cancelled' || code === 'superseded' || code === 'runtime_gone' || code === 'owner_revoked') return null;
  const apply = res.apply_error || null;
  // an explicit admin_required / conflict is that, whatever else is pending (review 05a S2)
  if (code === 'admin_required' || reason === 'admin_required') return { kind: 'admin_required' };
  if (code === 'conflict') return { kind: 'conflict' };
  // the error itself says cleanup_pending
  const said = reason === 'cleanup_pending' || (apply && apply.reason_code === 'cleanup_pending')
    || String(e.message_key || '').endsWith('cleanup_pending') || (apply && String(apply.message_key || '').endsWith('cleanup_pending'));
  if (said) return { kind: 'cleanup_pending' };
  const notApplied = res.saved === true && res.applied === false;
  // the global core.cleanup_pending explains only a mode that was not applied (S-M4: 未应用 +
  // cleanup_pending) or an io failure, not any failure
  if (cleanupPending && (notApplied || code === 'io')) return { kind: 'cleanup_pending' };
  if (notApplied) return { kind: 'not_applied', error: apply || e };
  return { kind: 'failed', error: e };
}

/** 节点池 (K1-K7, S-P1, S-P2). value: '—' unless known; pending only when some tier carries the field. */
export function poolCell({ run, pool, summary, stale }) {
  const known = run === 'running' && !!summary && !stale;
  const p = pool || { ok: 0, total: 0 };
  let pending = null;
  const tiers = summary && summary.tiers;
  if (known && tiers) for (const id of ['vanguard', 'sentinel', 'gateway']) {
    const t = tiers[id];
    if (t && Number.isInteger(t.pending)) pending = (pending || 0) + t.pending;
  }
  if (!known) return { known: false, state: run === 'starting' ? 'starting' : 'other' };
  const total = num(p.total), ok = num(p.ok);
  return { known: true, ok, total, rate: total ? ok / total : null, pending: pending || 0 };
}

/* ---------------------------------------------------------------- exit */

function legOf(leg) {
  const l = leg && typeof leg === 'object' ? leg : {};
  const s = v => (typeof v === 'string' && v ? v : null);
  return {
    status: s(l.status) || 'unknown', address: s(l.address), country: s(l.country), reason: s(l.reason_code),
    city: s(l.city), timezone: s(l.timezone), asn: s(l.asn), org: s(l.org), type: s(l.type),
    single: l.single_source === true,
  };
}

/** manual | auto: IdentityResult.trigger (C2), else whether this window asked for it (R3). */
export function exitHow(result, ownIds) {
  if (!result) return 'auto';
  if (result.trigger === 'manual') return 'manual';
  if (result.trigger === 'auto' || result.trigger === 'recheck') return 'auto';
  return ownIds && typeof ownIds.has === 'function' && ownIds.has(result.request_id) ? 'manual' : 'auto';
}

const firstFlag = r => EXIT_FLAGS.find(f => r && r[f] === true) || null;

/**
 * 当前出口 (D, G07, G09; S-E1..S-E17).
 * in: { run, cancelling, phase, identity ({current, previous} DTO or null), curHow (the projection's
 *       manual|auto guess for current), ownIds, auto (overview_auto_exit_lookup: true | false | null
 *       unknown), stale, canVerify }
 * out: {
 *   show: 'addr' | 'msg',
 *   legs: {v4, v6} (show=addr), last (from previous), dim (a check is running over it),
 *   badge: {kind: idle|running|ok|warn|info|expired|failed|stale|unknown, how?, at?, reason?, phase?},
 *   msg: {kind: connecting|stopped|starting|stopping|wait|auto_off|running|hidden|failed, reason?, phase?, spin?},
 *   refresh: {shown, enabled}, same (IPv6 location = IPv4's)
 * }
 */
export function exitCard(i) {
  const run = i.run, idt = i.identity || null;
  const cur = idt && idt.current ? idt.current : null, prev = idt && idt.previous ? idt.previous : null;
  const busy = !!(cur && cur.status === 'running');
  const can = i.canVerify !== false;
  // cancelling a start disables the actions (5th wave 02b-5; review 05a S5): a 刷新 then would
  // only queue a deferred check that is dropped at once
  const refresh = { shown: run === 'running' || run === 'starting', enabled: (run === 'running' || run === 'starting') && can && !busy && !i.cancelling };
  const out = { show: 'msg', legs: null, last: false, dim: false, badge: { kind: 'idle' }, msg: { kind: 'stopped' }, refresh, same: true };
  const addr = (r, last) => {
    const v4 = legOf(r.v4), v6 = legOf(r.v6);
    out.show = 'addr'; out.legs = { v4, v6 }; out.last = !!last;
    out.same = !v6.country || !v4.address || v6.country === v4.country;
    if (!v4.address && !v6.address) { out.show = 'msg'; out.legs = null; }
  };
  if (run === 'unknown' || run == null) { out.badge = { kind: 'unknown' }; out.msg = { kind: 'connecting' }; return out; }
  if (run === 'stopping') { out.badge = { kind: 'idle', state: 'stopped' }; out.msg = { kind: 'stopping' }; return out; }
  if (run !== 'running' && run !== 'starting') { out.badge = { kind: 'idle', state: 'stopped' }; out.msg = { kind: 'stopped' }; return out; }
  if (!cur) {
    out.badge = { kind: i.stale ? 'stale' : 'idle', state: run === 'starting' ? 'starting' : 'wait' };
    out.msg = run === 'starting' ? { kind: 'starting', phase: Math.max(0, num(i.phase)) } : i.auto === false ? { kind: 'auto_off' } : { kind: 'wait' };
    return out;
  }
  // trigger first (C2); without it the projection's own-request guess (store exit.how) or this
  // page's request ids. A running result without trigger is a manual one: the automatic lookup
  // publishes no progress at all (bridge_exit module notes; review 05a S7), and this window
  // learns its own request id only when the job ends
  const status = cur.status;
  const how = cur.trigger ? exitHow(cur) : status === 'running' || i.curHow === 'manual' ? 'manual' : exitHow(cur, i.ownIds);
  if (status === 'running') {
    const phase = RUN_PHASES.has(cur.reason_code) ? cur.reason_code : 'querying';
    out.badge = { kind: 'running', how, phase };
    if (prev && good(prev.status) && phase !== 'deferred') { addr(prev, true); out.dim = out.show === 'addr'; }
    if (out.show !== 'addr') out.msg = run === 'starting' && phase === 'deferred' ? { kind: 'starting', phase: Math.max(0, num(i.phase)), deferred: true } : { kind: 'running', phase, spin: true };
  } else if (good(status)) {
    addr(cur, false);
    const reason = cur.reason_code || null;
    const flag = firstFlag(cur);
    if (EXIT_HIDE.includes(reason)) { out.show = 'msg'; out.legs = null; out.badge = { kind: 'failed', reason }; out.msg = { kind: 'hidden', reason }; }
    else if (cur.fresh === false || reason === 'identity_stale') out.badge = { kind: 'expired', reason: reason || 'identity_stale', how, at: cur.observed_at_ms };
    else if (EXIT_WARN.includes(reason)) out.badge = { kind: 'warn', reason, how, at: cur.observed_at_ms };
    else if (flag) out.badge = { kind: flag === 'native_fallback_verified' ? 'info' : 'warn', reason: flag, how, at: cur.observed_at_ms };
    else if (reason === 'route_contract_failed') out.badge = { kind: 'warn', reason, how, at: cur.observed_at_ms };
    else out.badge = { kind: 'ok', how, at: cur.observed_at_ms };
    if (out.show !== 'addr' && out.msg.kind !== 'hidden') out.msg = { kind: 'wait' };
  } else if (status === 'failed') {
    const reason = cur.reason_code || firstFlag(cur) || 'unavailable';
    out.badge = { kind: 'failed', reason };
    if (EXIT_HIDE.includes(reason)) out.msg = { kind: 'hidden', reason };
    else if (prev && good(prev.status)) addr(prev, true);
    if (out.show !== 'addr' && out.msg.kind !== 'hidden') out.msg = { kind: 'failed', reason };
  } else if (status === 'cancelled' || status === 'superseded') {
    // quietly back to the previous result (S-E15)
    if (prev && good(prev.status)) { addr(prev, true); out.badge = { kind: 'ok', how: exitHow(prev, i.ownIds), at: prev.observed_at_ms }; }
    else { out.badge = { kind: 'idle', state: 'wait' }; out.msg = i.auto === false ? { kind: 'auto_off' } : { kind: 'wait' }; }
  } else {
    // idle (nothing measured in this Core generation) or a status this page does not know
    if (prev && good(prev.status)) { addr(prev, true); out.badge = { kind: 'expired', reason: 'identity_stale', how: exitHow(prev, i.ownIds), at: prev.observed_at_ms }; }
    else { out.badge = { kind: 'idle', state: run === 'starting' ? 'starting' : 'wait' }; out.msg = run === 'starting' ? { kind: 'starting', phase: Math.max(0, num(i.phase)) } : i.auto === false ? { kind: 'auto_off' } : { kind: 'wait' }; }
  }
  if (i.stale) out.badge = { kind: 'stale' };
  return out;
}

/* ------------------------------------------------------------ protocols */

/**
 * protocols.mix (L1-L7, D6, G33): rows sorted by count (desc) then the fixed protocol order,
 * the tail beyond PROTO_MAX merged into one "other" row; entries = Σcounts (R5: no node count).
 */
export function protoMix(mix) {
  const counts = mix && mix.counts && typeof mix.counts === 'object' ? mix.counts : {};
  const rank = name => { const i = PROTO_ORDER.indexOf(String(name).toLowerCase()); return i < 0 ? PROTO_ORDER.length : i; };
  const rows = Object.keys(counts).filter(k => Number.isInteger(counts[k]) && counts[k] > 0)
    .map(name => ({ name, count: counts[name] }))
    .sort((a, b) => b.count - a.count || rank(a.name) - rank(b.name) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const entries = rows.reduce((a, r) => a + r.count, 0);
  let list = rows;
  if (rows.length > PROTO_MAX) {
    const head = rows.slice(0, PROTO_MAX - 1), tail = rows.slice(PROTO_MAX - 1);
    list = [...head, { name: null, other: true, count: tail.reduce((a, r) => a + r.count, 0), names: tail.map(r => r.name) }];
  }
  return { source: mix && mix.source === 'loaded' ? 'loaded' : mix && mix.source === 'configured' ? 'configured' : null, entries, list };
}

/* ---------------------------------------------------------------- chart */

/** A series is drawable with at least two points (the ruling's empty state: 0 or 1 point, no NaN). */
export const drawable = pts => Array.isArray(pts) && pts.filter(p => p && Number.isFinite(p.t)).length >= 2;

/** Indexes i where the line breaks before point i (a hole longer than 1.5 steps, H4: no interpolation). */
export function breaksOf(pts, step) {
  const out = [];
  for (let i = 1; i < (pts || []).length; i++) if (pts[i].t - pts[i - 1].t > step * 1.5) out.push(i);
  return out;
}

/** Fold one traffic.tick sample into the 24 h buckets (H3: the last bucket follows the ticks). */
export function foldTick(h24, sample, bucketMs = 300000, max = 288) {
  if (!sample || !Number.isFinite(sample.t)) return h24;
  const last = h24[h24.length - 1];
  if (last && sample.t >= last.t && sample.t - last.t < bucketMs) {
    const n = (last.n || 0) + 1;
    last.d += (sample.d - last.d) / n; last.u += (sample.u - last.u) / n; last.n = n;
  } else if (!last || sample.t >= last.t + bucketMs) {
    const t = last ? last.t + Math.floor((sample.t - last.t) / bucketMs) * bucketMs : sample.t;
    h24.push({ t, d: sample.d, u: sample.u, n: 1 });
    if (h24.length > max) h24.splice(0, h24.length - max);
  }
  return h24;
}
