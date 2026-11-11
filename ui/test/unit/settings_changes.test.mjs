// T-P4-05d: the settings page's draft keys ⇄ the v1 contract (ui/src/pages/settings/changes.js),
// the save model's result table, and dirtyCount() against the save bar.
//
// Pure (changes.js has no DOM and no T):
//   * config.snapshot ConfigValues → the base of every row (known / hidden), incl. the port's read
//     name network.subscription.port and D8's ui.start_in_tray_on_autostart;
//   * every draft key → its ConfigChange (section, values, secrets), incl. the rule list ⇄
//     routing.rule_list rows, the rule sets ⇄ routing.providers (配置整理 P2), SecretEdit set /
//     keep / clear, the write name network.subscription_port;
//   * a settings.apply outcome → the save bar's face and each row's label (S-01…S-07, S-22);
//   * Error.field → the row a hold lands on.
// Browser (local Chrome through playwright-core; skipped when Chrome is missing): with drafts, the
// bar's count = T.ui.saveBar.dirtyCount() = changes.js countChanges, and drafts the bar no longer
// holds (another page after a forced navigation) still count in the shell's quit question.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as C from '../../src/pages/settings/changes.js';

const UI = new URL('../../', import.meta.url);
const read = rel => readFileSync(new URL(rel, UI), 'utf8');
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const skip = !existsSync(CHROME) && 'local Chrome not found';
const ZH = { ...JSON.parse(read('src/i18n/locales/zh_CN/settings.json')), ...JSON.parse(read('src/i18n/locales/zh_CN/core.json')) };

/* a full v1 projection (PROTOCOL §16 ConfigValues, T-P4-C1 additions) */
const VALUES = () => ({
  network: { allow_lan: false, ipv6: true, mihomo_mixed_port: 7890, mode: 'system', system_proxy: { bypass: ['localhost', '192.168.*'] },
    subscription: { port: 25500 }, egress_recovery: { enabled: true, retry_source_refresh: false } },
  dns: { mode: 'encrypted_only', respect_rules: true, nameserver: ['https://dns.example.net/dns-query'], proxy_server_nameserver: ['tls://dot.example.net'], default_nameserver: ['192.0.2.53'] },
  routing: { rules: { direct: ['suffix:example.cn'], proxy: [], reject: [],
    compatibility_defaults: [{ rule_type: 'direct', value: 'suffix:example.com' }, { rule_type: 'reject', value: 'keyword:ads' }] },
    rule_order: ['direct', 'proxy', 'reject'],
    // 配置整理 P2: the one list in match order (routing_rules.effective_rule_list) and the rule sets (provider_rows)
    rule_list: [
      { to: 'direct', kind: 'suffix', value: 'example.cn' },
      { to: 'proxy', kind: 'geoip', value: 'CN' },
      { to: 'proxy', kind: 'ip-cidr', value: '2001:db8::/32' },
      { to: 'reject', kind: 'and', of: [{ kind: 'dst-port', value: '443' }, { kind: 'network', value: 'udp' }] },
      { to: 'direct', kind: 'rule-set', value: 'cn-sites' },
      { to: 'reject', kind: 'clash', value: 'PROCESS-NAME,x.exe', params: ['extra'] },
      { to: 'proxy', kind: 'suffix', value: 'old.example.net', off: true, note: 'target_gone' },
      { to: 'direct', kind: 'geoip', value: 'JP', resolve: true },
      { to: 'proxy', kind: 'keyword', value: 'ads', not: true },
    ],
    providers: [{ name: 'cn-sites', type: 'http', behavior: 'domain', format: 'yaml', url: 'https://rules.example.net/cn.yaml' },
      { name: 'private', type: 'http', behavior: 'classical', url_hidden: true }, { name: 'local', type: 'file', behavior: 'ipcidr' }] },
  latency: { primary_gateway_max_latency_ms: 800 },
  sources: [{ id: 'src-1', kind: 'github_repos', display: 'GitHub', enabled: true, hint: 'e•••/f•••' }, { id: 'src-2', kind: 'subscription_urls', display: 'Sub', enabled: false }],
  alerts: { notify: { enabled: true, events: ['connection', 'tier_fallback', 'update'], sound: true } },
  ui: { language: 'zh_CN', overview_auto_exit_lookup: true, start_in_tray_on_autostart: true },
  autostart: { enabled: true },
  killswitch: { enabled: true, allow_lan: true, allow_dns: false, dns_whitelist: ['192.0.2.53'], additional_whitelist: ['LocalSubnet'] },
  bastion: { enabled: true, min_alive_nodes: 5 },
});
const SECTIONS = ['network', 'dns', 'routing', 'latency', 'sources', 'alerts', 'ui', 'autostart', 'traffic', 'killswitch', 'bastion'];
const base = () => C.fromSnapshot(VALUES(), []).values;
/** the change of one key edited in place: draft = base with edit(draft) applied */
function changeOf(key, edit, o = {}) {
  const b = o.base || base();
  const d = C.clone(b);
  edit(d);
  return C.toChanges({ draft: d, base: b, keys: [key], adapt: o.adapt || null });
}

/* ------------------------------------------------------------------ config.snapshot → base */
test('fromSnapshot: every contract key from a full projection is known; read names (subscription.port), DNS mode, the rule list and rule sets, sources', () => {
  const m = C.fromSnapshot(VALUES(), []);
  for (const k of C.KEYS) assert.equal(m.known[k], true, `known ${k}`);
  assert.deepEqual(m.hidden, {});
  const v = m.values;
  assert.equal(v.portSub, 25500, 'read as network.subscription.port');
  assert.equal(v.portMixed, 7890);
  assert.equal(v.dnsMode, 'enc');
  assert.equal(v.tray, true, 'D8 ui.start_in_tray_on_autostart');
  assert.equal(v.autoExit, true);
  assert.deepEqual(v.ksExtra, ['LocalSubnet']);
  assert.deepEqual(v.dnsList.map(r => [r.t, r.v]), [['main', 'https://dns.example.net/dns-query'], ['node', 'tls://dot.example.net'], ['boot', '192.0.2.53']]);
  assert.deepEqual(v.rules, [
    { id: 'r-0', act: 'direct', v: 'suffix:example.cn' },
    { id: 'r-1', act: 'proxy', v: 'geoip:CN' },
    { id: 'r-2', act: 'proxy', v: 'ip-cidr:2001:db8::/32' },
    { id: 'r-3', act: 'block', op: 'and', of: [{ v: 'dst-port:443' }, { v: 'network:udp' }] },
    { id: 'r-4', act: 'direct', v: 'rule-set:cn-sites' },
    { id: 'r-5', act: 'block', raw: true, v: 'PROCESS-NAME,x.exe', params: ['extra'] },
    { id: 'r-6', act: 'proxy', v: 'suffix:old.example.net', off: true, note: 'target_gone' },
    { id: 'r-7', act: 'direct', v: 'geoip:JP', resolve: true },
    { id: 'r-8', act: 'proxy', v: 'keyword:ads', not: true },
  ], 'routing.rule_list wins over the older lists the projection still carries');
  assert.ok(!('order' in v), 'routing.rule_order is not a page key: the list is the match order');
  assert.deepEqual(v.ruleSets, [
    { id: 's-0', name: 'cn-sites', url: 'https://rules.example.net/cn.yaml', behavior: 'domain', format: 'yaml', type: 'http' },
    { id: 's-1', name: 'private', hidden: true, behavior: 'classical', type: 'http' },
    { id: 's-2', name: 'local', behavior: 'ipcidr', type: 'file' }]);
  assert.ok(!('compat' in v), 'the purged compatibility stage is not a page key (it compiles nowhere)');
  assert.deepEqual(v.sources.map(s => [s.id, s.t, s.hint, s.enabled, s.v]), [['src-1', 'gh', 'e•••/f•••', true, ''], ['src-2', 'sub', null, false, '']]);
  assert.equal(v.bastionMin, 5);
});

test('fromSnapshot: a field the projection does not carry leaves its key unknown (read-only row, never saved over); a list withheld whole is known, empty and hidden', () => {
  const vals = VALUES();
  delete vals.bastion; delete vals.network.egress_recovery; vals.killswitch = { enabled: true };
  vals.ui = { language: 'en_US' };                       // a frozen sidecar: no D8 key
  const m = C.fromSnapshot(vals, ['killswitch.additional_whitelist', 'dns.nameserver']);
  for (const k of ['bastionOn', 'bastionMin', 'egress', 'egressRetry', 'ksLan', 'ksDns', 'autoExit']) assert.equal(m.known[k], false, k);
  assert.equal(m.known.ksDnsList, false, 'absent and not withheld');
  assert.equal(m.known.ksExtra, true, 'withheld whole: editable after the confirmation');
  assert.deepEqual(m.values.ksExtra, []);
  assert.deepEqual(m.hidden.ksExtra, ['killswitch.additional_whitelist']);
  assert.deepEqual(m.hidden.dnsList, ['dns.nameserver']);
  assert.equal(m.known.tray, true); assert.equal(m.values.tray, false, 'D8 is off unless saved on');
  const none = C.fromSnapshot(undefined, undefined);
  assert.ok(C.KEYS.every(k => none.known[k] === false || ['tray'].includes(k) === false), 'nothing known without values');
  assert.ok(C.KEYS.every(k => !none.known[k]));
});

/* ------------------------------------------------------------------ draft key → ConfigChange */
const ONE = [
  // key, edit, section, values
  ['ks', d => { d.ks = false; }, 'killswitch', { enabled: false }],
  ['ksLan', d => { d.ksLan = false; }, 'killswitch', { allow_lan: false }],
  ['ksDns', d => { d.ksDns = true; }, 'killswitch', { allow_dns: true }],
  ['ksDnsList', d => { d.ksDnsList = ['192.0.2.53', '198.51.100.53']; }, 'killswitch', { dns_whitelist: ['192.0.2.53', '198.51.100.53'] }],
  ['ksExtra', d => { d.ksExtra = ['LocalSubnet', 'DHCP']; }, 'killswitch', { additional_whitelist: ['LocalSubnet', 'DHCP'] }],
  ['autoExit', d => { d.autoExit = false; }, 'ui', { overview_auto_exit_lookup: false }],
  ['lang', d => { d.lang = 'en_US'; }, 'ui', { language: 'en_US' }],
  ['tray', d => { d.tray = false; }, 'ui', { start_in_tray_on_autostart: false }],
  ['autostart', d => { d.autostart = false; }, 'autostart', { enabled: false }],
  ['lan', d => { d.lan = true; }, 'network', { allow_lan: true }],
  ['ipv6', d => { d.ipv6 = false; }, 'network', { ipv6: false }],
  ['portMixed', d => { d.portMixed = 7891; }, 'network', { mihomo_mixed_port: 7891 }],
  ['portSub', d => { d.portSub = 25600; }, 'network', { subscription_port: 25600 }],   // written as network.subscription_port
  ['bypass', d => { d.bypass = ['localhost']; }, 'network', { system_proxy: { bypass: ['localhost'] } }],
  ['egress', d => { d.egress = false; }, 'network', { egress_recovery: { enabled: false } }],
  ['egressRetry', d => { d.egressRetry = true; }, 'network', { egress_recovery: { retry_source_refresh: true } }],
  ['dnsMode', d => { d.dnsMode = 'custom'; }, 'dns', { mode: 'custom' }],
  ['dnsRules', d => { d.dnsRules = false; }, 'dns', { respect_rules: false }],
  ['latency', d => { d.latency = 1200; }, 'latency', { primary_gateway_max_latency_ms: 1200 }],
  ['bastionOn', d => { d.bastionOn = false; }, 'bastion', { enabled: false }],
  ['bastionMin', d => { d.bastionMin = 7; }, 'bastion', { min_alive_nodes: 7 }],
];
for (const [key, edit, section, values] of ONE) {
  test(`toChanges: ${key} → ${section} ${JSON.stringify(values)}`, () => {
    const r = changeOf(key, edit);
    assert.deepEqual(r.changes, [{ section, values }]);
    assert.deepEqual(r.sent, { [section]: [key] });
    assert.deepEqual(r.skipped, []);
    assert.equal(C.SECTION[key], section);
    assert.ok(SECTIONS.includes(section));
  });
}

test('toChanges: the DNS mode enc is written encrypted_only; the resolver list sends only the kinds that changed', () => {
  assert.deepEqual(changeOf('dnsMode', d => { d.dnsMode = 'enc'; }, { base: { ...base(), dnsMode: 'auto' } }).changes, [{ section: 'dns', values: { mode: 'encrypted_only' } }]);
  const r = changeOf('dnsList', d => { d.dnsList = d.dnsList.filter(x => x.t !== 'node').concat([{ id: 'n1', t: 'node', v: 'tls://dot2.example.net' }]); });
  assert.deepEqual(r.changes, [{ section: 'dns', values: { proxy_server_nameserver: ['tls://dot2.example.net'] } }]);
});

test('toChanges: the rules go out as one list in match order (routing.rule_list, whole); a resave writes back what was read', () => {
  const stored = VALUES().routing.rule_list;
  assert.deepEqual(base().rules.map(C.rowToBackend), stored, 'every row reads back as it was stored (combination, rule set, raw with params, off, resolve, not)');
  assert.deepEqual(changeOf('rules', () => {}).changes, [{ section: 'routing', values: { rule_list: stored } }], 'the list goes out whole');
  assert.ok(C.same(C.fromSnapshot(VALUES(), []).values.rules, base().rules), 'a second read of the same snapshot is the same list (no draft)');
  const r = changeOf('rules', d => { d.rules = [{ id: 'n1', act: 'proxy', v: 'ip-cidr:2001:db8:1::/48' }].concat(d.rules); });
  assert.deepEqual(r.changes, [{ section: 'routing', values: { rule_list: [{ to: 'proxy', kind: 'ip-cidr', value: '2001:db8:1::/48' }, ...stored] } }]);
  // the order is the list's: two rows swapped are one change
  const r2 = changeOf('rules', d => { const [a, b, ...rest] = d.rules; d.rules = [b, a, ...rest]; });
  assert.deepEqual(r2.changes[0].values.rule_list.slice(0, 2), [stored[1], stored[0]]);
  assert.equal(C.countOf('rules', { rules: [base().rules[1], base().rules[0], ...base().rules.slice(2)] }, base()), 1, 'a reorder alone counts as one');
  // an action picked for an off row turns it on (the page drops off and its note)
  const on = changeOf('rules', d => { d.rules = d.rules.map(x => (x.off ? { id: x.id, act: 'direct', v: x.v } : x)); });
  assert.deepEqual(on.changes[0].values.rule_list[6], { to: 'direct', kind: 'suffix', value: 'old.example.net' });
  // a combination: each condition its kind and value (a path keeps its own colon), 不满足 as not
  assert.deepEqual(C.rowToBackend({ id: 'c', act: 'block', op: 'or', of: [{ v: 'process-path:C:\\Apps\\x.exe' }, { v: 'src-ip-cidr:192.168.1.0/24', not: true }] }),
    { to: 'reject', kind: 'or', of: [{ kind: 'process-path', value: 'C:\\Apps\\x.exe' }, { kind: 'src-ip-cidr', value: '192.168.1.0/24', not: true }] });
  assert.deepEqual(C.splitLit('process-path:C:\\x.exe'), { kind: 'process-path', value: 'C:\\x.exe' });
  assert.equal(C.splitLit('PROCESS-NAME,x.exe'), null, 'not a page literal');
  // a row the page cannot show is left out (the backend keeps nothing the page did not send)
  assert.equal(C.rowFromBackend({ to: 'proxy', kind: 'and', of: [{ kind: 'suffix', value: 'a.com' }] }, 0), null, 'a combination of one');
  assert.equal(C.rowFromBackend({ to: 'final-exit', kind: 'suffix', value: 'a.com' }, 0), null, 'a target the page does not offer');
  assert.equal(C.rowFromBackend({ to: 'proxy', kind: 'src-geoip', value: 'cn' }, 0), null, 'a kind the page does not know');
});

test('toChanges: rule sets go out whole (routing.providers): a public download with its address, a withheld address or a local file by name only', () => {
  const r = changeOf('ruleSets', d => { d.ruleSets = d.ruleSets.concat([{ id: 'n', name: 'ads', url: 'https://rules.example.net/ads.mrs', behavior: 'domain', format: 'mrs', type: 'http' }]); });
  assert.deepEqual(r.changes, [{ section: 'routing', values: { providers: [
    { name: 'cn-sites', url: 'https://rules.example.net/cn.yaml', behavior: 'domain', format: 'yaml' }, { name: 'private' }, { name: 'local' },
    { name: 'ads', url: 'https://rules.example.net/ads.mrs', behavior: 'domain', format: 'mrs' }] } }]);
  assert.equal(C.SECTION.ruleSets, 'routing');
  assert.equal(C.effectOf('ruleSets'), 'reload');
  // the rules and the rule sets of one save are one routing change
  const both = C.toChanges({ draft: { ...base(), rules: [], ruleSets: [] }, base: base(), keys: ['rules', 'ruleSets'] });
  assert.deepEqual(both.changes, [{ section: 'routing', values: { rule_list: [], providers: [] } }]);
  assert.deepEqual(both.sent, { routing: ['rules', 'ruleSets'] });
  // the rule sets a compiled row names: a combination's too; an off row is not compiled, a raw row not read
  const used = C.setsInUse([{ act: 'direct', v: 'rule-set:a' }, { act: 'proxy', op: 'and', of: [{ v: 'rule-set:b' }, { v: 'dst-port:443' }] },
    { act: 'proxy', v: 'rule-set:c', off: true }, { act: 'proxy', raw: true, v: 'RULE-SET,d' }]);
  assert.deepEqual([...used].sort(), ['a', 'b']);
});

test('fromSnapshot: an older projection without routing.rule_list: the three lists in routing.rule_order, then the configuration file\'s rules as stored rows (the ones the backend dropped are off)', () => {
  const vals = VALUES();
  delete vals.routing.rule_list; delete vals.routing.providers;
  vals.routing.rules = { direct: ['suffix:example.cn', 'DOMAIN-SUFFIX,example.de'], proxy: ['geoip:CN', 'ip-cidr6:2001:db8::/32'], reject: ['PROCESS-NAME,x.exe,extra'],
    raw: ['AND,((DST-PORT,443),(NETWORK,UDP)),REJECT', 'DST-PORT,22,DIRECT', 'nonsense'] };
  vals.routing.rule_order = ['reject', 'direct', 'proxy'];
  vals.routing.rule_providers = { media: { type: 'http', behavior: 'domain', url: 'https://rules.example.net/m.yaml' } };
  const m = C.fromSnapshot(vals, []);
  assert.equal(m.known.rules, true);
  assert.deepEqual(m.values.rules.map(r => [r.act, r.v, !!r.raw, !!r.off, r.note || null]), [
    ['block', 'PROCESS-NAME,x.exe,extra', true, false, null],
    ['direct', 'suffix:example.cn', false, false, null], ['direct', 'suffix:example.de', false, false, null],
    ['proxy', 'geoip:CN', false, false, null], ['proxy', 'ip-cidr:2001:db8::/32', false, false, null],
    ['block', 'AND,((DST-PORT,443),(NETWORK,UDP))', true, false, null],
    ['direct', 'DST-PORT,22', true, true, 'dropped_policy'],
    ['proxy', 'nonsense', true, true, 'unreadable'],
  ]);
  assert.equal(m.known.ruleSets, true);
  assert.deepEqual(m.values.ruleSets, [{ id: 's-0', name: 'media', hidden: true, behavior: 'domain', type: 'http' }], 'names only: never sent back with an address');
  const none = C.fromSnapshot({ ...VALUES(), routing: undefined }, []);
  assert.equal(none.known.rules, false, 'no routing at all: read-only');
  assert.equal(none.known.ruleSets, false);
});

test('shadowed: an earlier rule of another action that provably covers a later one, in the list\'s order', () => {
  const R = (id, act, v, x = {}) => ({ id, act, v, ...x });
  const list = [
    R('d1', 'direct', 'suffix:example.com'), R('d2', 'direct', 'ip-cidr:10.0.0.0/8'), R('d3', 'direct', 'keyword:ads'),
    R('p1', 'proxy', 'domain:api.example.com'), R('p2', 'proxy', 'suffix:example.org'), R('p3', 'proxy', 'ip-cidr:10.1.2.0/24'),
    R('p4', 'proxy', 'suffix:myexample.com'), R('p5', 'proxy', 'geosite:cn'), R('p6', 'proxy', 'domain:cdn.ads.example.net'),
    R('b1', 'block', 'domain:api.example.com.cn'), R('b2', 'block', 'suffix:example.org'), R('b3', 'block', 'DOMAIN,x.example.com', { raw: true }),
    R('p7', 'proxy', 'ip-cidr:2001:db8::/32'), R('b4', 'block', 'ip-cidr:2001:db8:1::/48'),
  ];
  const sh = C.shadowed(list);
  assert.deepEqual([...sh.keys()].sort(), ['b2', 'b4', 'p1', 'p3', 'p6'].sort());
  assert.equal(sh.get('p1').id, 'd1', 'a suffix covers a sub-domain');
  assert.equal(sh.get('p3').id, 'd2', 'an IPv4 network covers a smaller one inside it');
  assert.equal(sh.get('p6').id, 'd3', 'a keyword covers every host name holding it');
  assert.equal(sh.get('b2').id, 'p2', 'the same literal of an earlier action');
  assert.equal(sh.get('b4').id, 'p7', 'IPv6 networks too');
  assert.ok(!sh.has('p4'), 'myexample.com is not a sub-domain of example.com');
  assert.ok(!sh.has('b1'), 'api.example.com.cn is not under example.com');
  assert.ok(!sh.has('b3'), 'a rule kept as stored (raw) is never judged');
  // the list is the order: a broad rule further down covers nothing above it
  assert.equal(C.shadowed([R('p', 'proxy', 'domain:api.example.com'), R('d', 'direct', 'suffix:example.com')]).size, 0);
  // what proves nothing: an off row (not compiled), a negated condition, a combination
  assert.equal(C.shadowed([R('a', 'direct', 'suffix:example.com', { off: true }), R('b', 'proxy', 'domain:a.example.com')]).size, 0, 'off');
  assert.equal(C.shadowed([R('a', 'direct', 'suffix:example.com', { not: true }), R('b', 'proxy', 'domain:a.example.com')]).size, 0, 'not');
  assert.equal(C.shadowed([{ id: 'a', act: 'direct', op: 'and', of: [{ v: 'suffix:example.com' }, { v: 'dst-port:443' }] }, R('b', 'proxy', 'domain:a.example.com')]).size, 0, 'only on port 443');
  assert.equal(C.shadowed([R('a', 'direct', 'suffix:example.com'), R('b', 'proxy', 'domain:a.example.com', { not: true })]).size, 0, 'a negated later rule is not judged');
  // a same-action cover is not marked (it changes nothing)
  assert.equal(C.shadowed([R('a', 'proxy', 'suffix:example.com'), R('b', 'proxy', 'domain:a.example.com')]).size, 0);
});

test('localNet: an IP or network wholly inside the local and LAN networks Tesseract always sends direct (routing_rules.LOCAL_NETWORKS)', () => {
  for (const s of ['192.168.1.0/24', '10.0.0.1', '127.0.0.1/32', '172.16.5.0/24', '169.254.0.0/16', '::1', 'fe80::/64', 'fd00::/8']) assert.equal(C.localNet(s), true, s);
  for (const s of ['0.0.0.0/0', '10.0.0.0/7', '8.8.8.8', '172.32.0.0/16', '2001:db8::/32', 'fc00::/6', 'nonsense']) assert.equal(C.localNet(s), false, s);
});

test('advancedRules: routing.rules.raw / rule_sets read-only, marked as clash_formatter drops them', () => {
  const vals = VALUES();
  vals.routing.rules.raw = ['AND,((DST-PORT,443),(NETWORK,UDP)),REJECT', 'DST-PORT,22,DIRECT', 'MATCH,COMPATIBLE', 'nonsense', '  '];
  vals.routing.rules.rule_sets = ['ads,REJECT', 'RULE-SET,cn,DIRECT', { provider: 'media', target: 'proxy' }, { name: 'x', policy: 'direct' }, { target: 'proxy' }, 'a,b,c'];
  const a = C.advancedRules(vals, []);
  assert.equal(a.known, true); assert.equal(a.hidden, false);
  assert.deepEqual(a.rows.map(r => [r.kind, r.text, r.dropped, r.policy]), [
    ['raw', 'AND,((DST-PORT,443),(NETWORK,UDP)),REJECT', false, 'REJECT'],
    ['raw', 'DST-PORT,22,DIRECT', true, 'DIRECT'],
    ['raw', 'MATCH,COMPATIBLE', true, 'COMPATIBLE'],
    ['raw', 'nonsense', true, null],
    ['set', 'RULE-SET,ads,REJECT', false, 'REJECT'],
    ['set', 'RULE-SET,cn,DIRECT', true, 'DIRECT'],
    ['set', 'RULE-SET,media,GENERAL', false, 'GENERAL'],
    ['set', 'RULE-SET,x,DIRECT', true, 'DIRECT'],
    ['set', 'RULE-SET,?,GENERAL', true, null],
    ['set', 'a,b,c', true, null],
  ]);
  assert.equal(C.advancedRules(vals, ['routing.rules.raw']).hidden, true, 'a list withheld whole');
  assert.deepEqual(C.advancedRules({}, []), { rows: [], hidden: false, known: false });
  // one string of lines, as _as_string_list reads it
  vals.routing.rules.raw = 'DST-PORT,8080,REJECT\nPROCESS-PATH,C:\\x.exe,GENERAL';
  vals.routing.rules.rule_sets = [];
  assert.deepEqual(C.advancedRules(vals, []).rows.map(r => r.text), ['DST-PORT,8080,REJECT', 'PROCESS-PATH,C:\\x.exe,GENERAL']);
});

test('toChanges: sources: rows by id and kind; a new or replaced value is SecretEdit set; untouched values are kept (no SecretEdit); a deleted row is left out (never clear); values never carry the secret', () => {
  const r = changeOf('sources', d => {
    d.sources = [{ ...d.sources[0], v: 'https://github.com/a/b' }, { id: 'new-src-1', t: 'tg', hint: null, enabled: true, v: '@example_share' }];
  });
  assert.deepEqual(r.changes, [{ section: 'sources', values: { items: [{ id: 'src-1', kind: 'github_repos', enabled: true }, { id: 'new-src-1', kind: 'telegram_channels', enabled: true }] },
    secrets: [{ field: 'value', target_id: 'src-1', action: 'set', value: 'https://github.com/a/b' }, { field: 'value', target_id: 'new-src-1', action: 'set', value: '@example_share' }] }]);
  assert.ok(!JSON.stringify(r.changes[0].values).includes('example_share'));
  const keep = changeOf('sources', d => { d.sources = d.sources.map(s => ({ ...s, enabled: true })); });
  assert.equal(keep.changes[0].secrets, undefined, 'keep: no SecretEdit at all');
  assert.ok(!JSON.stringify(keep).includes('"clear"'));
});

test('toChanges: keys of one section merge into one change; typed-but-not-added (pend:*) and the alert drafts without an adapter stay drafts', () => {
  const b = { ...base(), 'pend:rule': '', 'ch:telegram': { on: false } };
  const d = { ...C.clone(b), lan: true, portMixed: 7000, portSub: 7001, 'pend:rule': 'suffix:x.org', 'ch:telegram': { on: true } };
  const r = C.toChanges({ draft: d, base: b, keys: ['lan', 'portMixed', 'portSub', 'pend:rule', 'ch:telegram'] });
  assert.deepEqual(r.changes, [{ section: 'network', values: { allow_lan: true, mihomo_mixed_port: 7000, subscription_port: 7001 } }]);
  assert.deepEqual(r.skipped, ['pend:rule', 'ch:telegram']);
  assert.deepEqual(C.sectionsOf(r.sent), { lan: 'network', portMixed: 'network', portSub: 'network' });
});

test('toChanges: the adapter hook (the alerts section for T-P4-05e): values merge, SecretEdit set / clear / keep pass through, an unknown section is refused', () => {
  const adapt = k => (k === 'ch:telegram' ? { section: 'alerts', values: { channels: [{ id: 'tg', type: 'telegram', enabled: true }] },
    secrets: [{ field: 'bot_token', target_id: 'tg', action: 'set', value: '123:abc' }, { field: 'chat_id', target_id: 'tg', action: 'keep' }] }
    : k === 'ch:ntfy' ? { section: 'alerts', values: {}, secrets: [{ field: 'token', target_id: 'nt', action: 'clear' }] }
      : k === 'ch:bad' ? { section: 'nowhere', values: {} } : null);
  const r = C.toChanges({ draft: {}, base: {}, keys: ['ch:telegram', 'ch:ntfy', 'ch:bad'], adapt });
  assert.deepEqual(r.changes, [{ section: 'alerts', values: { channels: [{ id: 'tg', type: 'telegram', enabled: true }] },
    secrets: [{ field: 'bot_token', target_id: 'tg', action: 'set', value: '123:abc' }, { field: 'chat_id', target_id: 'tg', action: 'keep' }, { field: 'token', target_id: 'nt', action: 'clear' }] }]);
  assert.deepEqual(r.skipped, ['ch:bad']);
  assert.deepEqual(r.sent, { alerts: ['ch:telegram', 'ch:ntfy'] });
});

/* ------------------------------------------------------------------ an older projection's rule strings */
test('ruleFromBackend: an older projection\'s strings (clash_formatter._targeted_rule forms) → page literals, or kept as stored', () => {
  const cases = [
    ['suffix:example.com', 'suffix:example.com', false], ['DOMAIN:api.example.com', 'domain:api.example.com', false],
    ['DOMAIN-SUFFIX,example.com', 'suffix:example.com', false], ['IP-CIDR,203.0.113.0/24,no-resolve', 'ip-cidr:203.0.113.0/24', false],
    ['GEOIP,CN,no-resolve', 'geoip:CN', false], ['DOMAIN-KEYWORD,ads', 'keyword:ads', false], ['PROCESS-NAME,x.exe', 'process:x.exe', false],
    ['203.0.113.7', 'ip-cidr:203.0.113.7', false], ['2001:db8::1', 'ip-cidr:2001:db8::1', false], ['example.org', 'suffix:example.org', false],
    ['+.example.org', 'suffix:example.org', false], ['ip-cidr6:2001:db8::/32', 'ip-cidr:2001:db8::/32', false],
    ['DOMAIN-SUFFIX,example.com,DIRECT', 'DOMAIN-SUFFIX,example.com,DIRECT', true], ['RULE-SET,x', 'RULE-SET,x', true], ['not a rule', 'not a rule', true],
  ];
  for (const [s, v, raw] of cases) assert.deepEqual(C.ruleFromBackend(s), { v, raw }, s);
});

/* ------------------------------------------------------------------ outcome → bar and rows */
const S = (section, saved, applied, effect = 'none', error = null) => ({ section, saved, applied, effect, error });
const E = (code, extra = {}) => ({ code, message_key: `errors.${code}`, params: {}, ...extra });
const OUTCOMES = [
  // name, input, ctx, kind, rows, bar outcome
  ['S-04 applied (reload)', { result: { rev: 8, sections: [S('network', true, true, 'reload'), S('ui', true, true, 'ui')] } }, { run: 'running' }, 'applied', { network: 'applied', ui: 'applied' }, 'applied'],
  ['S-13 recheck', { result: { rev: 8, sections: [S('latency', true, true, 'recheck')] } }, { run: 'running' }, 'applied', { latency: 'applied' }, 'applied'],
  ['S-02 deferred while starting', { result: { rev: 8, sections: [S('network', true, false, 'deferred')], pending: { intent_id: 'i', config_rev: 8, generation: 1, state: 'pending', effect: 'restart', error: null } } }, { run: 'starting' }, 'pending', { network: 'pending' }, 'pending'],
  ['S-03 stopped', { result: { rev: 8, sections: [S('network', true, true, 'none')] } }, { run: 'stopped', sent: { network: ['lan'] } }, 'next_start', { network: 'next_start' }, 'applied'],
  ['S-03 stopped, ui only', { result: { rev: 8, sections: [S('ui', true, true, 'ui')] } }, { run: 'stopped', sent: { ui: ['lang'] } }, 'applied', { ui: 'applied' }, 'applied'],
  ['S-01 saved, not applied', { error: E('io', { result: { rev: 8, sections: [S('network', true, false, 'restart', E('io'))] } }) }, { run: 'running' }, 'not_applied', { network: 'not_applied' }, 'not_applied'],
  ['S-04 partial', { error: E('io', { result: { rev: 8, sections: [S('ui', true, true, 'ui'), S('network', false, false, 'none', E('io'))] } }) }, { run: 'running' }, 'not_applied', { ui: 'applied', network: 'failed' }, 'not_applied'],
  ['S-05 hold at a field', { error: E('validation', { hold: true, field: 'network.mihomo_mixed_port', result: { rev: 7, sections: [S('network', false, false)] } }) }, { run: 'running' }, 'held', { network: 'failed' }, 'held'],
  ['S-06 base_rev stale', { error: E('conflict', { hold: true, reason_code: 'base_rev_mismatch', result: { rev: 9, sections: [S('network', false, false)] } }) }, { run: 'running' }, 'conflict', { network: 'failed' }, 'conflict'],
  ['S-07 lifecycle_busy (D-sem 3)', { error: E('lifecycle_busy', { hold: true, reason_code: 'recovery_in_progress', result: { rev: 7, sections: [S('network', false, false)] } }) }, { run: 'starting' }, 'held', { network: 'failed' }, 'held'],
  ['recovery gate (conflict / recovery_pending, no result)', { error: E('conflict', { reason_code: 'recovery_pending' }) }, { run: 'starting' }, 'failed', {}, 'failed'],
  ['failed, nothing saved', { error: E('io', { result: { rev: 7, sections: [S('network', false, false, 'none', E('io'))] } }) }, { run: 'running' }, 'failed', { network: 'failed' }, 'failed'],
  ['S-22 sources hold', { error: E('validation', { hold: true, field: 'sources.items[1].value', reason_code: 'source_duplicate', result: { rev: 7, sections: [S('sources', false, false)] } }) }, { run: 'running' }, 'held', { sources: 'failed' }, 'held'],
];
for (const [name, input, ctx, kind, rows, bar] of OUTCOMES) {
  test(`outcome: ${name} → ${kind}; bar ${bar}; rows ${JSON.stringify(rows)}`, () => {
    const o = C.outcome(input, ctx);
    assert.equal(o.kind, kind);
    assert.deepEqual(Object.fromEntries(Object.entries(o.rows).map(([s, r]) => [s, r.state])), rows);
    const f = C.barFace(o);
    assert.equal(f.outcome, bar);
    assert.ok(ZH[f.text], `bar text ${f.text} in the catalog`);
    if (f.retry) assert.ok(ZH[f.retry]);
    for (const r of Object.values(o.rows)) assert.ok(ZH[C.rowLabel(r.state)], `row label ${r.state}`);
    if (kind === 'held') assert.equal(o.field, input.error.field || null);
  });
}

test('barFace: the applied wording follows the heaviest effect; every row label (and external) is a message', () => {
  assert.equal(C.barFace({ kind: 'applied', effect: 'restart' }).text, 'core.savebar.done.restart');
  assert.equal(C.barFace({ kind: 'applied', effect: 'reload' }).text, 'core.savebar.done.reload');
  assert.equal(C.barFace({ kind: 'applied', effect: 'recheck' }).text, 'settings.save.done.recheck');
  assert.equal(C.barFace({ kind: 'applied', effect: 'instant' }).text, 'core.savebar.done.instant');
  for (const s of ['applied', 'pending', 'not_applied', 'next_start', 'failed', 'external']) assert.ok(ZH[C.rowLabel(s)], s);
  for (const id of ['core.savebar.saved_pending', 'core.savebar.not_applied', 'core.savebar.conflict', 'core.savebar.conflict_reason']) assert.ok(ZH[id], id);
});

test('pendingState: config.changed.pending relabels the rows it holds', () => {
  assert.equal(C.pendingState({ state: 'applied' }), 'applied');
  assert.equal(C.pendingState({ state: 'failed' }), 'not_applied');
  // the ledger's merge rule: a superseded intent's work is carried by its successor (still pending);
  // a cancelled one (the service stopped) loads the saved file on the next start
  assert.equal(C.pendingState({ state: 'superseded' }), 'pending');
  assert.equal(C.pendingState({ state: 'cancelled' }), 'next_start');
  for (const s of ['pending', 'applying']) assert.equal(C.pendingState({ state: s }), 'pending', s);
  assert.equal(C.pendingState(null), null);
  assert.ok(ZH[C.rowLabel(C.pendingState({ state: 'cancelled' }))]);
});

test('pendingFollows: a rebased intent (new intent_id, config_rev ≥ the save\'s rev) still carries the rows (fix M4)', () => {
  const track = { ids: new Set(['intent-1']), rev: 8 };
  assert.equal(C.pendingFollows({ intent_id: 'intent-1', config_rev: 8 }, track), true, 'the same intent');
  assert.equal(C.pendingFollows({ intent_id: 'intent-2', config_rev: 9 }, track), true, 'the successor after a second save / gateway.select');
  assert.equal(C.pendingFollows({ intent_id: 'intent-0', config_rev: 7 }, track), false, 'an older intent');
  assert.equal(C.pendingFollows({ intent_id: 'intent-2', config_rev: 9 }, null), false, 'nothing tracked');
  assert.equal(C.pendingFollows(null, track), false);
});

test('fieldKey: an Error.field lands on its row (holds, port conflicts after B-26(3), source batches)', () => {
  const table = { 'network.mihomo_mixed_port': 'portMixed', 'network.subscription_port': 'portSub', 'network.subscription.port': 'portSub',
    'network.system_proxy.bypass[2]': 'bypass', 'network.egress_recovery.retry_source_refresh': 'egressRetry', 'network.egress_recovery.enabled': 'egress',
    'killswitch.additional_whitelist[0]': 'ksExtra', 'killswitch.dns_whitelist': 'ksDnsList', 'dns.mode': 'dnsMode', 'dns.nameserver[1]': 'dnsList',
    'routing.rules.proxy[3]': 'rules', 'routing.rule_order': 'rules', 'routing.rule_list': 'rules', 'routing.rule_list[3].of[1].value': 'rules',
    'routing.providers': 'ruleSets', 'routing.providers[1].url': 'ruleSets', 'sources.items[1].value': 'sources',
    'bastion.min_alive_nodes': 'bastionMin', 'ui.language': 'lang', 'ui.start_in_tray_on_autostart': 'tray', 'latency.primary_gateway_max_latency_ms': 'latency',
    'alerts.notify.sound': 'nSound', 'alerts.notify.events': 'nConn', 'alerts.notify.enabled': 'nOn', '': null };
  for (const [f, k] of Object.entries(table)) assert.equal(C.fieldKey(f), k, f);
});

test('counts and effects: a list counts its items, a scalar one; the bar\'s effect is the heaviest estimate', () => {
  const b = base();
  const d = { ...C.clone(b), rules: [{ id: 'n1', act: 'proxy', v: 'suffix:a.org' }, { id: 'n2', act: 'proxy', v: 'suffix:b.org' }, ...b.rules.slice(1)], lan: true };
  assert.equal(C.countOf('rules', d, b), 3, 'two added, one removed');
  assert.equal(C.countChanges(['rules', 'lan'], d, b), 4);
  assert.equal(C.heaviest(['lan', 'autoExit'], d), 'restart');
  assert.equal(C.heaviest(['latency'], d), 'recheck');
  assert.equal(C.effectOf('dnsList', { dnsMode: 'custom' }), 'reload');
  assert.equal(C.effectOf('dnsList', { dnsMode: 'auto' }), 'instant');
});

/* ------------------------------------------------------------------ in the browser */
let dir, browser, run;
before(async () => {
  if (skip) return;
  const { buildTarget, writeOutput } = await import('../../build.mjs');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-settings-changes-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  run = await import('../render/run.mjs');
  browser = await run.launch();
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});
const scenario = name => JSON.parse(read(`test/fixtures/scenarios/${name}.json`));

test('dirtyCount() equals the save bar\'s count and the page\'s countChanges; drafts the bar no longer holds still count in the quit question', { skip }, async () => {
  const cell = await run.openCell(browser, { distDir: join(dir, 'design'), scenario: scenario('settings_focus'), lang: 'zh_CN', size: '1280x800', page: 'settings' });
  const { page } = cell;
  await page.click('.switch[data-k="lan"]'); await run.runAction(page, { page_ms: 400 });
  await page.fill('#set-rule-in', 'example.net'); await run.runAction(page, { page_ms: 400 });
  await page.fill('#set-tok-bypass input', 'nas.local'); await page.keyboard.press('Enter'); await run.runAction(page, { page_ms: 400 });
  const s = await page.evaluate(() => ({ bar: window.T.ui.saveBarCurrent.count, dirty: window.T.ui.saveBar.dirtyCount(), quit: window.T.shellc.unsaved() }));
  assert.equal(s.bar, 3, 'lan + the typed rule + one bypass entry');
  assert.equal(s.dirty, s.bar);
  assert.equal(s.quit, s.bar, 'no double count while the bar holds the drafts');
  // a forced navigation (the shell's host:navigate) leaves the drafts without a bar
  await page.evaluate(() => window.T.go('logs', { force: true })); await run.runAction(page, { page_ms: 800 });
  const away = await page.evaluate(() => ({ bar: window.T.ui.saveBar.dirtyCount(), quit: window.T.shellc.unsaved() }));
  assert.equal(away.bar, 0);
  assert.equal(away.quit, 3, 'the page\'s drafts still count (T.shellc.registerDraft)');
  assert.deepEqual(cell.errors, []);
  const t = await page.evaluate(() => window.__TESSERACT_DESIGN__.transport().failures);
  assert.deepEqual(t, []);
  await cell.context.close();
});
