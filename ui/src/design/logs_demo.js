/* ==========================================================================
   design/logs_demo.js — the logs page's demo records (T-P4-04d)
   Moved out of pages/logs.js so that the page holds no Chinese literal (the cjk gate,
   P4 architecture §3.9) while the design target keeps showing exactly what it showed:
     seedRecords()   the page's two-run seed (design/demo-data.js seedLogRecords, the same
                     records the simulator serves as logs.tail) in the page's record shape
     fromSim()       a simulator record (sim:log / T.sim.logs) → a page record: the event key,
                     the plain-language detail and an English raw line for each sim title
     FIX             the reader's suggestion cards (B-22: not in the v1 contract)
     debugLine()     the Mihomo DEBUG lines streamed while capture is on (sim:tick)
   Records carry the v1 ids (PROTOCOL §5.5, §16): source TESSERACT / MIHOMO / UNKNOWN, category
   runtime / routing / health / circuit_breaker / update / configuration / connection / alert /
   other, plus the prototype's `verification` (验证, no runtime category; 映射_logs #8). The
   wording below is demo data (log titles and details are the backend's text in the real app):
   it is rendered under translate="no" and never enters the catalog.
   The page imports this module and uses it only inside if (__DEMO__): the app target's
   bundle drops it. T-P4-05c replaces the page's seed with logs.tail / logs.batch.
   design/demo-data.js is reached by a require() inside if (__DEMO__), not by a static import:
   esbuild follows every static import into the bundle's inputs even when the code is
   dropped, and the app target must not list demo-data.js (design_sim.test.mjs, T-P4-02b).
   ========================================================================== */

/* the prototype's category names (demo-data.js, the sim table below) → v1 ids */
const CAT = { 运行状态: 'runtime', 路由: 'routing', 层级健康: 'health', 熔断: 'circuit_breaker', 验证: 'verification',
  更新: 'update', 配置: 'configuration', 连接: 'connection', 运行告警: 'alert', 常规: 'other' };
const SRC = { Tesseract: 'TESSERACT', Mihomo: 'MIHOMO' };
const catId = c => CAT[c] || 'other';
// a record that already carries v1 ids (the render tests' ASCII fixture records) keeps them
const V1_SRC = new Set(['TESSERACT', 'MIHOMO', 'ALERT', 'UNKNOWN']);
const V1_CAT = new Set(['runtime', 'routing', 'health', 'circuit_breaker', 'update', 'configuration', 'connection', 'alert', 'other']);
const V1_LEVEL = new Set(['debug', 'info', 'warn', 'error', 'critical']);
const srcId = s => SRC[s] || (V1_SRC.has(s) ? s : 'UNKNOWN');

/** The page's seed: two runs, ~140 records (yesterday evening + the current run), oldest first. */
export function seedRecords(T, D, BOOT) {
  if (__DEMO__) {
    const { seedLogRecords } = require('./demo-data.js');
    return seedLogRecords(T, D, BOOT).map(r => Object.assign({}, r, { src: srcId(r.src), cat: catId(r.cat) }));
  }
  return [];
}

/* ---- sim records → page records (plain-language detail, verbatim raw line) ---- */
const SIMCAT = { runtime: '运行状态', source: '更新', exit: '验证', proxy: '配置', killswitch: '熔断', health: '层级健康', traffic: '常规', dns: '连接', tier: '路由' };
const latOf = s => { const m = /(\d+) ms/.exec(s || ''); return m ? m[1] : '—'; };
const nodeOf = s => { const p = String(s || '').split(' · '); return p.length > 1 ? p[1] : p[0]; };
const SIM = {
  '正在启动服务': { cat: '运行状态', key: 'runtime.start_requested', runStart: true, detail: () => '正在收集节点来源并准备代理核心。', raw: (s, gen) => `Desktop runtime start requested (generation=${gen()})` },
  '节点来源刷新完成': { cat: '更新', key: 'update.source_completed', raw: s => { const m = /(\w+) · (\d+)\/(\d+)/.exec(s.detail) || []; return `Node source refresh finished: tier=${(m[1] || 'unknown').toLowerCase()} ok=${m[2] || 0} total=${m[3] || 0}`; } },
  '正在验证最终出口': { event: false, cat: '验证', raw: s => `Verifying final exit via "${s.detail}"` },
  '代理出口已可用': { level: 'ok', cat: '路由', key: 'tier.egress_ready', detail: s => `出口校验通过，流量经 ${s.detail.split(' · ')[0]} 离开（${s.detail.split(' · ')[1] || '3/3'} 可用）。`, raw: () => 'Egress ready: exit="东京 Gateway 01" members=3/3 probe=203.0.113.24:443' },
  '代理核心已启动': { cat: '运行状态', key: 'runtime.mihomo_ready', src: 'Mihomo', raw: () => 'RESTful API listening at: 127.0.0.1:25500' },
  '系统代理已接管': { cat: '配置', key: 'proxy.system_set', raw: s => `System proxy set: ${s.detail} bypass=localhost;127.*;10.*;192.168.*` },
  '熔断保护已布防': { cat: '熔断', key: 'killswitch.armed', raw: () => 'Killswitch armed (policy=block-on-total-failure)' },
  '出口已确认': { level: 'ok', cat: '验证', key: 'egress.verified', raw: () => 'Exit identity: 203.0.113.24 AS64500 JP/Tokyo (auto, via runtime readiness probe)' },
  '出口已验证': { level: 'ok', cat: '验证', key: 'egress.verified', raw: () => 'Exit verified: 203.0.113.24 AS64500 JP/Tokyo (manual full check)' },
  '服务已停止': { cat: '运行状态', key: 'runtime.stopped', runEnd: true, raw: () => 'Desktop runtime stopped (reason=user)' },
  '已取消启动': { cat: '运行状态', key: 'runtime.start_cancelled', runEnd: true, detail: () => '启动在完成前被取消，没有接管任何流量。', raw: () => 'Desktop runtime start cancelled by user' },
  '正在重启代理核心': { cat: '运行状态', key: 'runtime.core_restarting', raw: () => 'Restarting core (reason=user)' },
  '代理核心已重启': { cat: '运行状态', key: 'runtime.core_restarted', src: 'Mihomo', detail: () => '核心重新加载完成，连接已恢复。', raw: () => 'Initial configuration complete, total time: 1.12s' },
  'Vanguard 延迟升高': { cat: '层级健康', key: 'tier.vanguard_slow', raw: () => 'Latency over ceiling: proxy="香港 IEPL 03" delay=820ms ceiling=800ms' },
  '流量由 Sentinel 承载': { cat: '路由', key: 'tier.sentinel_active', raw: () => 'Tier mode changed: tier1_degraded -> tier2_active (tier1=0/2 tier2=4)' },
  'Vanguard 已恢复': { level: 'ok', cat: '路由', key: 'tier.vanguard_restored', raw: () => 'Tier mode changed: tier2_active -> tier1_healthy (tier1=2/2 tier2=4)' },
  'Vanguard 延迟已恢复': { level: 'ok', cat: '层级健康', key: 'tier.vanguard_healthy', raw: () => 'Latency back under ceiling: proxy="香港 IEPL 03" delay=44ms ceiling=800ms' },
  '正在切换网络模式': { cat: '配置', key: 'proxy.mode_switching', raw: s => `Switching mode: ${s.detail}` },
  '网络模式已切换': { cat: '配置', key: 'proxy.mode_switched', raw: s => `Mode switched: ${s.detail}` },
  // routine lines: evidence only (原始日志), never key events
  // (a node that is down has no latency: its check is a failure line, not "delay=—ms")
  '健康检查通过': { event: false, cat: '层级健康', level: s => (latOf(s.detail) === '—' ? 'warn' : 'info'),
    raw: s => { const g = String(s.detail).split(' · ')[0].toUpperCase(), n = nodeOf(s.detail), l = latOf(s.detail);
      return l === '—' ? `health check failed: group=${g} proxy="${n}" error: i/o timeout` : `health check ok: group=${g} proxy="${n}" delay=${l}ms`; } },
  '流量采样正常': { event: false, cat: '常规', raw: s => { const m = /↓ ([\d.]+ \S+) · ↑ ([\d.]+ \S+)/.exec(s.detail) || []; return `[Stats] down=${(m[1] || '0').replace(' ', '')} up=${(m[2] || '0').replace(' ', '')} conns=${30 + (s.ts % 17)}`; } },
  '复检通过': { event: false, cat: '层级健康', raw: () => 'recheck ok: group=SENTINEL proxy="首尔 01" delay=49ms' },
  'DNS 解析正常': { event: false, cat: '连接', raw: () => '[DNS] resolver ok: https://dns.example.net/dns-query avg=24ms' },
  // a key event marks the change (the node was suspended); the rechecks that keep failing
  // afterwards are evidence only (the page demotes a repeat within the same run)
  '节点暂不可用': { cat: '层级健康', key: 'tier.node_suspended', once: true, detail: () => 'Sentinel · 法兰克福 04 连接超时，已暂停使用；恢复后会自动加回。', raw: () => 'health check failed: proxy="法兰克福 04" dial tcp 198.51.100.44:8388: i/o timeout; suspended',
    again: () => 'recheck failed: proxy="法兰克福 04" dial tcp 198.51.100.44:8388: i/o timeout; still suspended' },
};

/**
 * A simulator record ({id, ts, level, source, category, title, detail, raw, key?}) → a page record.
 * lite: no raw line (a record kept for the sidebar badge only). gen() returns the next
 * runtime generation (the page's counter) for the start line.
 * The simulator's own records (sim.js) never carry a key, an empty title, a debug / critical
 * level or a v1 category other than runtime / health (which mean the same in both), so for them
 * this is the prototype's mapping unchanged; a record in v1 terms (the render tests' ASCII
 * fixture records, ui/test/render/cases/logs_i18n.mjs) keeps its key, level, source and category,
 * and an empty title makes it a raw line (no key event).
 */
export function fromSim(s, lite, gen) {
  const m = SIM[s.title] || {};
  const ev = m.event !== false && !!s.title;
  const lv = typeof m.level === 'function' ? m.level(s) : m.level;
  const src = m.src || s.source;
  return {
    ts: s.ts, level: lv || (V1_LEVEL.has(s.level) ? s.level : 'info'), once: !!m.once, again: m.again ? m.again(s) : null,
    src: src ? srcId(src) : 'UNKNOWN', cat: m.cat ? catId(m.cat) : V1_CAT.has(s.category) ? s.category : catId(SIMCAT[s.category] || '常规'),
    key: ev ? m.key || s.key || 'sim.' + s.category : null, title: ev ? s.title : null,
    detail: ev ? (m.detail ? m.detail(s) : s.detail) : null,
    raw: lite ? '' : m.raw ? m.raw(s, gen || (() => 0)) : s.raw, runStart: !!m.runStart, runEnd: !!m.runEnd,
    meta: { logger_name: srcId(s.source) === 'MIHOMO' ? 'mihomo.stream' : `tesseract.${s.category}`, capture_kind: srcId(s.source) === 'MIHOMO' ? 'controller_stream' : 'app_logger' },
  };
}

/* what the reader offers after "what happened": the fix, and a jump to where it is made
   (B-22: the contract has no such field; demo records carry fix: <key>) */
export const FIX = {
  port: { title: '怎么处理', text: '关闭占用这个端口的程序后再启动，或换一个代理端口。', label: '在配置中修改端口', go: 'settings', params: { focus: 'ports' } },
  sub403: { title: '怎么处理', text: '在订阅商处重新复制订阅地址，然后在 Sentinel 里更新这个订阅。', label: '在层级中更新订阅', go: 'profiles', params: { tier: 'sentinel' } },
  drift: { title: '怎么处理', text: '另一个代理软件可能也在改系统代理。关掉它的「系统代理」开关，或改用全局模式。', label: '查看网络模式', go: 'settings', params: { focus: 'mode' } },
  selfheal: { title: '影响', text: '不影响当前连接：现有节点会继续使用，下次刷新时自动重试。' },
};

/* Mihomo DEBUG capture: the extra lines streamed while it is on (one every other sim tick) */
const DBG = [
  () => `[DNS] cache hit api.example.com --> 198.51.100.12 (ttl=${120 + Math.floor(Math.random() * 180)})`,
  () => `[Sniffer] sniffed TLS host=cdn.example.net from 127.0.0.1:${50000 + Math.floor(Math.random() * 15000)}`,
  () => `[TCP] 127.0.0.1:${50000 + Math.floor(Math.random() * 15000)} --> static.example.org:443 dial PROXY[香港 IEPL 03] ${38 + Math.floor(Math.random() * 20)}ms`,
  () => `[UDP] 127.0.0.1:${50000 + Math.floor(Math.random() * 15000)} --> 192.0.2.53:53 matched DIRECT (rule=IP-CIDR 192.0.2.0/24)`,
];
/** the i-th DEBUG line as a page record (ts: now) */
export function debugRecord(i, ts) {
  return { ts, level: 'debug', src: 'MIHOMO', cat: 'connection', raw: DBG[i % DBG.length](), meta: { logger_name: 'mihomo.stream', capture_kind: 'controller_stream', level: 'debug' } };
}
