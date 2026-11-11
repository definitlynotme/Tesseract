/* ==========================================================================
   pages/settings/changes.js — the settings page's draft keys ⇄ the v1 contract (T-P4-05d)
   One place for every conversion between what the page edits (its draft keys, mapping_settings
   §1.1) and what the sidecar speaks (PROTOCOL §11 config.snapshot ConfigValues, §10/§16
   settings.apply ConfigChange[] / SaveResult, §12 SecretEdit):
     fromSnapshot(values, secretFields) → { values, known, hidden }   the base of every row
     toChanges({ draft, base, keys })   → { changes, sent, skipped }  one ConfigChange per section
     outcome({ result, error }, { run }) → the save bar's result and each row's label
     fieldKey(field)                    → the draft key an Error.field points at (a hold's row)
     rule literals (suffix:example.com …) ⇄ backend rule strings (clash_formatter._targeted_rule)
   Pure: no DOM, no T, no imports (the unit test loads it as a data: module). Values that the page
   never edits are never sent: only the fields of the changed keys go into a change, and a list
   withheld whole by the projection (secret_fields_set holds its field id) is only replaced when
   the user edits it (PROTOCOL §16, ruling 5th wave C1-#5).
   ========================================================================== */

/* ---- small helpers ---------------------------------------------------------------- */
export const clone = v => (v == null || typeof v !== 'object' ? v : JSON.parse(JSON.stringify(v)));
const obj = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : null);
const isBool = v => typeof v === 'boolean';
const isInt = v => Number.isInteger(v) && v >= 0;
const strList = v => (Array.isArray(v) && v.every(x => typeof x === 'string') ? v.slice() : null);
export const sig = v => JSON.stringify(v, (k, x) => (k === 'id' ? undefined : x));
export const same = (a, b) => sig(a) === sig(b);

/* ---- the draft keys: section, estimated effect (only a preview: SectionResult.effect decides) ---- */
export const SECTION = Object.freeze({
  ks: 'killswitch', ksLan: 'killswitch', ksDns: 'killswitch', ksDnsList: 'killswitch', ksExtra: 'killswitch',
  autoExit: 'ui', lang: 'ui', tray: 'ui', autostart: 'autostart',
  // 配置整理 P2: the rules as one ordered list and the rule sets they may name
  rules: 'routing', ruleSets: 'routing',
  bypass: 'network', lan: 'network', ipv6: 'network', portMixed: 'network', portSub: 'network', egress: 'network', egressRetry: 'network',
  dnsMode: 'dns', dnsRules: 'dns', dnsList: 'dns',
  latency: 'latency', bastionOn: 'bastion', bastionMin: 'bastion', sources: 'sources',
  // 提醒 (10-30 redesign): alerts.notify {enabled, events, sound}
  nOn: 'alerts', nConn: 'alerts', nTier: 'alerts', nUpd: 'alerts', nSound: 'alerts',
});
/* the event switches → alerts.notify.events names (alert_policy.NOTICE_GROUPS order) */
export const NOTICE_KEYS = Object.freeze([['nConn', 'connection'], ['nTier', 'tier_fallback'], ['nUpd', 'update']]);
export const KEYS = Object.freeze(Object.keys(SECTION));
/* ConfigChange.section's closed set (PROTOCOL §16) */
export const SECTIONS = new Set(['network', 'dns', 'routing', 'latency', 'sources', 'alerts', 'ui', 'autostart', 'traffic', 'killswitch', 'bastion']);
/** sent ({section: [keys]}) → {key: section} */
export const sectionsOf = sent => { const m = {}; for (const [s, ks] of Object.entries(sent || {})) for (const k of ks) m[k] = s; return m; };
/* estimated effect per key (mapping_settings §1.1 "原型 eff"; R-13: a preview, worded as such) */
const EFFECT = {
  ks: 'instant', ksLan: 'instant', ksDns: 'instant', ksDnsList: 'instant', ksExtra: 'instant', autoExit: 'instant',
  lang: 'instant', tray: 'instant', autostart: 'instant',
  rules: 'reload', ruleSets: 'reload', bypass: 'instant',
  lan: 'restart', ipv6: 'restart', portMixed: 'restart', portSub: 'restart', egress: 'instant', egressRetry: 'instant',
  dnsMode: 'reload', dnsRules: 'reload', latency: 'recheck', bastionOn: 'instant', bastionMin: 'instant', sources: 'instant',
  nOn: 'instant', nConn: 'instant', nTier: 'instant', nUpd: 'instant', nSound: 'instant',
};
export const RANK = Object.freeze({ instant: 0, recheck: 1, reload: 2, restart: 3 });
/** the estimated effect of one key (the resolver list reloads only in the custom DNS mode) */
export function effectOf(key, draft) {
  if (key === 'dnsList') return draft && draft.dnsMode === 'custom' ? 'reload' : 'instant';
  return EFFECT[key] || 'instant';
}
/** the heaviest estimated effect of a set of keys */
export function heaviest(keys, draft) {
  let best = 'instant';
  for (const k of keys) { const e = effectOf(k, draft); if (RANK[e] > RANK[best]) best = e; }
  return best;
}

/* ---- list items: counted one by one (two rules added = two changes) ----------------- */
export const itemKey = x => (x && typeof x === 'object' ? x.id : x);
export function listDiff(a, b) {
  const bm = new Map(b.map(x => [itemKey(x), x])), am = new Map(a.map(x => [itemKey(x), x]));
  const add = a.filter(x => !bm.has(itemKey(x)));
  const rem = b.filter(x => !am.has(itemKey(x))).filter(r => { const i = add.findIndex(x => sig(x) === sig(r)); if (i < 0) return true; add.splice(i, 1); return false; });
  const edit = a.filter(x => bm.has(itemKey(x)) && !same(x, bm.get(itemKey(x))));
  return { add, rem, edit };
}
/** the number of changes one draft key holds (a list counts its items; a reorder alone is one) */
export function countOf(key, draft, base) {
  const d = draft[key], b = base[key];
  if (!Array.isArray(d) || !Array.isArray(b)) return 1;
  const x = listDiff(d, b);
  return x.add.length + x.rem.length + x.edit.length || 1;
}
/** the save bar's count: what T.ui.saveBar.dirtyCount() reports while the bar is dirty */
export const countChanges = (keys, draft, base) => keys.reduce((n, k) => n + countOf(k, draft, base), 0);

/* ---- an older projection's rules (no routing.rule_list) -------------------------------
   Before 配置整理 P2 the backend kept strings in routing.rules.<direct|proxy|reject> and compiled
   them in clash_formatter._targeted_rule (<kind>:<value>, ip-cidr6:, the Clash comma form
   DOMAIN-SUFFIX,x, bare IPs / domains). rulesFromLegacy reads them into the one list: a string
   the page can show as a condition becomes one, anything else stays as stored (raw). */
const PREFIXES = ['domain', 'suffix', 'keyword', 'geosite', 'geoip', 'ip-cidr', 'ip-cidr6', 'process'];
const COMMA = { DOMAIN: 'domain', 'DOMAIN-SUFFIX': 'suffix', 'DOMAIN-KEYWORD': 'keyword', GEOSITE: 'geosite', GEOIP: 'geoip', 'IP-CIDR': 'ip-cidr', 'IP-CIDR6': 'ip-cidr', 'PROCESS-NAME': 'process' };
const V4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
function isV6(s) {
  s = String(s).replace(/^\[|\]$/g, '');
  if (!/^[0-9a-f:]+$/i.test(s) || s.indexOf(':') < 0) return false;
  const dbl = s.split('::'); if (dbl.length > 2) return false;
  const segs = s.split(':').filter(x => x !== '');
  if (segs.some(x => x.length > 4)) return false;
  return dbl.length === 2 ? segs.length <= 7 : segs.length === 8;
}
const ipOrCidr = s => { const [ip, bits] = String(s).split('/'); return (V4.test(ip) || isV6(ip)) && (bits == null || /^\d{1,3}$/.test(bits)); };
const isDomain = s => /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i.test(s);

/** a backend rule string → { v: the page literal, raw: true when it is kept as written } */
export function ruleFromBackend(s) {
  const text = String(s == null ? '' : s).trim();
  const lower = text.toLowerCase();
  for (const p of PREFIXES) {
    if (lower.startsWith(p + ':')) {
      const body = text.slice(p.length + 1).trim();
      if (!body) break;
      return { v: `${p === 'ip-cidr6' ? 'ip-cidr' : p}:${body}`, raw: false };
    }
  }
  if (text.includes(',')) {
    const parts = text.split(',').map(x => x.trim()).filter(Boolean);
    const kind = COMMA[(parts[0] || '').toUpperCase()];
    const tail = parts.slice(2);
    // TYPE,value[,no-resolve]: no policy of its own (the bucket gives it); anything else stays raw
    if (kind && parts.length >= 2 && (tail.length === 0 || (tail.length === 1 && tail[0].toLowerCase() === 'no-resolve' && (kind === 'geoip' || kind === 'ip-cidr')))) {
      return { v: `${kind}:${parts[1]}`, raw: false };
    }
    return { v: text, raw: true };
  }
  if (ipOrCidr(text)) return { v: `ip-cidr:${text}`, raw: false };
  const host = text.replace(/^[+*]\./, '');
  if (isDomain(host)) return { v: `suffix:${host.toLowerCase()}`, raw: false };
  return { v: text, raw: true };
}

/* page action ⇄ backend bucket */
export const BUCKET = Object.freeze({ direct: 'direct', proxy: 'proxy', block: 'reject' });
const ACT_OF = { direct: 'direct', proxy: 'proxy', reject: 'block' };
/** routing.rule_order (which action's list matched first; config_utils.normalize_routing_rule_order)
    → the page's action order, or null unless it is a full permutation */
export function orderFromBackend(v) {
  if (!Array.isArray(v) || v.length !== 3) return null;
  const acts = v.map(b => ACT_OF[b]);
  return acts.every(Boolean) && new Set(acts).size === 3 ? acts : null;
}

/* ---- rules that can never match: an earlier rule of another action already covers them ----
   Only what the literals prove: a suffix covers itself and its sub-domains, a keyword every host
   name holding it, an IP network every network inside it; the same literal covers itself. A
   geosite: / geoip: category's members are not known here: it covers nothing. */
const lit = v => { const m = String(v == null ? '' : v).match(/^([a-z][a-z0-9-]*):(.+)$/i); return m ? { k: m[1].toLowerCase(), x: m[2].trim().toLowerCase() } : null; };
function ipNum(ip) {
  if (V4.test(ip)) return { bits: 32, n: ip.split('.').reduce((a, x) => (a << 8n) + BigInt(+x), 0n) };
  if (!isV6(ip)) return null;
  const s = ip.replace(/^\[|\]$/g, ''), [h, tl] = s.split('::');
  const a = h ? h.split(':') : [], b = tl == null ? null : (tl ? tl.split(':') : []);
  const parts = b == null ? a : a.concat(Array(8 - a.length - b.length).fill('0'), b);
  return { bits: 128, n: parts.reduce((x, p) => (x << 16n) + BigInt(parseInt(p || '0', 16)), 0n) };
}
function net(s) {
  const [ip, len] = String(s).split('/'), a = ipNum(ip);
  if (!a) return null;
  const pre = len == null ? a.bits : +len;
  return Number.isInteger(pre) && pre >= 0 && pre <= a.bits ? Object.assign(a, { pre }) : null;
}
/* the local and LAN networks Tesseract always sends direct, before the user's rules
   (routing_rules.LOCAL_NETWORKS) */
const LOCAL_NETS = ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10'].map(net);
/** an IP or network that lies wholly inside one of them */
export function localNet(s) {
  const i = net(s);
  return !!i && LOCAL_NETS.some(o => {
    if (o.bits !== i.bits || i.pre < o.pre) return false;
    const sh = BigInt(o.bits - o.pre);
    return (o.n >> sh) === (i.n >> sh);
  });
}
/** does rule literal a (earlier) match everything rule literal b (later) matches? */
export function covers(a, b) {
  const p = lit(a), q = lit(b);
  if (!p || !q) return false;
  if (p.k === q.k && p.x === q.x) return true;
  if (p.k === 'suffix' && (q.k === 'domain' || q.k === 'suffix')) return q.x === p.x || q.x.endsWith('.' + p.x);
  if (p.k === 'keyword' && (q.k === 'domain' || q.k === 'suffix' || q.k === 'keyword')) return q.x.includes(p.x);
  if (p.k === 'ip-cidr' && q.k === 'ip-cidr') {
    const o = net(p.x), i = net(q.x);
    if (!o || !i || o.bits !== i.bits || i.pre < o.pre) return false;
    const sh = BigInt(o.bits - o.pre);
    return (o.n >> sh) === (i.n >> sh);
  }
  return false;
}
/** rule id → the earlier rule of another action that covers it, in the one list (配置整理 P2:
    the list is the match order). A rule of the same action behind its own cover changes nothing
    and is not listed; off rows, combinations, negated and stored (raw) rows prove nothing. */
const plain = r => r && !r.off && !r.raw && !r.op && !r.not;
export function shadowed(list) {
  const seq = (list || []).filter(r => !r.off), out = new Map();
  seq.forEach((r, i) => {
    if (!plain(r)) return;
    for (let j = 0; j < i; j++) {
      const e = seq[j];
      if (plain(e) && e.act !== r.act && covers(e.v, r.v)) { out.set(r.id, e); break; }
    }
  });
  return out;
}

/* ---- routing.rules.raw / rule_sets of an older projection (rulesFromLegacy reads them) ----
   As clash_formatter compiled them after the three lists: a raw rule is used when it holds a comma
   and names no policy this list may not use (DIRECT, COMPATIBLE: public direct traffic comes only
   from the direct list; the retired AI-FIXED-IP); a rule set string is <provider>,<policy> or
   RULE-SET,<provider>,<policy>[,…], a mapping {provider|name, target|policy} with the targets
   direct / proxy / reject as the lists. */
const BARRED = new Set(['DIRECT', 'COMPATIBLE', 'AI-FIXED-IP', 'AI_FIXED_IP']);
const SET_TARGET = { direct: 'DIRECT', proxy: 'GENERAL', reject: 'REJECT' };
function policyOf(rule) {
  const parts = String(rule).split(',').map(x => x.trim().toUpperCase()).filter(Boolean);
  if (parts.length < 2) return null;
  const i = parts[0] === 'MATCH' ? 1 : ['AND', 'OR', 'NOT', 'SUB-RULE'].includes(parts[0]) ? parts.length - 1 : 2;
  return parts[i] || null;
}
/** values: ConfigValues; secretFields: secret_fields_set → { rows: [{ id, kind: 'raw' | 'set', text,
    dropped, policy }], hidden: bool (a list withheld whole), known: bool (the snapshot carries
    routing.rules) } */
export function advancedRules(values, secretFields) {
  const rules = obj(obj(obj(values) && values.routing) && values.routing.rules);
  const sf = new Set(Array.isArray(secretFields) ? secretFields : []);
  const out = { rows: [], hidden: sf.has('routing.rules.raw') || sf.has('routing.rules.rule_sets'), known: !!rules };
  if (!rules) return out;
  // (as _as_string_list: a list, or one string of lines)
  (Array.isArray(rules.raw) ? rules.raw : typeof rules.raw === 'string' ? rules.raw.split(/\r?\n/) : []).forEach((s, i) => {
    const text = String(s == null ? '' : s).trim();
    if (!text) return;
    const policy = policyOf(text);
    out.rows.push({ id: `raw-${i}`, kind: 'raw', text, policy, dropped: !text.includes(',') || BARRED.has(policy) });
  });
  (Array.isArray(rules.rule_sets) ? rules.rule_sets : []).forEach((e, i) => {
    let text = null;
    if (typeof e === 'string') {
      const parts = e.split(',').map(x => x.trim()).filter(Boolean);
      if (parts.length === 2) text = `RULE-SET,${parts[0]},${parts[1]}`;
      else if (parts.length >= 3 && parts[0].toUpperCase() === 'RULE-SET') text = parts.join(',');
      else if (e.trim()) { out.rows.push({ id: `set-${i}`, kind: 'set', text: e.trim(), policy: null, dropped: true }); return; }
    } else if (obj(e)) {
      const name = String(e.provider || e.name || '').trim(), tk = String(e.target || e.policy || 'proxy').trim();
      text = `RULE-SET,${name || '?'},${SET_TARGET[tk.toLowerCase()] || tk || 'GENERAL'}`;
      if (!name) { out.rows.push({ id: `set-${i}`, kind: 'set', text, policy: null, dropped: true }); return; }
    }
    if (!text) return;
    const policy = policyOf(text);
    out.rows.push({ id: `set-${i}`, kind: 'set', text, policy, dropped: BARRED.has(policy) });
  });
  return out;
}

/* ---- the rule list (配置整理 P2: routing.rule_list, routing_rules.py) -------------------
   One list in match order, each row a condition and its action. The page keeps a simple condition
   as its literal <kind>:<value> (the 当 … 时 field writes it, covers / shadowed read it), a
   combination as { op: and | or, of: [{ v, not }] } and a stored Clash rule it cannot express as
   { raw: true, v, params } (kept as it is: moved, deleted, never edited). An off row (off, note)
   is kept and not compiled. Tesseract's own rules around the list are not the page's. */
export const KINDS = Object.freeze(['domain', 'suffix', 'keyword', 'regex', 'geosite', 'geoip', 'ip-cidr', 'ip-asn',
  'src-ip-cidr', 'dst-port', 'src-port', 'process', 'process-path', 'network', 'rule-set']);
const TO_OF = { direct: 'direct', proxy: 'proxy', block: 'reject' };
const ACT_OF_TO = { direct: 'direct', proxy: 'proxy', reject: 'block' };
/** a literal → { kind, value } (the first colon after the kind: process-path:C:\… keeps its own) */
export function splitLit(v) {
  const m = String(v == null ? '' : v).match(/^([a-z][a-z-]*):([\s\S]*)$/i);
  return m && KINDS.includes(m[1].toLowerCase()) ? { kind: m[1].toLowerCase(), value: m[2] } : null;
}
const condOf = c => (obj(c) && KINDS.includes(c.kind) && typeof c.value === 'string' ? Object.assign({ v: `${c.kind}:${c.value}` }, c.not ? { not: true } : {}) : null);
/** a routing.rule_list row → the page's row (null: not one the page can show) */
export function rowFromBackend(r, i) {
  if (!obj(r) || !ACT_OF_TO[r.to]) return null;
  const row = { id: `r-${i}`, act: ACT_OF_TO[r.to] };
  if (r.kind === 'and' || r.kind === 'or') {
    const of = Array.isArray(r.of) ? r.of.map(condOf) : [];
    if (of.length < 2 || !of.every(Boolean)) return null;
    Object.assign(row, { op: r.kind, of });
  } else if (r.kind === 'clash') {
    if (typeof r.value !== 'string') return null;
    Object.assign(row, { raw: true, v: r.value }, Array.isArray(r.params) && r.params.length ? { params: r.params.slice() } : {});
  } else {
    const c = condOf(r);
    if (!c) return null;
    Object.assign(row, c, r.resolve ? { resolve: true } : {});
  }
  if (r.off) Object.assign(row, { off: true }, r.note ? { note: r.note } : {});
  return row;
}
/** the page's row → a routing.rule_list row */
export function rowToBackend(r) {
  const out = { to: TO_OF[r.act] };
  const cond = c => { const s = splitLit(c.v) || { kind: 'clash', value: c.v }; return Object.assign({ kind: s.kind, value: s.value }, c.not ? { not: true } : {}); };
  if (r.op) Object.assign(out, { kind: r.op, of: r.of.map(cond) });
  else if (r.raw) Object.assign(out, { kind: 'clash', value: r.v }, r.params && r.params.length ? { params: r.params.slice() } : {});
  else Object.assign(out, cond(r), r.resolve ? { resolve: true } : {});
  if (r.off) Object.assign(out, { off: true }, r.note ? { note: r.note } : {});
  return out;
}
/** an older projection without rule_list: the three lists in rule_order, then the configuration
    file's rules (advancedRules) as stored rows, the ones the backend dropped off */
function rulesFromLegacy(values, secretFields) {
  const routing = obj(obj(values) && values.routing) || {}, rules = obj(routing.rules) || {};
  const order = orderFromBackend(routing.rule_order) || ['direct', 'proxy', 'block'];
  const rows = [];
  for (const act of order) {
    (strList(rules[BUCKET[act]]) || []).forEach(s => {
      const r = ruleFromBackend(s);
      rows.push(Object.assign({ id: `r-${rows.length}`, act }, r.raw ? { raw: true, v: `${s}`.trim() } : { v: r.v }));
    });
  }
  advancedRules(values, secretFields).rows.forEach(x => {
    const parts = x.text.split(',').map(p => p.trim()), act = { DIRECT: 'direct', REJECT: 'block' }[(x.policy || '').toUpperCase()] || 'proxy';
    // the rule without its policy (a logic rule's policy is its last part), its params kept
    const logic = /^(AND|OR|NOT|SUB-RULE)$/i.test(parts[0]);
    const keep = logic ? x.text.slice(0, x.text.lastIndexOf(',')) : parts.length >= 3 ? `${parts[0]},${parts[1]}` : x.text;
    const params = !logic && parts.length > 3 ? parts.slice(3) : [];
    rows.push(Object.assign({ id: `r-${rows.length}`, act, raw: true, v: keep }, params.length ? { params } : {},
      x.dropped ? { off: true, note: x.policy ? 'dropped_policy' : 'unreadable' } : {}));
  });
  return rows;
}

/* ---- rule sets (routing.providers): name, content, address ------------------------------
   An address the projection withholds (credentials, a query: url_hidden) or a rule set that is not
   a download (a local file) is shown, kept and never sent: the row goes out by name only. */
export const SET_BEHAVIORS = Object.freeze(['domain', 'ipcidr', 'classical']);
function setsFromSnapshot(routing) {
  if (Array.isArray(routing.providers)) {
    return routing.providers.filter(p => obj(p) && typeof p.name === 'string').map((p, i) => Object.assign({ id: `s-${i}`, name: p.name },
      typeof p.url === 'string' ? { url: p.url } : {}, p.url_hidden ? { hidden: true } : {},
      SET_BEHAVIORS.includes(p.behavior) ? { behavior: p.behavior } : {}, typeof p.format === 'string' ? { format: p.format } : {},
      typeof p.type === 'string' ? { type: p.type } : {}));
  }
  const map = obj(routing.rule_providers) || {};
  return Object.keys(map).map((name, i) => Object.assign({ id: `s-${i}`, name, hidden: true },
    SET_BEHAVIORS.includes(obj(map[name]) && map[name].behavior) ? { behavior: map[name].behavior } : {}, obj(map[name]) && typeof map[name].type === 'string' ? { type: map[name].type } : {}));
}
/** a rule set → routing.providers: a public download with its address, anything else by name */
export const setToBackend = s => (s.url && !s.hidden ? Object.assign({ name: s.name, url: s.url, behavior: s.behavior || 'classical' }, s.format ? { format: s.format } : {}) : { name: s.name });
/** the rule sets a compiled row names (an off row is not compiled) */
export function setsInUse(list) {
  const names = new Set();
  for (const r of list || []) {
    if (r.off) continue;
    for (const c of r.op ? r.of : [r]) { const s = !r.raw && splitLit(c.v); if (s && s.kind === 'rule-set') names.add(s.value); }
  }
  return names;
}

/* ---- DNS modes, source kinds, resolver kinds ---------------------------------------- */
const DNS_IN = { auto: 'auto', encrypted_only: 'enc', encrypted: 'enc', system: 'system', custom: 'custom' };
const DNS_OUT = { auto: 'auto', enc: 'encrypted_only', system: 'system', custom: 'custom' };
export const SRC_KIND = Object.freeze({ sub: 'subscription_urls', gh: 'github_repos', tg: 'telegram_channels', file: 'local_files' });
const SRC_OF = { subscription_urls: 'sub', github_repos: 'gh', telegram_channels: 'tg', local_files: 'file' };
export const RES_FIELD = Object.freeze({ main: 'nameserver', node: 'proxy_server_nameserver', boot: 'default_nameserver' });
/* the field-level ids a projection may withhold whole (secret_fields_set) → the draft key */
const HIDDEN_IDS = {
  ksDnsList: ['killswitch.dns_whitelist'], ksExtra: ['killswitch.additional_whitelist'],
  bypass: ['network.system_proxy.bypass'],
  dnsList: ['dns.nameserver', 'dns.proxy_server_nameserver', 'dns.default_nameserver'],
  rules: ['routing.rule_list', 'routing.rules.direct', 'routing.rules.proxy', 'routing.rules.reject'],
};

/* ---- config.snapshot → the page's base ---------------------------------------------- */
/** values: ConfigValues; secretFields: secret_fields_set. → { values: {key: v}, known: {key: bool},
    hidden: {key: [field ids withheld]} }. A key whose field is absent (a frozen sidecar, a
    projection without it) is not known: its row is read-only (never saved over a value the page
    could not read, mapping_settings R-03). */
export function fromSnapshot(values, secretFields) {
  const v = obj(values) || {};
  const sf = new Set(Array.isArray(secretFields) ? secretFields : []);
  const out = { values: {}, known: {}, hidden: {} };
  const put = (k, val, ok) => { out.known[k] = !!ok; if (ok) out.values[k] = val; };
  const ks = obj(v.killswitch) || {}, ui = obj(v.ui), net = obj(v.network) || {}, dns = obj(v.dns) || {};
  const sp = obj(net.system_proxy) || {}, eg = obj(net.egress_recovery) || {}, sub = obj(net.subscription) || {};
  const routing = obj(v.routing) || {}, rules = obj(routing.rules), lat = obj(v.latency) || {}, bas = obj(v.bastion) || {};
  const auto = obj(v.autostart) || {};
  for (const [k, ids] of Object.entries(HIDDEN_IDS)) { const h = ids.filter(id => sf.has(id)); if (h.length) out.hidden[k] = h; }
  // a list withheld whole reads as empty: the page shows what it can and says some are hidden
  const list = (k, val) => { const l = strList(val); if (l) return put(k, l, true); put(k, [], !!out.hidden[k]); };
  put('ks', ks.enabled, isBool(ks.enabled));
  put('ksLan', ks.allow_lan, isBool(ks.allow_lan));
  put('ksDns', ks.allow_dns, isBool(ks.allow_dns));
  list('ksDnsList', ks.dns_whitelist);
  list('ksExtra', ks.additional_whitelist);
  put('autoExit', ui && ui.overview_auto_exit_lookup, !!ui && isBool(ui.overview_auto_exit_lookup));
  put('lang', ui && ui.language, !!ui && typeof ui.language === 'string' && !!ui.language);
  // D8: off unless saved on (only a boolean is projected; ui present = the projection ran)
  put('tray', !!(ui && ui.start_in_tray_on_autostart === true), !!ui);
  put('autostart', auto.enabled, isBool(auto.enabled));
  put('lan', net.allow_lan, isBool(net.allow_lan));
  put('ipv6', net.ipv6, isBool(net.ipv6));
  put('portMixed', net.mihomo_mixed_port, isInt(net.mihomo_mixed_port));
  // read as network.subscription.port, written as network.subscription_port (mapping_settings §1.1)
  put('portSub', sub.port, isInt(sub.port));
  list('bypass', sp.bypass);
  put('egress', eg.enabled, isBool(eg.enabled));
  put('egressRetry', eg.retry_source_refresh, isBool(eg.retry_source_refresh));
  put('dnsMode', DNS_IN[dns.mode], !!DNS_IN[dns.mode]);
  put('dnsRules', dns.respect_rules, isBool(dns.respect_rules));
  {
    const rows = []; let ok = true;
    for (const t of ['main', 'node', 'boot']) {
      const l = strList(dns[RES_FIELD[t]]);
      if (!l) { if (!(out.hidden.dnsList || []).includes(`dns.${RES_FIELD[t]}`) && dns[RES_FIELD[t]] !== undefined) ok = false; continue; }
      l.forEach((x, i) => rows.push({ id: `d-${t}-${i}`, t, v: x }));
    }
    put('dnsList', rows, ok && !!obj(v.dns));
  }
  put('latency', lat.primary_gateway_max_latency_ms, isInt(lat.primary_gateway_max_latency_ms));
  put('bastionOn', bas.enabled, isBool(bas.enabled));
  put('bastionMin', bas.min_alive_nodes, isInt(bas.min_alive_nodes));
  // 配置整理 P2: the one list (routing.rule_list); an older projection gives the three lists
  if (Array.isArray(routing.rule_list)) put('rules', routing.rule_list.map(rowFromBackend).filter(Boolean), true);
  else if (rules) put('rules', rulesFromLegacy(v, secretFields), true);
  else put('rules', [], false);
  put('ruleSets', setsFromSnapshot(routing), Array.isArray(routing.providers) || !!obj(routing.rule_providers));
  {
    // 提醒: alerts.notify; an absent block (a frozen sidecar) leaves the rows read-only
    const notify = obj(obj(v.alerts) && v.alerts.notify);
    const events = notify && strList(notify.events);
    put('nOn', notify && notify.enabled, !!notify && isBool(notify.enabled));
    put('nSound', notify && notify.sound, !!notify && isBool(notify.sound));
    for (const [k, group] of NOTICE_KEYS) put(k, !!events && events.includes(group), !!events);
  }
  if (Array.isArray(v.sources)) {
    put('sources', v.sources.filter(s => obj(s) && typeof s.id === 'string' && SRC_OF[s.kind])
      .map(s => ({ id: s.id, t: SRC_OF[s.kind], hint: typeof s.hint === 'string' ? s.hint : null, enabled: s.enabled !== false, v: '' })), true);
  } else put('sources', [], false);
  return out;
}

/* ---- the page's draft → settings.apply changes -------------------------------------- */
/** { draft, base, keys, adapt? } → { changes: [ConfigChange], sent: {section: [keys]}, skipped: [keys] }.
    Only the changed fields go out: a list key sends only the sub-fields whose list changed (a DNS
    kind, a rule bucket). A key outside SECTION goes through adapt(key) → { section, values,
    secrets? } | null (values merge shallowly per section, secrets append); a key adapt does not
    take (null, or no adapt) is skipped and stays a draft (the typed-but-not-added fields). */
export function toChanges({ draft, base, keys, adapt = null }) {
  const vals = {}, secrets = {}, sent = {}, skipped = [];
  const into = sec => (vals[sec] = vals[sec] || {});
  const mark = (sec, k) => { (sent[sec] = sent[sec] || []).push(k); };
  for (const k of keys) {
    const sec = SECTION[k], d = draft[k];
    if (!sec) {
      const a = adapt ? adapt(k) : null;
      if (!a || !SECTIONS.has(a.section) || !obj(a.values)) { skipped.push(k); continue; }
      Object.assign(into(a.section), clone(a.values));
      if (Array.isArray(a.secrets) && a.secrets.length) (secrets[a.section] = secrets[a.section] || []).push(...clone(a.secrets));
      mark(a.section, k);
      continue;
    }
    const s = into(sec);
    switch (k) {
      case 'ks': s.enabled = !!d; break;
      case 'ksLan': s.allow_lan = !!d; break;
      case 'ksDns': s.allow_dns = !!d; break;
      case 'ksDnsList': s.dns_whitelist = d.slice(); break;
      case 'ksExtra': s.additional_whitelist = d.slice(); break;
      case 'autoExit': s.overview_auto_exit_lookup = !!d; break;
      case 'lang': s.language = d; break;
      case 'tray': s.start_in_tray_on_autostart = !!d; break;
      case 'autostart': s.enabled = !!d; break;
      case 'lan': s.allow_lan = !!d; break;
      case 'ipv6': s.ipv6 = !!d; break;
      case 'portMixed': s.mihomo_mixed_port = +d; break;
      case 'portSub': s.subscription_port = +d; break;
      case 'bypass': s.system_proxy = Object.assign(s.system_proxy || {}, { bypass: d.slice() }); break;
      case 'egress': s.egress_recovery = Object.assign(s.egress_recovery || {}, { enabled: !!d }); break;
      case 'egressRetry': s.egress_recovery = Object.assign(s.egress_recovery || {}, { retry_source_refresh: !!d }); break;
      case 'dnsMode': s.mode = DNS_OUT[d] || d; break;
      case 'dnsRules': s.respect_rules = !!d; break;
      case 'dnsList':
        for (const t of ['main', 'node', 'boot']) {
          const now = d.filter(r => r.t === t).map(r => r.v), was = (base.dnsList || []).filter(r => r.t === t).map(r => r.v);
          if (sig(now) !== sig(was)) s[RES_FIELD[t]] = now;
        }
        break;
      case 'latency': s.primary_gateway_max_latency_ms = +d; break;
      case 'bastionOn': s.enabled = !!d; break;
      case 'bastionMin': s.min_alive_nodes = +d; break;
      // 配置整理 P2: the whole list and the whole set of rule sets (each replaces the stored one)
      case 'rules': s.rule_list = d.map(rowToBackend); break;
      case 'ruleSets': s.providers = d.map(setToBackend); break;
      case 'nOn': s.notify = Object.assign(s.notify || {}, { enabled: !!d }); break;
      case 'nSound': s.notify = Object.assign(s.notify || {}, { sound: !!d }); break;
      case 'nConn': case 'nTier': case 'nUpd':
        // the whole list goes out: one switch changes membership, the others keep theirs
        s.notify = Object.assign(s.notify || {}, { events: NOTICE_KEYS.filter(([key]) => !!draft[key]).map(([, group]) => group) });
        break;
      case 'sources':
        s.items = d.map(x => ({ id: x.id, kind: SRC_KIND[x.t], enabled: x.enabled !== false }));
        secrets.sources = d.filter(x => x.v).map(x => ({ field: 'value', target_id: x.id, action: 'set', value: x.v }));
        break;
      default: skipped.push(k); continue;
    }
    mark(sec, k);
  }
  const changes = [];
  for (const sec of Object.keys(vals)) {
    if (!sent[sec]) continue;
    const c = { section: sec, values: vals[sec] };
    if (secrets[sec] && secrets[sec].length) c.secrets = secrets[sec];
    changes.push(c);
  }
  return { changes, sent, skipped };
}

/* ---- an Error.field → the draft key whose row it names (a hold lands on its row) ---- */
const FIELD_KEY = [
  [/^killswitch\.enabled\b/, 'ks'], [/^killswitch\.allow_lan\b/, 'ksLan'], [/^killswitch\.allow_dns\b/, 'ksDns'],
  [/^killswitch\.dns_whitelist\b/, 'ksDnsList'], [/^killswitch\.additional_whitelist\b/, 'ksExtra'],
  [/^ui\.overview_auto_exit_lookup\b/, 'autoExit'], [/^ui\.language\b/, 'lang'], [/^ui\.start_in_tray_on_autostart\b/, 'tray'],
  [/^autostart\b/, 'autostart'],
  [/^routing\.providers\b/, 'ruleSets'], [/^routing\b/, 'rules'],
  [/^network\.system_proxy\b/, 'bypass'], [/^network\.allow_lan\b/, 'lan'], [/^network\.ipv6\b/, 'ipv6'],
  [/^network\.mihomo_mixed_port\b/, 'portMixed'], [/^network\.subscription(_port|\.port)\b/, 'portSub'],
  [/^network\.egress_recovery\.retry_source_refresh\b/, 'egressRetry'], [/^network\.egress_recovery\b/, 'egress'],
  [/^dns\.mode\b/, 'dnsMode'], [/^dns\.respect_rules\b/, 'dnsRules'], [/^dns\b/, 'dnsList'],
  [/^latency\b/, 'latency'], [/^bastion\.enabled\b/, 'bastionOn'], [/^bastion\.min_alive_nodes\b/, 'bastionMin'],
  [/^sources\b/, 'sources'],
  [/^alerts\.notify\.sound\b/, 'nSound'], [/^alerts\.notify\.events\b/, 'nConn'], [/^alerts\b/, 'nOn'],
];
export function fieldKey(field) {
  const f = String(field || '');
  for (const [re, k] of FIELD_KEY) if (re.test(f)) return k;
  return null;
}

/* ---- a settings.apply outcome → the save bar's result and each row's label ------------
   kinds: applied (已生效) · pending (已保存，待应用) · next_start (服务未运行：下次启动生效) ·
   not_applied (已保存但没有生效, also a partial save) · held (还不能保存: Error.hold) ·
   conflict (base_rev stale: re-read the snapshot) · failed (nothing saved).
   rows: {section: {state: applied | pending | next_start | not_applied | failed, error}}.
   run: the service's run state when the job ended (store `run`); collecting / core_standby count as
   running (PROTOCOL §15), a stopped service applies on its next start. */
const STOPPED = new Set(['stopped', 'failed']);
const LIVE_EFFECTS = new Set(['reload', 'restart', 'recheck']);
const EFFECT_RANK = { restart: 3, reload: 2, recheck: 1 };
export function outcome({ result = null, error = null } = {}, { run = 'unknown', sent = {} } = {}) {
  const res = result && typeof result === 'object' ? result : (error && error.result) || null;
  const sections = res && Array.isArray(res.sections) ? res.sections : [];
  const stopped = STOPPED.has(run);
  const pend = res && res.pending && (res.pending.state === 'pending' || res.pending.state === 'applying') ? res.pending : null;
  const rows = {};
  let effect = 'instant', bestRank = 0;
  for (const s of sections) {
    if (!s || typeof s.section !== 'string') continue;
    let state;
    if (!s.saved) state = 'failed';
    else if (s.applied) {
      const keys = sent[s.section] || [];
      const est = keys.length ? heaviest(keys, null) : 'instant';
      state = stopped && (LIVE_EFFECTS.has(s.effect) || LIVE_EFFECTS.has(est)) ? 'next_start' : 'applied';
      const r = EFFECT_RANK[s.effect] || 0;
      if (state === 'applied' && r > bestRank) { bestRank = r; effect = s.effect; }
    } else state = (s.effect === 'deferred' || pend) && !s.error ? 'pending' : 'not_applied';
    rows[s.section] = { state, error: s.error || null, effect: s.effect || 'none' };
  }
  const states = Object.values(rows).map(r => r.state);
  const any = st => states.includes(st);
  const base = { rows, effect, pending: pend, field: null, error: null };
  if (error && !(res && sections.some(s => s && s.saved))) {
    const e = error;
    if (e.code === 'conflict' && e.reason_code !== 'recovery_pending') return { ...base, kind: 'conflict', error: e };
    if (e.hold) return { ...base, kind: 'held', error: e, field: e.field || null };
    return { ...base, kind: 'failed', error: e };
  }
  const kind = any('not_applied') || any('failed') ? 'not_applied' : any('pending') ? 'pending' : any('next_start') ? 'next_start' : 'applied';
  const firstErr = Object.values(rows).map(r => r.error).find(Boolean) || error || null;
  return { ...base, kind, error: kind === 'not_applied' ? firstErr : null };
}
/** an outcome → the save bar's face (core.js T.ui.saveBar results): { outcome: applied | pending |
    not_applied | held | conflict | failed, text: message id, error?: the Error whose words replace
    text, retry?: the retry button's message id }. applied / pending / not_applied resolve the bar's
    save; held / conflict / failed reject it (the page words them). */
export function barFace(o) {
  switch (o && o.kind) {
    case 'not_applied': return { outcome: 'not_applied', text: 'core.savebar.failed_reason', error: o.error || null, retry: 'settings.save.retry_apply' };
    case 'pending': return { outcome: 'pending', text: 'settings.save.done.pending' };
    case 'next_start': return { outcome: 'applied', text: 'settings.save.done.next_start_short' };
    case 'held': return { outcome: 'held', text: 'core.savebar.held_reason', error: o.error || null };
    case 'conflict': return { outcome: 'conflict', text: 'settings.save.conflict_body' };
    case 'failed': return { outcome: 'failed', text: 'core.savebar.failed_reason', error: o.error || null };
    default: {
      const e = o && o.effect;
      return { outcome: 'applied', text: e === 'restart' ? 'core.savebar.done.restart' : e === 'reload' ? 'core.savebar.done.reload' : e === 'recheck' ? 'settings.save.done.recheck' : 'core.savebar.done.instant' };
    }
  }
}
/** the label id of a row's own result (rows[section].state, or a page state: external) */
export const rowLabel = state => `settings.save.row.${state}`;
/** a PendingApply update (config.changed.pending / config.snapshot.pending) → the label of the rows
    it holds (runtime_settings_apply.py's ledger): applied → applied; failed → not_applied;
    cancelled (the service stopped: the saved file loads on the next start) → next_start;
    superseded (a newer config_rev carries the work in a successor intent, or a newer start loaded
    the file) and pending / applying → pending (the successor, or pending:null, ends it) */
export function pendingState(p) {
  if (!p || typeof p !== 'object') return null;
  if (p.state === 'applied') return 'applied';
  if (p.state === 'failed') return 'not_applied';
  if (p.state === 'cancelled') return 'next_start';
  return 'pending';
}
/** does a PendingApply carry the rows a page save left pending? track: { ids: Set, rev } (the
    intent ids seen, the config_rev of the first save that left rows pending). The ledger merges:
    a newer config_rev rebases the live intent into a successor with a new id (and a gateway.select
    or another save does the same), so every intent at config_rev ≥ rev carries those rows. */
export function pendingFollows(p, track) {
  if (!p || typeof p !== 'object' || !track) return false;
  if (typeof p.intent_id === 'string' && track.ids && track.ids.has(p.intent_id)) return true;
  return Number.isInteger(p.config_rev) && Number.isInteger(track.rev) && p.config_rev >= track.rev;
}
