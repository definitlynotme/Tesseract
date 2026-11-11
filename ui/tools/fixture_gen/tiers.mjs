// T-P4-05b: generates ui/test/fixtures/scenarios/tiers_*.json (the tiers page's fixture scenarios).
// node ui/tools/fixture_gen/tiers.mjs --force [<ui dir>] [<frames.json dir>]   (OUTDATED: see below)
// Collected into ui/tools/fixture_gen/ by T-P4-12 (第八波裁定 05a-§11.5); usage and state: ui/README.md.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// OUTDATED (T-P4-12 fix 1): T-P4-H2 hand-edited 11 of the tiers_*.json this script writes (gateway.current
// selected/effective set to null per the runtime invariant, desc text). Running it overwrites
// the current fixtures, so it refuses to write without --force (ui/README.md §6). To compare, run it
// with --force on a COPY of the ui directory: node ui/tools/fixture_gen/tiers.mjs --force <copy of ui> [<frames dir>]
const ARGS = process.argv.slice(2);
if (!ARGS.includes('--force')) {
  console.error('tiers.mjs is outdated and would overwrite test/fixtures/scenarios (T-P4-H2 hand-edited 11 of them); see ui/README.md §6. ' +
    'Refusing to write. Re-run with --force (preferably on a copy of the ui directory) if you really mean it.');
  process.exit(2);
}
const POS = ARGS.filter(a => a !== '--force');
const UI = POS[0] || resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const OUT = join(UI, 'test', 'fixtures', 'scenarios');
const T0 = 1790000000000;
// the render fixture clock's start (test/render/fixture_clock.js): the K-field times are relative to it
const NOW = Date.UTC(2026, 8, 29, 4, 0, 0);
const E1 = 'rt-fixture-e1';
const base = (epoch = E1) => ({ epoch, produced_at_ms: T0, sent_at_ms: T0 + 1 });
const clone = v => JSON.parse(JSON.stringify(v));

// ---------------------------------------------------------------- frames
const evt = (topic, data, o = {}) => {
  const f = { t: 'evt', ...base(o.epoch), topic, rev: o.rev || 1, kind: o.kind || 'snapshot', data };
  if (o.key) f.key = o.key;
  if (o.page) f.page = o.page;
  if (o.base_rev != null) f.base_rev = o.base_rev;
  if (o.seq != null) f.seq = o.seq;
  return f;
};
const COMMANDS = ['service.start', 'service.stop', 'service.restart', 'nodes.diagnose', 'nodes.cancelDiagnose', 'nodes.update', 'nodes.delete',
  'nodes.reveal', 'subscriptions.refresh', 'import.preview', 'import.apply', 'gateway.select', 'settings.apply',
  'config.snapshot', 'nodes.detail', 'import.specs'];
const TOPICS = ['service.state', 'route.published', 'tiers.summary', 'nodes.list', 'subscriptions.list', 'config.items', 'gateway.current',
  'bastion.pool', 'core.info', 'jobs'];
// the tiers page's B items (feature_registry.js): a sidecar declaring them is key 1 of the two-key
// release; key 2 is the build (features.json, or --h-features all). Declared in every tiers scenario,
// so a normal build shows the v1 faces and a --h-features all build the prototype controls.
const TIERS_B = ['B-01', 'B-03', 'B-04', 'B-05', 'B-06', 'B-07', 'B-08', 'B-09', 'B-10', 'B-11', 'B-12', 'B-13', 'B-14', 'B-15', 'B-16', 'B-17', 'B-18', 'B-37'];
const hello = ({ epoch = E1, topics = TOPICS, commands = COMMANDS } = {}) => ({
  t: 'hello', ...base(epoch), proto: 1, app_version: '1.0.0', runtime_pid: 4381, lang: 'zh_CN',
  caps: { commands, topics, features: TIERS_B }, scope: { kind: 'custom', id: 'rt-fixture' },
  network_recovery: { proxy: { state: 'restored', error_code: null, remaining_count: 0 }, firewall: { state: 'not_needed', error_code: null, remaining_count: 0 } },
});
const serviceState = (running, epoch = E1) => evt('service.state', {
  intent: { desired_running: running, stop_requested: false, lifecycle_revision: 12, runtime_generation: 4 },
  runtime: { runtime_running: running, startup_phase: running ? 'running' : 'stopped', shutdown_cleanup_pending: false, failure_code: null },
  core: { running, pid: running ? 7654 : null, process_generation: 4, core_generation: 8, cleanup_pending: false },
  readiness: { state: 'ready', validation_active: false, reason: '', remaining_ms: null, publication_revision: 36, lifecycle_revision: 12, route_epoch: 7, network_context_epoch: 2 },
  freshness: { sampled_at_ms: T0, snapshot_revision: 51, stale: false, uptime_s: running ? 120 : null },
}, { epoch });
const route = (leaf, tier, epoch = E1) => evt('route.published', {
  publication_revision: 20,
  owner: { process_pid: 7654, process_generation: 4, core_generation: 8, semantic_digest: 'a'.repeat(64), route_epoch: 7, network_context_epoch: 2 },
  selected_chain: [tier === 'vanguard' ? 'TIER1' : 'TIER2', leaf], leaf, managed_tier: tier, observed_at_ms: T0, valid: true, invalid_reason: null, fail_closed_confirmed: false,
}, { epoch });
const summary = ({ bastion = true, carrier = 'vanguard', counts = {}, intervals = true, epoch = E1 } = {}) => {
  const c = { vanguard: [4, 2], sentinel: [6, 3], bastion: [5, 3], gateway: [3, 2], ...counts };
  const iv = { vanguard: 30, sentinel: 60, bastion: 120, gateway: 60 };
  const tiers = {};
  for (const t of ['vanguard', 'sentinel', 'bastion', 'gateway']) {
    tiers[t] = { enabled: t === 'bastion' ? bastion : true, total: c[t][0], usable: c[t][1] };
    if (intervals) tiers[t].check_interval_s = iv[t];
  }
  return evt('tiers.summary', { carrier, offline_reason: null, order: [carrier, ...['vanguard', 'sentinel', 'bastion', 'gateway'].filter(x => x !== carrier)],
    degraded: false, fallback: false, tiers, pool: { usable: 3, total: 5 } }, { epoch });
};
const coreInfo = running => evt('core.info', { running, pid: running ? 7654 : null, version: '1.19.27', generation: 8, cleanup_pending: false });
const hostFrames = (epoch = E1) => [{ t: 'host:language', language: 'zh_CN', epoch }, { t: 'host:visibility', visible: true, epoch }];

// ---------------------------------------------------------------- data (ASCII only)
const N = (id, name, tier, kind, status, o = {}) => ({
  id, name, tier, kind, enabled: status !== 'disabled', status, reason_code: o.reason || null, protocol: o.protocol || 'trojan',
  latency_ms: o.lat == null ? null : o.lat, ...(o.extra || {}),
});
const K = o => o; // K-field helper (kept explicit in the data)
const NODES = {
  vanguard: [
    N('v-hk03', 'hk03-vps', 'vanguard', 'manual', 'healthy', { protocol: 'hysteria2', lat: 42, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 17000 }) }),
    N('v-tyo01', 'tyo01-vps', 'vanguard', 'manual', 'healthy', { protocol: 'vless_reality', lat: 388, extra: K({ subscription_id: null, slow: true, last_checked_at_ms: NOW - 25000 }) }),
    N('v-sg02', 'sg02-vps', 'vanguard', 'manual', 'failed', { protocol: 'trojan', reason: 'tls_failed', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 9000 }) }),
    N('v-new01', 'new01-vps', 'vanguard', 'manual', 'pending', { protocol: 'naive', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: null }) }),
    N('v-old', 'old-vps', 'vanguard', 'manual', 'disabled', { protocol: 'trojan', reason: 'disabled', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: null }) }),
  ],
  sentinel: [
    N('s-fra04', 'fra04-sub', 'sentinel', 'subscription', 'healthy', { protocol: 'vmess', lat: 120, extra: K({ subscription_id: 'sub-nebula', slow: false, last_checked_at_ms: NOW - 30000 }) }),
    N('s-sel01', 'sel01-sub', 'sentinel', 'subscription', 'healthy', { protocol: 'trojan', lat: 96, extra: K({ subscription_id: 'sub-nebula', slow: false, last_checked_at_ms: NOW - 30000 }) }),
    N('s-osa03', 'osa03-sub', 'sentinel', 'subscription', 'failed', { protocol: 'shadowsocks', reason: 'connection_timeout', extra: K({ subscription_id: 'sub-nebula', slow: false, last_checked_at_ms: NOW - 30000 }) }),
    N('s-lax02', 'lax02-sub', 'sentinel', 'subscription', 'pending', { protocol: 'vmess', reason: 'subscription_recheck_pending', extra: K({ subscription_id: 'sub-aurora', slow: false, last_checked_at_ms: null }) }),
    N('s-sin01', 'sin01-sub', 'sentinel', 'subscription', 'healthy', { protocol: 'trojan', lat: 210, extra: K({ subscription_id: 'sub-aurora', slow: false, last_checked_at_ms: NOW - 50000 }) }),
    N('s-solo', 'solo-node', 'sentinel', 'manual', 'healthy', { protocol: 'anytls', lat: 160, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 40000 }) }),
  ],
  bastion: [
    N('b-p1', 'pool-a1', 'bastion', 'automatic', 'healthy', { protocol: 'shadowsocks', lat: 140, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 60000 }) }),
    N('b-p2', 'pool-a2', 'bastion', 'automatic', 'healthy', { protocol: 'vmess', lat: 310, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 60000 }) }),
    N('b-p3', 'pool-a3', 'bastion', 'automatic', 'failed', { protocol: 'trojan', reason: 'connection_refused', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 60000 }) }),
    N('b-u1', 'my-emergency', 'bastion', 'manual', 'healthy', { protocol: 'trojan', lat: 220, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 60000 }) }),
    N('b-s1', 'src-node-1', 'bastion', 'subscription', 'healthy', { protocol: 'shadowsocks', lat: 260, extra: K({ subscription_id: 'sub-mysrc', slow: false, last_checked_at_ms: NOW - 60000 }) }),
  ],
  gateway: [
    N('g-osa02', 'osa02-exit', 'gateway', 'manual', 'healthy', { protocol: 'vless_reality', lat: 64, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 20000 }) }),
    N('g-tyo01', 'tyo01-exit', 'gateway', 'manual', 'healthy', { protocol: 'trojan', lat: 81, extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 20000 }) }),
    N('g-sel03', 'sel03-exit', 'gateway', 'manual', 'failed', { protocol: 'trojan', reason: 'dns_resolution_failed', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: NOW - 20000 }) }),
  ],
};
const S = (id, name, tier, o = {}) => ({ id, name, tier, enabled: o.enabled !== false, display: o.display || `${name}.example.com`,
  total: o.total || 0, healthy: o.healthy || 0, visibility: o.visibility || 'current', refreshing: !!o.refreshing, ...(o.extra || {}) });
const SUBS = {
  vanguard: [],
  sentinel: [
    S('sub-nebula', 'nebula', 'sentinel', { total: 3, healthy: 2, extra: { last_refresh_at_ms: NOW - 600000 } }),
    S('sub-aurora', 'aurora', 'sentinel', { total: 2, healthy: 1, visibility: 'pending', extra: { recheck: { done: 1, total: 2 }, last_refresh_at_ms: NOW - 600000 } }),
    S('sub-orion', 'orion', 'sentinel', { enabled: false, total: 4, healthy: 0, visibility: 'unknown', extra: { last_refresh_at_ms: NOW - 600000 } }),
    S('sub-vega', 'vega', 'sentinel', { total: 5, healthy: 4, visibility: 'previous', refreshing: true, extra: { last_refresh_at_ms: NOW - 600000 } }),
    S('sub-lyra', 'lyra', 'sentinel', { total: 0, healthy: 0, visibility: 'unknown', extra: { last_refresh_at_ms: null } }),
  ],
  bastion: [S('sub-mysrc', 'my-source', 'bastion', { total: 1, healthy: 1 })],
  gateway: [S('sub-exits', 'exit-feed', 'gateway', { total: 0, healthy: 0, visibility: 'unknown', extra: { last_refresh_at_ms: null } })],
};
const CI = (id, name, kind, section, enabled = true) => ({ id, name, kind, section, enabled, secret_set: true });
const ITEMS = [
  CI('v-hk03', 'hk03-vps', 'node', 'vanguard'), CI('v-tyo01', 'tyo01-vps', 'node', 'vanguard'), CI('v-sg02', 'sg02-vps', 'node', 'vanguard'),
  CI('v-new01', 'new01-vps', 'node', 'vanguard'), CI('cfg-old', 'old-vps', 'node', 'vanguard', false), CI('cfg-draft2', 'draft-two', 'node', 'vanguard', false),
  CI('s-solo', 'solo-node', 'node', 'sentinel'), CI('b-u1', 'my-emergency', 'node', 'bastion'),
  CI('g-osa02', 'osa02-exit', 'node', 'gateway'), CI('g-tyo01', 'tyo01-exit', 'node', 'gateway'), CI('g-sel03', 'sel03-exit', 'node', 'gateway'),
  CI('cfg-gdraft', 'exit-draft', 'node', 'gateway', false),
  CI('sub-nebula', 'nebula', 'subscription', 'sentinel'), CI('sub-aurora', 'aurora', 'subscription', 'sentinel'), CI('sub-orion', 'orion', 'subscription', 'sentinel', false),
  CI('sub-vega', 'vega', 'subscription', 'sentinel'), CI('sub-lyra', 'lyra', 'subscription', 'sentinel'), CI('sub-mysrc', 'my-source', 'subscription', 'bastion'),
  CI('sub-exits', 'exit-feed', 'subscription', 'gateway'),
];
const listFrames = ({ nodes = NODES, subs = SUBS, items = ITEMS, withSubs = true, epoch = E1, rev = 1 } = {}) => [
  ...['vanguard', 'sentinel', 'bastion', 'gateway'].map(t => evt('nodes.list', { items: nodes[t] || [] }, { key: t, epoch, rev })),
  ...(withSubs ? ['vanguard', 'sentinel', 'bastion', 'gateway'].map(t => evt('subscriptions.list', { items: subs[t] || [] }, { key: t, epoch, rev })) : []),
  evt('config.items', { items }, { epoch, rev }),
];
const gatewayCurrent = (o = {}, epoch = E1) => evt('gateway.current', { selected: 'osa02-exit', effective: 'osa02-exit', missing: false, applied: true, pending: null, ...o }, { epoch });
const pool = (o = {}) => evt('bastion.pool', { runtime_alive: 2, user_alive: 1, capacity: 60, min_alive_nodes: 3, target_alive: 24, collecting: false, observed_at_ms: T0, ...o });

// ---------------------------------------------------------------- answers
let opSeq = 0;
const err = (code, message_key, o = {}) => ({ code, message_key, params: o.params || {}, ...(o.reason ? { reason_code: o.reason } : {}), ...(o.field ? { field: o.field } : {}) });
const res = (cmd, data, o = {}) => ({ t: 'res', ...base(o.epoch), id: 1, req_id: '$req_id', owner: '$owner', cmd, ok: true, data });
const resErr = (cmd, error) => ({ t: 'res', ...base(), id: 1, req_id: '$req_id', owner: '$owner', cmd, ok: false, error });
/** a job: ack, queued, running, then done / failed / cancelled (or nothing: still running). seq is
    the first jobs seq; every job gets its own block of ten in the order of the case. */
function job(name, seq, end, o = {}) {
  const op = `op-${name}-${++opSeq}`;
  const J = (state, x = {}, n) => evt('jobs', { op, req_id: '$req_id', owner: '$owner', name, state, started_at_ms: state === 'queued' ? null : T0, progress: null, result: null, error: null, ...x }, { kind: 'edge', rev: seq + n, seq: seq + n });
  const out = [{ t: 'ack', ...base(), id: 2, req_id: '$req_id', owner: '$owner', accepted: true, op }, J('queued', {}, 0), J('running', {}, 1)];
  if (end === 'done') out.push(J('done', { result: o.result }, 2));
  else if (end === 'failed') out.push(J('failed', { result: o.result == null ? null : o.result, error: o.error }, 2));
  else if (end === 'cancelled') out.push(J('cancelled', {}, 2));
  return out.concat(o.after || []);
}
const refused = (error) => [{ t: 'ack', ...base(), id: 2, req_id: '$req_id', owner: '$owner', accepted: false, error }];
const on = (cmd, args, reply) => ({ on_invoke: args ? { cmd, args } : { cmd }, reply });
const configSnapshot = ({ bastion = true, ceiling = 600 } = {}) => res('config.snapshot', {
  rev: 5, values: { latency: { primary_gateway_max_latency_ms: ceiling }, ...(bastion ? { bastion: { enabled: true, min_alive_nodes: 3 } } : {}) },
  secret_fields_set: [], read_errors: [], pending: null,
});
const detail = (node, o = {}) => res('nodes.detail', {
  node, config_item: o.item === undefined ? CI(node.id, node.name, 'node', node.tier) : o.item, last_diagnostic: o.last || null, check_interval_s: o.interval || 30,
  ...(o.endpoint === false ? {} : { endpoint: o.endpoint || { host_masked: 'h•••.net', port: 443 } }),
  ...(o.protocols ? { protocols: o.protocols } : {}),
});
const find = id => Object.values(NODES).flat().find(n => n.id === id);

// ---------------------------------------------------------------- scenarios
function scenario(name, desc, steps) { return { name, desc, lang: 'zh_CN', steps }; }
function start({ running = true, bastion = true, carrier = 'vanguard', leaf = 'hk03-vps', topics = TOPICS, lists = {}, gw = {}, poolO = {}, intervals = true, extra = [] } = {}) {
  return [
    { deliver: { t: 'rt:reset', epoch: E1, reason: 'reconnect' } },
    { deliver: hello({ topics }) },
    { deliver: serviceState(running) },
    ...(running ? [{ deliver: route(leaf, carrier) }] : []),
    { deliver: summary({ bastion, carrier, intervals }) },
    ...listFrames(lists).map(f => ({ deliver: f })),
    { deliver: gatewayCurrent(running ? gw : { effective: null, applied: false, ...gw }) },
    ...(running ? [{ deliver: pool(poolO) }] : []),
    { deliver: coreInfo(running) },
    ...extra.map(f => ({ deliver: f })),
    ...hostFrames().map(f => ({ deliver: f })),
  ];
}
const cfg = (o, n = 2) => Array.from({ length: n }, () => on('config.snapshot', null, [configSnapshot(o)]));

const out = {};
const SPECS = JSON.parse(readFileSync(join(UI, 'src', 'design', 'catalog.json'), 'utf8')).import;

// 1. running, every tier populated, the K fields present
out.tiers_running = scenario('tiers_running', 'T-P4-05b: running; the four tiers with nodes (current, slow, failed, pending, drafts from nodes.list and config.items), subscriptions in every visibility (current, pending with recheck, previous while refreshing, unknown, disabled), Bastion on with its pool, Gateway current by name; T-P4-C1/C2 fields present (subscription_id, slow, last_checked_at_ms, recheck, last_refresh_at_ms, check_interval_s); config.snapshot carries bastion (K-7).', [
  ...start(),
  ...cfg({ bastion: true }),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail(find('v-hk03'), { protocols: [{ name: 'hysteria2', port: 443, node_id: 'v-hk03' }, { name: 'vless_reality', port: 8443, node_id: 'v-hk03-r' }],
    last: { request_id: 'diag-prev', mode: 'direct', state: 'ok', latency_ms: 44, reason_code: null, status_applied: true, cleanup_pending: false } })]),
  on('nodes.detail', { node_id: 'g-osa02' }, [detail(find('g-osa02'), { endpoint: { host_masked: 'o•••.net', port: 443 } })]),
  on('nodes.detail', { node_id: 's-fra04' }, [detail(find('s-fra04'), { item: null, endpoint: false })]),
  on('nodes.detail', { node_id: 'b-p1' }, [detail(find('b-p1'), { item: null, endpoint: false })]),
  on('nodes.detail', { node_id: 'v-old' }, [detail(find('v-old'), { item: CI('cfg-old', 'old-vps', 'node', 'vanguard', false) })]),
  on('import.specs', null, [res('import.specs', SPECS)]),
]);

// 2. stopped: the last results, Gateway's saved choice
out.tiers_stopped = scenario('tiers_stopped', 'T-P4-05b: service stopped; the lists show the last results (last-known status shapes), gateway.current carries the saved choice only (effective null), no bastion.pool baseline.', [
  ...start({ running: false }),
  ...cfg({ bastion: true }),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail(find('v-hk03'))]),
]);

// 3. empty tiers (G34)
const EMPTY = { vanguard: [], sentinel: [], bastion: [], gateway: [] };
out.tiers_empty = scenario('tiers_empty', 'T-P4-05b G34: running, every tier empty (no nodes, subscriptions or drafts), Bastion off, no Gateway selection: each tier says what to do and offers the import.', [
  ...start({ bastion: false, lists: { nodes: EMPTY, subs: EMPTY, items: [] }, gw: { selected: null, effective: null, applied: false }, poolO: { runtime_alive: 0, user_alive: 0, target_alive: 0 },
    extra: [] }).map(s => (s.deliver && s.deliver.topic === 'tiers.summary' ? { deliver: summary({ bastion: false, counts: { vanguard: [0, 0], sentinel: [0, 0], bastion: [0, 0], gateway: [0, 0] } }) } : s)),
  ...cfg({ bastion: true }),
]);

// 4. Bastion off, your own sources and nodes still shown (G39); K-7 absent (config.snapshot without bastion)
out.tiers_bastion_off = scenario('tiers_bastion_off', 'T-P4-05b G39 + K-7 absent: Bastion switched off (tiers.summary), its user nodes and user subscription still listed under the explanation card; config.snapshot has no bastion section, so the minimum is read-only and no switch is offered.', [
  ...start({ bastion: false, poolO: { runtime_alive: 0, target_alive: 0, collecting: false } }),
  ...cfg({ bastion: false }),
]);

// 5. subscriptions.list not published, Node without subscription_id (K-4 absent, section 4.3)
const noK = Object.fromEntries(Object.entries(NODES).map(([t, list]) => [t, list.map(n => { const c = clone(n); delete c.subscription_id; delete c.slow; delete c.last_checked_at_ms; return c; })]));
out.tiers_degraded = scenario('tiers_degraded', 'T-P4-05b section 4.3: hello.caps.topics without subscriptions.list (the P2-14 getter not delivered): subscription rows come from config.items (name, enabled; health unknown); the nodes have no subscription_id / slow / last_checked_at_ms (frozen sidecar): no drill-in, no search by subscription name, no slow word, no check time; tiers.summary without check_interval_s.', [
  ...start({ topics: TOPICS.filter(t => t !== 'subscriptions.list'), lists: { nodes: noK, withSubs: false }, intervals: false }),
  ...cfg({ bastion: true }),
  on('subscriptions.refresh', { item_id: 'sub-nebula' }, job('subscriptions.refresh', 10, 'done', { result: { total: 3, healthy: 2, stored: true, applied: true, retained_previous: true, apply_error: null, error: null } })),
]);

// 6. paging (3 pages) and patches (one merges, one with a base_rev mismatch)
const pv = NODES.vanguard;
const pg = (i, items, complete) => ({ snapshot_id: 'snap-v1', index: i, complete, next_cursor: complete ? null : `cur-${i + 1}` });
const pagedSteps = start().filter(s => !(s.deliver && s.deliver.topic === 'nodes.list' && s.deliver.key === 'vanguard'));
const at = pagedSteps.findIndex(s => s.deliver && s.deliver.topic === 'nodes.list');
pagedSteps.splice(at, 0,
  { deliver: evt('nodes.list', { items: pv.slice(0, 2) }, { key: 'vanguard', page: pg(0, null, false) }) },
  { deliver: evt('nodes.list', { items: pv.slice(2, 4) }, { key: 'vanguard', page: pg(1, null, false) }) },
  { deliver: evt('nodes.list', { items: pv.slice(4) }, { key: 'vanguard', page: pg(2, null, true) }) },
  // a patch on rev 1: hk03 gets a new latency
  { deliver: evt('nodes.list', { changed: [{ ...pv[0], latency_ms: 51 }], removed: [] }, { key: 'vanguard', kind: 'patch', rev: 2, base_rev: 1 }) },
);
out.tiers_paging = scenario('tiers_paging', 'T-P4-05b section 12.3: nodes.list vanguard arrives in three pages (installed once complete) and a patch that merges (rev 1 -> 2); the case then delivers a patch whose base_rev does not match: the list keeps what it shows and says it is not up to date (no last-wins).', [
  ...pagedSteps,
  ...cfg({ bastion: true }),
]);

// 7. Gateway: the configured exit is missing (G37); selecting fails with needs_route; turning off
out.tiers_gateway_missing = scenario('tiers_gateway_missing', 'T-P4-05b G37: gateway.current.missing = true (the configured exit gone-exit is not among the candidates): the missing row with its two ways out; gateway.select of tyo01-exit fails with needs_route; turning Gateway off (proxy_name "") is done with selection null.', [
  ...start({ gw: { selected: 'gone-exit', effective: null, missing: true, applied: false } }),
  ...cfg({ bastion: true }),
  on('gateway.select', { node_id: 'g-tyo01' }, job('gateway.select', 10, 'failed', { error: err('needs_route', 'errors.needs_route') })),
  on('gateway.select', { proxy_name: '' }, job('gateway.select', 20, 'done', { result: { selected: '', applied: true, apply_error: null, selection: null, pending: null },
    after: [gatewayCurrent({ selected: null, effective: null, missing: false, applied: false }, E1)].map(f => ({ ...f, rev: 2 })) })),
]);

// 8. diagnoses: success (latency from nodes.detail afterwards), failure, cancel, panel closed while running
const TYO = job('nodes.diagnose', 30, null);
const DIAG_OK = { request_id: 'diag-ok', status_applied: false, reason_code: null, cleanup_pending: false };
out.tiers_diagnose = scenario('tiers_diagnose', 'T-P4-05b section 8.3: nodes.diagnose of hk03-vps is done (status_applied false) and nodes.detail then gives its latency; sg02-vps fails with tls_failed (cleanup_pending true); tyo01-vps keeps running until its cancel (nodes.cancelDiagnose requested, cleanup pending) ends it as cancelled; new01-vps keeps running and the panel is closed: the owner revoke sends nodes.cancelDiagnose{request_id}. With B-12 on (--h-features all) the pool node pool-a1 is refused with tier3_auto_pool_self_check.', [
  ...start(),
  ...cfg({ bastion: true }),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail(find('v-hk03'))]),
  on('nodes.diagnose', { node_id: 'v-hk03' }, job('nodes.diagnose', 10, 'done', { result: DIAG_OK })),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail({ ...find('v-hk03'), latency_ms: 47 }, { last: { request_id: 'diag-ok', mode: 'direct', state: 'ok', latency_ms: 47.4, reason_code: null, status_applied: false, cleanup_pending: true } })]),
  on('nodes.detail', { node_id: 'v-sg02' }, [detail(find('v-sg02'))]),
  on('nodes.diagnose', { node_id: 'v-sg02' }, job('nodes.diagnose', 20, 'failed', { result: { request_id: 'diag-bad', status_applied: true, reason_code: 'tls_failed', cleanup_pending: true },
    error: err('validation', 'errors.diagnose.probe_failed', { reason: 'tls_failed' }) })),
  on('nodes.detail', { node_id: 'v-tyo01' }, [detail(find('v-tyo01'))]),
  on('nodes.diagnose', { node_id: 'v-tyo01' }, TYO),
  on('nodes.cancelDiagnose', null, [res('nodes.cancelDiagnose', { requested: true, cleanup_pending: true }),
    evt('jobs', { op: TYO[0].op, req_id: '$req_id', owner: '$owner', name: 'nodes.diagnose', state: 'cancelled', started_at_ms: T0, progress: null, result: null, error: null }, { kind: 'edge', rev: 33, seq: 33 })]),
  on('nodes.detail', { node_id: 'v-new01' }, [detail(find('v-new01'))]),
  on('nodes.diagnose', { node_id: 'v-new01' }, job('nodes.diagnose', 40, null)),
  on('nodes.cancelDiagnose', null, [res('nodes.cancelDiagnose', { requested: true, cleanup_pending: false })]),
  /* B-12 builds only (--h-features all): a pool node's diagnosis is refused after resolution
     (bridge_diagnostics AUTO_POOL_REASON: not_allowed, reason tier3_auto_pool_self_check) */
  on('nodes.detail', { node_id: 'b-p1' }, [detail(find('b-p1'))]),
  on('nodes.diagnose', { node_id: 'b-p1' }, job('nodes.diagnose', 50, 'failed', { result: { request_id: 'diag-pool', status_applied: false, reason_code: 'tier3_auto_pool_self_check', cleanup_pending: false },
    error: err('not_allowed', 'errors.diagnose.tier3_auto_pool_self_check', { reason: 'tier3_auto_pool_self_check' }) })),
]);

// 9. writes: nodes.update rolled back, saved but not applied; nodes.delete saved not applied; reveal
const SECRET = 'hysteria2://FIXTURE-SECRET-7f3a@hk03.vps.example.net:443';
out.tiers_writes = scenario('tiers_writes', 'T-P4-05b sections 8.4-8.7: edit hk03-vps (nodes.update fails rolled_back, then saves with applied false: saved=true/applied=false), switch tyo01-vps to draft (G18 warning first), delete sg02-vps (saved, not applied), re-enable the draft old-vps by its ConfigItem id (K-11 / T-P4-C1 item 8), reveal the share link of hk03-vps (nodes.reveal private, memory only).', [
  ...start(),
  ...cfg({ bastion: true }),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail(find('v-hk03'))]),
  on('nodes.reveal', { item_id: 'v-hk03', fields: ['uri'] }, [{ ...res('nodes.reveal', { item_id: 'v-hk03', fields: { uri: SECRET } }) }]),
  on('nodes.update', { item_id: 'v-hk03' }, job('nodes.update', 10, 'failed', { result: { saved: false, applied: false, apply_error: err('io', 'errors.nodes.rolled_back', { reason: 'rolled_back' }), pending: null },
    error: err('io', 'errors.nodes.rolled_back', { reason: 'rolled_back' }) })),
  on('nodes.update', { item_id: 'v-hk03' }, job('nodes.update', 20, 'failed', { result: { saved: true, applied: false, apply_error: err('io', 'errors.nodes.runtime_reload_failed', { reason: 'runtime_reload_failed' }), pending: null },
    error: err('io', 'errors.nodes.runtime_reload_failed', { reason: 'runtime_reload_failed' }) })),
  on('nodes.detail', { node_id: 'v-tyo01' }, [detail(find('v-tyo01'))]),
  on('nodes.update', { item_id: 'v-tyo01', updates: { enabled: false } }, job('nodes.update', 30, 'done', { result: { saved: true, applied: true, apply_error: null, pending: null } })),
  on('nodes.detail', { node_id: 'v-sg02' }, [detail(find('v-sg02'))]),
  on('nodes.delete', { item_id: 'v-sg02' }, job('nodes.delete', 40, 'failed', { result: { saved: true, applied: false, apply_error: err('io', 'errors.nodes.runtime_reload_failed'), pending: null }, error: err('io', 'errors.nodes.runtime_reload_failed') })),
  on('nodes.detail', { node_id: 'v-old' }, [detail(find('v-old'), { item: CI('cfg-old', 'old-vps', 'node', 'vanguard', false) })]),
  on('nodes.update', { item_id: 'cfg-old', updates: { enabled: true } }, job('nodes.update', 50, 'done', { result: { saved: true, applied: true, apply_error: null, pending: null } })),
]);

// 10. subscriptions.refresh: the four results (G35) and the refusals
const RR = (o) => ({ total: 3, healthy: 2, stored: true, applied: true, retained_previous: true, apply_error: null, error: null, ...o });
out.tiers_refresh = scenario('tiers_refresh', 'T-P4-05b G35: subscriptions.refresh of nebula is stored and applied; aurora fails and keeps the previous nodes (retained_previous); lyra fails without a previous cache (a new subscription); exit-feed is stored but not applied; vega is refused (conflict, in progress); my-source is refused with lifecycle_busy.', [
  ...start(),
  ...cfg({ bastion: true }),
  on('subscriptions.refresh', { item_id: 'sub-nebula' }, job('subscriptions.refresh', 10, 'done', { result: RR() })),
  on('subscriptions.refresh', { item_id: 'sub-aurora' }, job('subscriptions.refresh', 20, 'failed', { result: RR({ stored: false, applied: false, error: err('io', 'errors.subscriptions.refresh_failed') }), error: err('io', 'errors.subscriptions.refresh_failed') })),
  on('subscriptions.refresh', { item_id: 'sub-lyra' }, job('subscriptions.refresh', 30, 'failed', { result: RR({ total: 0, healthy: 0, stored: false, applied: false, retained_previous: false, error: err('io', 'errors.subscriptions.empty_subscription') }), error: err('io', 'errors.subscriptions.empty_subscription') })),
  on('subscriptions.refresh', { item_id: 'sub-vega' }, refused(err('conflict', 'errors.subscriptions.refresh_in_progress', { reason: 'refresh_in_progress' }))),
  on('subscriptions.refresh', { item_id: 'sub-exits' }, job('subscriptions.refresh', 40, 'failed', { result: RR({ applied: false, apply_error: err('io', 'errors.subscriptions.core_reload_failed') }), error: err('io', 'errors.subscriptions.core_reload_failed') })),
  on('subscriptions.refresh', { item_id: 'sub-mysrc' }, refused(err('lifecycle_busy', 'errors.subscriptions.lifecycle_busy'))),
]);

// 11. import: preview with groups, plan expired once (preview again), apply done, new rows by difference
const NEWN = N('v-imp1', 'imported-one', 'vanguard', 'manual', 'pending', { protocol: 'hysteria2', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: null }) });
const NEWN2 = N('v-imp2', 'imported-two', 'vanguard', 'manual', 'pending', { protocol: 'vless_reality', extra: K({ subscription_id: null, slow: false, last_checked_at_ms: null }) });
const PREVIEW_G = { plan_id: 'plan-1', expires_at_ms: T0 + 600000, accepted: 3, rejected: [], groups: [{ key: 'g0', label: 'hk-box', row_indexes: [0, 1] }, { key: 'g1', row_indexes: [2] }] };
out.tiers_import = scenario('tiers_import', 'T-P4-05b section 10: import.preview (vanguard) answers 3 accepted nodes in two physical-node groups (G38); import.apply fails with plan_expired, the page previews again once (plan-2) and imports; the reply carries the next nodes.list snapshot, whose new rows (by difference: items are batch tokens) glow once.', [
  ...start(),
  ...cfg({ bastion: true }),
  on('import.preview', { tier: 'vanguard' }, [res('import.preview', PREVIEW_G)]),
  on('import.apply', { plan_id: 'plan-1' }, job('import.apply', 10, 'failed', { error: err('conflict', 'errors.import.plan_expired', { reason: 'plan_expired' }) })),
  on('import.preview', { tier: 'vanguard' }, [res('import.preview', { ...PREVIEW_G, plan_id: 'plan-2' })]),
  on('import.apply', { plan_id: 'plan-2' }, job('import.apply', 20, 'done', { result: { saved: true, applied: true, items: ['batch-1', 'batch-2'], subscriptions: [], apply_error: null },
    after: [evt('nodes.list', { items: [...NODES.vanguard, NEWN, NEWN2] }, { key: 'vanguard', rev: 2 })] })),
]);

// 12. import refusals: rejected reasons without groups, a subscription address pasted (Sentinel),
//     manual form from import.specs, subscription saved but its first fetch failed
out.tiers_import_errors = scenario('tiers_import_errors', 'T-P4-05b sections 10.2-10.4, 12.3: Sentinel import.preview rejects the paste (subscription_url_not_node twice, unsupported_uri_scheme with its scheme; no groups); the manual form comes from import.specs (G19) and import.apply answers duplicate_node on the server field; the subscription tab saves the subscription but its first fetch fails (saved=true, job failed: added, first fetch failed).', [
  ...start(),
  ...cfg({ bastion: true }),
  on('import.preview', { tier: 'sentinel' }, [res('import.preview', { plan_id: 'plan-s', expires_at_ms: T0 + 600000, accepted: 1, rejected: [
    err('validation', 'errors.import.subscription_url_not_node', { reason: 'subscription_url_not_node' }),
    err('validation', 'errors.import.subscription_url_not_node', { reason: 'subscription_url_not_node' }),
    err('validation', 'errors.import.unsupported_uri_scheme', { reason: 'unsupported_uri_scheme', params: { scheme: 'xyz' } }),
  ] })]),
  on('import.specs', null, [res('import.specs', SPECS)]),
  on('import.apply', { tier: 'sentinel', mode: 'manual' }, job('import.apply', 10, 'failed', { error: err('conflict', 'errors.import.duplicate_node', { reason: 'duplicate_node', field: 'server' }) })),
  on('import.apply', { tier: 'sentinel', mode: 'subscriptions' }, job('import.apply', 20, 'failed', {
    result: { saved: true, applied: false, items: ['batch-s'], subscriptions: [RR({ total: 0, healthy: 0, stored: false, applied: false, retained_previous: false, error: err('io', 'errors.subscriptions.refresh_failed') })], apply_error: null },
    error: err('io', 'errors.import.initial_refresh_failed', { reason: 'initial_refresh_failed' }) })),
]);

// 13. a new runtime generation while the side panel is open (rt:reset runtime_gone + new epoch), and
//     a same-epoch reconnect (criticism B9): the case delivers the frames
out.tiers_reset = scenario('tiers_reset', 'T-P4-05b criticism B9: running with nodes.detail for hk03-vps (twice) and its diagnosis left running; the case opens the panel and then delivers either a same-epoch reconnect (rt:reset reconnect + the same hello: the panel stays) or a new generation (rt:reset runtime_gone + hello of epoch e2: the panel closes, no cancel goes to the new sidecar).', [
  ...start(),
  ...cfg({ bastion: true }, 4),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail(find('v-hk03'))]),
  on('nodes.detail', { node_id: 'v-hk03' }, [detail(find('v-hk03'))]),
  on('nodes.diagnose', { node_id: 'v-hk03' }, job('nodes.diagnose', 10, null)),
]);

for (const [name, sc] of Object.entries(out)) writeFileSync(join(OUT, `${name}.json`), JSON.stringify(sc, null, 1) + '\n');
console.log(Object.keys(out).join(' '));
// helpers the render case reuses (frames delivered by actions)
writeFileSync(join(POS[1] || '.', 'frames.json'), JSON.stringify({
  e2: [{ t: 'rt:reset', epoch: 'rt-fixture-e2', reason: 'runtime_gone' }, hello({ epoch: 'rt-fixture-e2' }), serviceState(true, 'rt-fixture-e2'), route('hk03-vps', 'vanguard', 'rt-fixture-e2'), summary({ epoch: 'rt-fixture-e2' }), ...listFrames({ epoch: 'rt-fixture-e2' }), gatewayCurrent({}, 'rt-fixture-e2')],
  same: [{ t: 'rt:reset', epoch: E1, reason: 'reconnect' }, hello()],
  mismatch: evt('nodes.list', { changed: [{ ...pv[1], latency_ms: 99 }], removed: [] }, { key: 'vanguard', kind: 'patch', rev: 4, base_rev: 3 }),
}, null, 1));
