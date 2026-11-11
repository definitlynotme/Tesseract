// Tiers page derivations (T-P4-05b; mapping_tiers sections 1, 2, 4, 6, 7, 8, 10, 12; P4
// architecture section 2.6). Pure functions over v1 DTOs, imported by pages/tiers.js (criticism
// C10) and unit-tested in ui/test/unit/tiers_data.test.mjs. Nothing here reads the store, the
// DOM or the clock; counts are always the runtime's own (no recount).
//
// Contract notes kept here (PROTOCOL section 16 and the rulings):
//   Node.status   healthy | failed | pending | unknown | disabled. pending with reason
//                 subscription_recheck_pending is "waiting for a recheck"; any other pending
//                 and unknown are "not checked yet". Node.slow (T-P4-C2, K-5) is shown only
//                 when the field is there. The latency ceiling only colours a value: it never
//                 re-judges a node (mapping_tiers section 1).
//   current       Vanguard / Sentinel / Bastion: route.published valid, managed_tier == tier and
//                 leaf == Node.name (only the carrying tier marks a node; standby tiers are B-01).
//                 Gateway: gateway.current.effective while the service runs (starting counts as
//                 running), gateway.current.selected while it is stopped. A Gateway row is
//                 marked only by gateway.current.selected_node_id (T-P2-H8) when the exit shown
//                 is the selected one; without that field the current exit is text only, never
//                 guessed from a row's name (7th-wave ruling, T-P2-H5 item 2).
//   drafts        config.items rows (kind node, enabled false) of the tier; a nodes.list row with
//                 status disabled of the same name is the same draft and wins.
//   subscriptions subscriptions.list rows; when the sidecar does not publish that topic
//                 (hello.caps.topics), config.items rows of kind subscription stand in (degraded:
//                 health unknown).
//   K items       fields of T-P4-C1 / C2 (subscription_id, slow, last_checked_at_ms, recheck,
//                 last_refresh_at_ms, endpoint, protocols, groups) are used when they are
//                 present on the DTO, never inferred (P4 architecture section 4 item 2).

export const TIERS = Object.freeze(['vanguard', 'sentinel', 'bastion', 'gateway']);

// nodes.diagnose / Node.reason_code vocabulary (ui/tools/backend_keys.py reasons tiers.reason)
// plus the page's own configuration_invalid; anything else reads as node_unavailable.
export const REASON_CODES = Object.freeze(['cancelled', 'chain_requires_gateway', 'cleanup_failed', 'component_missing',
  'configuration_invalid', 'connection_refused', 'connection_timeout', 'diagnostic_in_progress', 'diagnostic_unavailable',
  'disabled', 'dns_resolution_failed', 'invalid_request', 'latency_limit_exceeded', 'node_changed', 'node_index_unavailable',
  'node_not_found', 'node_unavailable', 'probe_failed', 'request_id_reused', 'service_closed', 'setup_failed', 'startup_failed',
  'subscription_recheck_pending', 'tier3_auto_pool_self_check', 'timeout', 'tls_failed', 'unsupported_protocol', 'upstream_missing']);
export const reasonKey = code => (REASON_CODES.includes(code) ? code : 'node_unavailable');

// The service counts as running while it starts (P4 contract memo: "starting is running").
export const isRunning = run => run === 'running' || run === 'starting';

const has = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k);

/** A node row's status key (T.ui.status vocabulary): ok | slow | down | pending | recheck | draft. */
export function nodeState(n) {
  if (!n) return 'pending';
  if (n.enabled === false || n.status === 'disabled') return 'draft';
  switch (n.status) {
    case 'healthy': return n.slow === true ? 'slow' : 'ok';
    case 'failed': return 'down';
    case 'pending': return n.reason_code === 'subscription_recheck_pending' ? 'recheck' : 'pending';
    default: return 'pending';
  }
}

/** The reason a row shows: only for a failed node (its reason_code, node_unavailable when none). */
export function nodeReason(n) {
  return nodeState(n) === 'down' ? reasonKey(n.reason_code || 'node_unavailable') : null;
}

/** With the service stopped an ok / slow / down state is the last known one. */
export const lastKnown = (state, run) => !isRunning(run) && (state === 'ok' || state === 'slow' || state === 'down');

/** Gateway's current exit from gateway.current (absent until T-P2-H6 publishes it: unknown). */
export function gatewayView(g, run) {
  if (!g || typeof g !== 'object') return { known: false, name: null, selected: null, nodeId: null, missing: false, applied: false, pending: null, waiting: false };
  const live = isRunning(run);
  const selected = typeof g.selected === 'string' && g.selected ? g.selected : null;
  const effective = typeof g.effective === 'string' && g.effective ? g.effective : null;
  const name = live ? effective : selected;
  // T-P2-H8: the node the selection resolves to; it names the row only while the exit shown is
  // the selected one (running with another effective exit: no row is marked)
  const sid = typeof g.selected_node_id === 'string' && g.selected_node_id ? g.selected_node_id : null;
  return {
    known: true, name, selected, nodeId: sid && name && name === selected ? sid : null,
    missing: g.missing === true, applied: g.applied === true, pending: g.pending || null,
    // running, an exit is chosen, it is not missing and not in effect yet
    waiting: live && !!selected && !effective && g.missing !== true,
  };
}

/** The name of the node that carries the tier now (null: none, or not known). */
export function currentName(tier, { route = null, gateway = null, run = 'unknown' } = {}) {
  if (tier === 'gateway') { const g = gatewayView(gateway, run); return g.known ? g.name : null; }
  if (!route || route.valid !== true || route.managed_tier !== tier || typeof route.leaf !== 'string' || !route.leaf) return null;
  if (!isRunning(run)) return null;
  return route.leaf;
}

export function isCurrent(node, tier, ctx) {
  if (!node || node.enabled === false || node.status === 'disabled') return false;
  if (tier === 'gateway') { const g = gatewayView(ctx && ctx.gateway, ctx ? ctx.run : 'unknown'); return !!g.nodeId && node.id === g.nodeId; }
  const name = currentName(tier, ctx);
  return !!name && (node.name === name || (tier === 'vanguard' && isVariantOf(name, node.name)));
}

/** §8u: nodes.list shows a multi-protocol Vanguard node as one row (its physical name); the route's
    leaf is the variant's proxy name, `<physical> :: <protocol label>` (tier1_protocols.tier1_proxy_name). */
export const isVariantOf = (leaf, name) => typeof leaf === 'string' && typeof name === 'string' && !!name
  && leaf.startsWith(name + ' :: ') && !leaf.slice(name.length + 4).includes(' :: ');

/** Draft rows of a tier: config.items nodes switched off, unless nodes.list already has the row. */
export function draftRows(tier, items, nodes) {
  const listed = new Set((nodes || []).filter(n => nodeState(n) === 'draft').map(n => n.name));
  return (items || []).filter(i => i && i.kind === 'node' && i.section === tier && i.enabled === false && !listed.has(i.name))
    .map(i => ({ id: i.id, name: i.name, tier, kind: 'manual', enabled: false, status: 'disabled', reason_code: null, protocol: '',
      latency_ms: null, configItemId: i.id, fromConfig: true }));
}

/** Enabled config.items nodes of a tier that nodes.list does not carry yet: listed during the wait after
    an import, until nodes.list catches up; its row id is the ConfigItem id, which nodes.delete /
    nodes.update accept. */
export function unloadedRows(tier, items, nodes) {
  const listed = new Set((nodes || []).map(n => n.name));
  return (items || []).filter(i => i && i.kind === 'node' && i.section === tier && i.enabled !== false && !listed.has(i.name))
    .map(i => ({ id: i.id, name: i.name, tier, kind: 'manual', enabled: true, status: 'pending', reason_code: null, protocol: '',
      latency_ms: null, configItemId: i.id, fromConfig: true }));
}

/** The ConfigItem id of a node (for nodes.update / nodes.delete of a disabled node: K-11). */
export function configItemIdOf(row, items, detail) {
  if (row && row.configItemId) return row.configItemId;
  if (detail && detail.config_item && typeof detail.config_item.id === 'string') return detail.config_item.id;
  if (!row) return null;
  const hit = (items || []).filter(i => i && i.kind === 'node' && i.section === row.tier && i.name === row.name);
  return hit.length === 1 ? hit[0].id : null;
}

/** Subscription rows of a tier; null while the topic has no snapshot yet. */
export function subscriptionRows(tier, { list, published, items }) {
  if (published) return Array.isArray(list) ? list.map(s => ({ ...s, degraded: false })) : null;
  return (items || []).filter(i => i && i.kind === 'subscription' && i.section === tier).map(i => ({
    id: i.id, name: i.name, tier, enabled: i.enabled !== false, display: '', total: null, healthy: null,
    visibility: 'unknown', refreshing: false, degraded: true,
  }));
}

/** The health word of a subscription row: current | previous | pending | unknown (G35). */
export function subscriptionHealth(s) {
  if (!s || s.degraded) return 'unknown';
  if (s.enabled === false) return 'disabled';
  return ['current', 'previous', 'pending', 'unknown'].includes(s.visibility) ? s.visibility : 'unknown';
}

/** K-4: membership is known when the sidecar sends Node.subscription_id (the key, even null). */
export const hasMembership = nodes => (nodes || []).some(n => has(n, 'subscription_id'));
export const membersOf = (subId, nodes) => (nodes || []).filter(n => n && n.subscription_id === subId);

/** Search: name, protocol and (K-4) the subscription's name (G40). */
export function matches(row, q, subName) {
  const s = String(q || '').trim().toLowerCase();
  if (!s) return true;
  return [row && row.name, row && row.protocol, subName].filter(Boolean).join(' ').toLowerCase().includes(s);
}

/** Latency sort key: usable rows by latency, then unchecked, then failed, drafts last. */
export function latKey(row) {
  const st = nodeState(row);
  if (st === 'draft') return 1e7;
  if (st === 'down') return 1e6;
  if (row.latency_ms == null || st === 'pending' || st === 'recheck') return 5e5;
  return row.latency_ms;
}

/** Ids of the rows that were not there before (write-after-read: import results are batch tokens). */
export function diffNew(before, rows) {
  const b = before instanceof Set ? before : new Set(before || []);
  return (rows || []).map(r => r.id).filter(id => !b.has(id));
}

/** nodes.detail answered after a diagnosis: the newer of the list row and the detail's node. The
    list row wins when the list moved on after the detail was requested (a later patch). */
export function pickNewer({ listRow = null, revAtRequest = 0, revNow = 0, detailNode = null }) {
  if (listRow && revNow > revAtRequest) return listRow;
  return detailNode || listRow;
}

/** Outcome of an Applied result (nodes.update / nodes.delete / gateway.select done). */
export function appliedOutcome(r, run) {
  if (!r) return 'unknown';
  if (r.pending) return 'pending';
  if (r.applied) return isRunning(run) ? 'applied' : 'next_start';
  if (r.saved) return 'unapplied';
  return 'unsaved';
}

/** The detailed error of a failed write: the result's apply_error (import, update) or the error. */
export function detailError(err) {
  const r = err && err.result;
  if (r && r.apply_error && typeof r.apply_error === 'object') return r.apply_error;
  return err || null;
}

/** Outcome of a failed nodes.update / nodes.delete: rolled_back | rollback_unconfirmed | unapplied |
    busy | error. A job that saved but did not apply ends failed with its Applied result
    (saved=true, applied=false); a rollback that could not be confirmed is not "saved". */
export function writeFailure(err) {
  const r = err && err.result, d = detailError(err);
  const reason = (d && d.reason_code) || (err && err.reason_code) || null;
  if (reason === 'rolled_back' && !(r && r.saved === true)) return 'rolled_back';
  if (reason === 'rollback_unconfirmed') return 'rollback_unconfirmed';
  if (r && r.saved === true && r.applied === false) return 'unapplied';
  if (err && (err.code === 'lifecycle_busy' || err.hold === true)) return 'busy';
  return 'error';
}

/** subscriptions.refresh: the four results of G35 plus the refusals (mapping_tiers 12.3). */
export function refreshOutcome({ result = null, error = null, run = 'unknown' } = {}) {
  if (result && !error) {
    if (result.stored && result.applied) return isRunning(run) ? 'applied' : 'next_start';
    if (result.stored && !result.applied) return 'stored_unapplied';
    return result.retained_previous ? 'kept_previous' : 'failed';
  }
  const r = error && error.result;
  if (error && error.code === 'lifecycle_busy') return 'busy_stopping';
  if (error && error.code === 'conflict' && !r) return error.reason_code === 'recovery_pending' ? 'recovery' : 'in_progress';
  if (r && r.stored && !r.applied) return 'stored_unapplied';
  if (r && r.retained_previous) return 'kept_previous';
  if (error && error.code === 'conflict') return error.reason_code === 'recovery_pending' ? 'recovery' : 'in_progress';
  return 'failed';
}

/** The pool panel of Bastion (D-sem 2, D9: only "collecting" plus the numbers; no phases). */
export function poolView(pool, { enabled = false, run = 'unknown', carrier = null } = {}) {
  if (!enabled) return { known: true, phase: 'off' };
  if (!pool || typeof pool !== 'object') return { known: false, phase: 'unknown' };
  const ok = pool.runtime_alive | 0, target = pool.target_alive | 0, capacity = pool.capacity | 0, min = pool.min_alive_nodes | 0;
  let phase;
  if (!isRunning(run)) phase = 'idle';
  else if (pool.collecting) phase = 'filling';
  else if (carrier === 'bastion') phase = 'carrying';
  else if (ok < min) phase = 'low';
  else phase = 'ready';
  /* §8u: the totals (auto pool; the user's own nodes) when the sidecar sends them, else null. The
     target stays internal: the page shows available/total, never the target */
  const cnt = k => (Number.isInteger(pool[k]) && pool[k] >= 0 ? pool[k] : null);
  const total = cnt('runtime_total'), userTotal = cnt('user_total');
  return { known: true, phase, ok, target, capacity, min, user: pool.user_alive | 0, total, userTotal, low: phase === 'low' };
}

// ---- import.specs (G19) ----------------------------------------------------------------------
// FieldSpec.conditions: visible_when / required_when [{field, values? | excluded_values? |
// present?}], advanced, group, min, max, notices [{code, visible_when}], implicit_default (only for
// evaluating conditions: a select is never prefilled with it, P4 contract memo for Shadowsocks).

/** The value a condition reads: what was entered, else the field's implicit default. */
export function conditionValue(fields, values, id) {
  const v = values ? values[id] : undefined;
  if (v != null && v !== '') return v;
  const f = (fields || []).find(x => x && x.id === id);
  const d = f && f.conditions ? f.conditions.implicit_default : undefined;
  return d == null ? '' : d;
}

export function conditionMet(list, fields, values) {
  if (!Array.isArray(list) || !list.length) return true;
  return list.every(c => {
    if (!c || typeof c.field !== 'string') return true;
    const v = conditionValue(fields, values, c.field);
    const present = v !== '' && v != null && v !== false;
    if (c.present != null) return c.present ? present : !present;
    if (Array.isArray(c.values)) return c.values.map(String).includes(String(v));
    if (Array.isArray(c.excluded_values)) return !c.excluded_values.map(String).includes(String(v));
    return true;
  });
}

export function fieldVisible(f, fields, values) {
  const c = (f && f.conditions) || {};
  return conditionMet(c.visible_when, fields, values);
}

export function fieldRequired(f, fields, values) {
  if (!f) return false;
  if (!fieldVisible(f, fields, values)) return false;
  if (f.required) return true;
  const c = f.conditions || {};
  return Array.isArray(c.required_when) && c.required_when.length > 0 && conditionMet(c.required_when, fields, values);
}

/** Notice codes of a field shown for the current values (catalog.notice.<code>). */
export function fieldNotices(f, fields, values) {
  const n = f && f.conditions && Array.isArray(f.conditions.notices) ? f.conditions.notices : [];
  return n.filter(x => x && typeof x.code === 'string' && conditionMet(x.visible_when, fields, values)).map(x => x.code);
}

/** The protocols import.specs allows for a tier (its `tier` field's choices); chain: only those
    with conditions.chain_allowed !== false. */
export function protocolsForTier(specs, tier, { chain = false } = {}) {
  const list = specs && Array.isArray(specs.protocols) ? specs.protocols : [];
  return list.filter(p => {
    const tf = (p.fields || []).find(f => f.id === 'tier');
    if (tf && Array.isArray(tf.choices) && tf.choices.length && !tf.choices.includes(tier)) return false;
    if (chain && tf && tf.conditions && tf.conditions.chain_allowed === false) return false;
    return true;
  });
}

/** The form fields of a protocol in the order of G19: required first, then optional, advanced apart.
    The `tier` field is the page's own (the sheet's tier). `shared` (a chain row) drops name / server. */
export function formFields(spec, values, { shared = false } = {}) {
  const fields = (spec && spec.fields ? spec.fields : []).filter(f => f && f.id !== 'tier' && !(shared && (f.id === 'name' || f.id === 'server')));
  const vis = fields.filter(f => fieldVisible(f, fields, values));
  const main = vis.filter(f => !(f.conditions && f.conditions.advanced));
  const adv = vis.filter(f => f.conditions && f.conditions.advanced);
  const req = f => fieldRequired(f, fields, values);
  const order = list => list.filter(req).concat(list.filter(f => !req(f)));
  return { main: order(main), advanced: order(adv), all: fields };
}

/** Local checks only (P4 contract: the rest is the sidecar's, its error.field lands on the field):
    required fields not empty, integers in range. -> {[fieldId]: 'required' | 'range'} */
export function localProblems(spec, values, opts) {
  const { main, advanced, all } = formFields(spec, values, opts);
  const out = {};
  for (const f of main.concat(advanced)) {
    const v = values ? values[f.id] : undefined;
    const empty = v == null || (typeof v === 'string' && !v.trim());
    if (empty) { if (fieldRequired(f, all, values) && f.type !== 'boolean') out[f.id] = 'required'; continue; }
    if (f.type === 'integer') {
      const n = Number(String(v).trim());
      const c = f.conditions || {};
      if (!/^-?\d+$/.test(String(v).trim()) || (c.min != null && n < c.min) || (c.max != null && n > c.max)) out[f.id] = 'range';
    }
  }
  return out;
}

/** The fields object of import.apply.manual / chain items: only what was entered (integers as
    numbers, booleans as booleans); nothing is filled from a default. */
export function applyFields(spec, values, opts) {
  const { main, advanced } = formFields(spec, values, opts);
  const out = {};
  for (const f of main.concat(advanced)) {
    const v = values ? values[f.id] : undefined;
    if (v == null || v === '') continue;
    if (f.type === 'integer') { const s = String(v).trim(); if (s) out[f.id] = /^-?\d+$/.test(s) ? Number(s) : s; }
    else if (f.type === 'boolean') out[f.id] = v === true || v === 'true';
    else if (typeof v === 'string') { if (v.trim()) out[f.id] = v; }
    else out[f.id] = v;
  }
  return out;
}

/** import.apply failed because the preview plan is no longer valid: preview again once. */
export const PLAN_RETRY = Object.freeze(['plan_expired', 'plan_config_changed', 'import_plan_changed', 'plan_missing']);
export const planRetry = err => { const d = detailError(err); return !!d && PLAN_RETRY.includes(d.reason_code); };

/** import.preview groups (G38, T-P4-C2 #13): valid only when every accepted row is in one group. */
export function previewGroups(res) {
  const g = res && Array.isArray(res.groups) ? res.groups : null;
  if (!g || !g.length || !(res.accepted > 0)) return null;
  const seen = new Set();
  for (const x of g) {
    if (!x || !Array.isArray(x.row_indexes) || !x.row_indexes.length) return null;
    for (const i of x.row_indexes) { if (!Number.isInteger(i) || i < 0 || i >= res.accepted || seen.has(i)) return null; seen.add(i); }
  }
  return seen.size === res.accepted ? g : null;
}

/** Rejected preview entries counted by reason (the same reason twice is one line with a count). */
export function rejectedSummary(rejected) {
  const out = [];
  for (const e of Array.isArray(rejected) ? rejected : []) {
    if (!e || typeof e !== 'object') continue;
    const key = `${e.message_key || ''}\u0000${e.params && e.params.scheme ? e.params.scheme : ''}`;
    const hit = out.find(x => x.key === key);
    if (hit) hit.n++; else out.push({ key, n: 1, error: e });
  }
  return out;
}

/** The first http(s) line of a paste (the "add as a subscription" offer; never sent back by the
    sidecar, read from the page's own input). */
export function firstUrlLine(text) {
  for (const raw of String(text || '').split(/\r?\n/)) {
    const s = raw.trim();
    if (/^https?:\/\/\S+$/i.test(s)) return s;
  }
  return null;
}
