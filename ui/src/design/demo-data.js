/* ==========================================================================
   design/demo-data.js — synthetic v1 data for the design target's simulator
   (T-P4-02b). Plausible, not real: RFC 5737 / 3849 addresses, example domains.
   Everything here is answered or published by design/sim.js through FakeTransport
   in the v1 shapes of PROTOCOL §5, §11 and §16; nothing reaches the app target.
   ========================================================================== */

/* ---- the log history (pages/logs.js seed(), two runs, ~140 records) ----
   Ported from the prototype's run-log seed so that logs.tail / logs.batch carry it once the
   logs page reads the runtime (T-P4-05c); until then the page still seeds itself and the
   simulator's own records reach it as sim:log (design/sim.js). Records keep the page's shape
   ({ts, level, src, cat, key, title, detail, raw, ...}); frames.js turns them into v1 Log rows
   (fields the contract has no place for — fix hints, metadata, run markers — are dropped). */
export function seedLogRecords(T, D, BOOT) {
  const pad = (n, w) => String(n).padStart(w || 2, '0');
  const ymd = ts => { const d = new Date(ts); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const BASTION_NOTE = 'Bastion 使用公共节点，请避免登录或提交敏感信息。';
  const rnd = T.util.rng(26092026);
  const L = [];            // [ts, spec]
  const add = (base, at, lv, src, cat, key, title, detail, raw, x) => L.push(Object.assign({ ts: base + Math.round(at * 1000) + Math.floor(rnd() * 900), level: lv, src, cat, key, title, detail, raw }, x || {}));
  const sp = () => 50000 + Math.floor(rnd() * 15000);
  function noise(base, at, kind, lv, targets, gap, via) {
    const list = [];
    targets.forEach(([h, ip, port, n]) => { for (let i = 0; i < n; i++) list.push([h, ip, port]); });
    for (let i = list.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [list[i], list[j]] = [list[j], list[i]]; }
    list.forEach(([h, ip, port], i) => {
      const raw = kind === 'timeout' ? `[TCP] dial ${via || 'DIRECT'} 127.0.0.1:${sp()} --> ${h}:${port} error: dial tcp ${ip}:${port}: i/o timeout`
        : kind === 'refused' ? `[TCP] dial ${via} 127.0.0.1:${sp()} --> ${h}:${port} error: connect: connection refused`
        : `[TCP] 127.0.0.1:${sp()} --> ${h}:${port} match DomainSuffix(${h.split('.').slice(-2).join('.')}) using ${via}`;
      add(base, at + i * gap, lv, 'Mihomo', '连接', null, null, null, raw, { noise: kind, meta: { logger_name: 'mihomo.stream', capture_kind: 'controller_stream', conn: { host: h, port } } });
    });
  }
  const TRACE = 'Traceback (most recent call last):\n  File "collector.py", line 214, in refresh_source\n    body = fetch(source.url, timeout=8.0)\n  File "collector.py", line 88, in fetch\n    raise SourceTimeout(host)\nSourceTimeout: sub.nebula.example.com timed out after 8.0s';

  /* run 1: yesterday 21:58 → 23:47 (a rough evening: fallback, Bastion, kill switch, recovery),
     preceded by a start that failed on a taken port (the user closed the other program and retried) */
  const d1 = new Date(BOOT - D.session.uptime * 1000); d1.setHours(0, 0, 0, 0);
  const R1 = d1.getTime() - (2 * 3600 + 2 * 60) * 1000;
  const R0 = R1 - 94 * 1000;
  add(R0, 0, 'info', 'Tesseract', '运行状态', 'runtime.start_requested', '正在启动服务', '正在收集节点来源并准备代理核心。', 'Desktop runtime start requested (generation=40)', { runStart: true, gen: 40 });
  add(R0, 0.8, 'error', 'Mihomo', '运行状态', null, null, null, 'Start Mixed(http+socks) server error: listen tcp 127.0.0.1:7890: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.');
  add(R0, 1.4, 'error', 'Tesseract', '运行状态', 'runtime.desktop_failed', '服务启动失败', '原因：代理端口 127.0.0.1:7890 正被其他程序占用。服务没有启动，也没有接管任何流量。', 'Desktop runtime failed: port in use (endpoints=127.0.0.1:7890)', { runEnd: true, runFail: true, fix: 'port', meta: { event_params: { reason: 'port_in_use', endpoints: ['127.0.0.1:7890'] }, logger_name: 'desktop_runtime' } });
  add(R1, 0, 'info', 'Tesseract', '运行状态', 'runtime.start_requested', '正在启动服务', '正在收集节点来源并准备代理核心。', 'Desktop runtime start requested (generation=41)', { runStart: true, gen: 41 });
  add(R1, 1.2, 'info', 'Mihomo', '运行状态', null, null, null, 'Start initial configuration in progress');
  add(R1, 2.3, 'info', 'Mihomo', '运行状态', 'runtime.mihomo_ready', '代理核心已启动', '核心初始化完成，用时 1.84 秒。', 'Initial configuration complete, total time: 1.84s');
  add(R1, 2.4, 'info', 'Mihomo', '运行状态', null, null, null, 'RESTful API listening at: 127.0.0.1:25500');
  add(R1, 2.5, 'info', 'Mihomo', '运行状态', null, null, null, 'Mixed(http+socks) proxy listening at: 127.0.0.1:7890');
  add(R1, 3.1, 'info', 'Tesseract', '常规', 'dns.resolver_qualified', 'DNS 解析器已就绪', '本次运行使用加密 DNS · dns.example.net（首选解析器可用）。', 'DNS resolver qualified: doh https://dns.example.net/dns-query (preferred)');
  add(R1, 9.4, 'info', 'Tesseract', '更新', 'update.sources_completed', '节点来源刷新完成', '共处理 6 个来源，1 个失败，得到 2,140 个候选节点。', 'Node source refresh finished: sources=6 failed=1 candidates=2140', { meta: { event_params: { source_total: 6, failed_count: 1, candidates: 2140 } } });
  add(R1, 10.2, 'ok', 'Tesseract', '层级健康', 'tier.vanguard_healthy', 'Vanguard 全部可用', '可用 2/2。', 'Tier health: tier1=2/2 tier2=4/5 tier3=off');
  add(R1, 10.6, 'ok', 'Tesseract', '路由', 'tier.egress_ready', '代理出口已可用', '出口校验通过，流量经 香港 IEPL 03 转发到 东京 Gateway 01。', 'Egress ready: via="香港 IEPL 03" exit="东京 Gateway 01" probe=203.0.113.24:443 rtt=212ms');
  add(R1, 10.9, 'info', 'Tesseract', '熔断', 'killswitch.armed', '熔断保护已布防', '所有层级都不可用时阻断出站，真实 IP 不会外泄。', 'Killswitch armed (policy=block-on-total-failure)');
  add(R1, 11.0, 'info', 'Tesseract', '配置', 'proxy.system_set', '系统代理已接管', '127.0.0.1:7890 · 已写入 Windows 系统代理设置。', 'System proxy set: 127.0.0.1:7890 bypass=localhost;127.*;10.*;192.168.*');
  noise(R1, 14, 'conn', 'info', [['api.example.com', '', 443, 6], ['cdn.example.net', '', 443, 5], ['static.example.org', '', 443, 2], ['mail.example.com', '', 993, 1]], 5.5, 'PROXY[香港 IEPL 03]');
  add(R1, 600, 'info', 'Tesseract', '层级健康', null, null, null, 'Health check round: tier1 2/2 ok avg=44ms tier2 4/5 ok avg=86ms');
  add(R1, 962, 'warn', 'Mihomo', '层级健康', 'mihomo.health_check_forced', '已重新检测全部节点', '经 Vanguard 的连接多次失败，代理核心已重新检测全部节点。', 'because PROXY-FALLBACK failed multiple times, activate health check');
  noise(R1, 963, 'timeout', 'warn', [['raw.cdn.example.net', '198.51.100.7', 443, 24], ['t.example.org', '198.51.100.23', 443, 4], ['git.example.com', '192.0.2.45', 443, 2]], 0.5);
  add(R1, 980, 'warn', 'Tesseract', '层级健康', 'tier.vanguard_degraded', 'Vanguard 部分节点不可用', '可用 1/2，流量仍由 Vanguard 承载。', 'Tier mode changed: tier1_healthy -> tier1_degraded (tier1=1/2 tier2=4 tier3=0)');
  add(R1, 1011, 'warn', 'Tesseract', '路由', 'tier.sentinel_active', '流量由 Sentinel 承载', 'Vanguard 已全部不可用，流量切换到 Sentinel · 首尔 01。', 'Tier mode changed: tier1_degraded -> tier2_active (tier1=0/2 tier2=4)', { meta: { event_params: { from: 'tier1', to: 'tier2', tier2_available: 4 } } });
  add(R1, 1012, 'warn', 'Tesseract', '运行告警', null, null, null, 'Fallback active — Available fallback nodes: 4');
  add(R1, 1013, 'info', 'Tesseract', '运行告警', 'alert.delivered', '提醒已推送到手机', '流量已回退到 Sentinel · 1 个渠道已送达。', 'Alert delivered: kind=fallback channels=1 ok=1');
  [1395, 1515, 1635, 1875].forEach((t, i) => {
    if (i === 0) add(R1, t - 1, 'error', 'Tesseract', '更新', null, null, null, TRACE);
    add(R1, t - 0.6, 'warn', 'Mihomo', '连接', null, null, null, `[DNS] resolve sub.nebula.example.com: dial udp 192.0.2.53:53: i/o timeout`, { noise: 'dns' });
    add(R1, t, 'error', 'Tesseract', '更新', 'update.sources_failed', '节点来源刷新失败', '5 个来源全部失败：超时 ×3，DNS 解析失败 ×1，TLS 握手失败 ×1。现有节点不会被空结果覆盖。', 'Node source refresh failed for all sources', { fix: 'selfheal', meta: { event_params: { source_total: 5, failed_count: 5, failure_kinds: { timeout: 3, dns: 1, tls: 1 } }, logger_name: 'collector' } });
  });
  add(R1, 2131, 'info', 'Tesseract', '层级健康', 'tier3.pool_target_reached', 'Bastion 已达到目标数量', '可用 6/6，应急池已补满。', 'Tier3 pool target reached: live=6 target=6 sources=4');
  add(R1, 2140, 'warn', 'Tesseract', '路由', 'tier.bastion_active', '流量由 Bastion 承载', `Sentinel 也不可用，流量切换到 Bastion · 6 个公共节点可用。${BASTION_NOTE}`, 'Tier mode changed: tier2_active -> tier3_active (tier1=0/2 tier2=0 tier3=6)');
  add(R1, 2150.6, 'critical', 'Tesseract', '层级健康', 'tier.all_down', '所有层级均不可用', '此前由 Bastion 承载；现在 Vanguard、Sentinel 与 Bastion 都没有可用节点。', 'Tier mode changed: tier3_active -> all_down (tier1=0/2 tier2=0 tier3=0)');
  add(R1, 2152, 'error', 'Tesseract', '路由', 'tier.egress_failed', '代理出口不可用', '原因：出口校验超时（203.0.113.24:443）。已暂停转发，等待节点恢复。', 'Egress check failed: probe=203.0.113.24:443 timeout after 5000ms');
  add(R1, 2152.4, 'critical', 'Tesseract', '熔断', 'killswitch.triggered', '熔断保护已触发', '所有层级都不可用，已阻断出站流量，真实 IP 没有外泄。', 'Killswitch TRIGGERED: all tiers down, outbound blocked');
  add(R1, 2153, 'warn', 'Tesseract', '运行告警', 'alert.delivery_deferred', '提醒暂未送达', '出站流量已被熔断保护阻断，恢复连接后会补发。', 'Alert delivery deferred: outbound blocked by killswitch (queued=1)');
  add(R1, 2167, 'ok', 'Tesseract', '层级健康', 'tier.vanguard_healthy', 'Vanguard 全部可用', '可用 2/2，流量已从 Bastion 切回 Vanguard。', 'Tier mode changed: tier3_active -> tier1_healthy (tier1=2/2)');
  add(R1, 2167.3, 'ok', 'Tesseract', '路由', 'tier.egress_restored', '代理出口已恢复', '出口校验重新通过，流量经 香港 IEPL 03 转发到 东京 Gateway 01。', 'Egress restored: via="香港 IEPL 03" exit="东京 Gateway 01"');
  add(R1, 2167.6, 'info', 'Tesseract', '熔断', 'killswitch.released', '熔断保护已解除', '出口已恢复，出站流量恢复正常。', 'Killswitch released (egress restored)');
  add(R1, 2169, 'info', 'Tesseract', '运行告警', 'alert.delivered', '提醒已补发', '熔断与恢复 · 2 条提醒已送达。', 'Alert delivered: queued=2 ok=2');
  noise(R1, 2175, 'conn', 'info', [['api.example.com', '', 443, 4], ['cdn.example.net', '', 443, 4], ['static.example.org', '', 443, 2]], 6, 'PROXY[香港 IEPL 03]');
  add(R1, 3910, 'info', 'Tesseract', '更新', null, null, null, 'Subscription fetch: https://sub.nebula.example.com/api/v1/client/•••••••• 200 (38.2KB)');
  add(R1, 3911, 'info', 'Tesseract', '更新', 'subscription.updated', '订阅已更新', 'Nebula 订阅 · 24 个节点，新增 2 个。', 'Subscription updated: id=nebula nodes=24 added=2 removed=0');
  add(R1, 5412, 'info', 'Tesseract', '更新', 'geodata.updated', '规则数据已更新', `网站分类与 IP 归属地数据已更新到 ${ymd(R1)} 版本。`, `GeoData updated: geoip.dat geosite.dat (release ${ymd(R1)})`);
  add(R1, 6548, 'info', 'Tesseract', '运行状态', 'runtime.stopped', '服务已停止', '本次会话 ↓ 3.12 GB · ↑ 241.60 MB。', 'Desktop runtime stopped (reason=user)', { runEnd: true });
  add(R1, 6549, 'info', 'Mihomo', '运行状态', 'runtime.mihomo_stopped', '代理核心已停止', '代理核心已退出，本次核心会话结束。', 'Mihomo core exited (code=0)');
  add(R1, 6549.5, 'info', 'Tesseract', '配置', 'proxy.system_restored', '系统代理已恢复', '已还原为启动前的系统代理设置。', 'System proxy restored to previous settings');

  /* run 2: the current one (the sim's own history is part of it) */
  const R2 = BOOT - D.session.uptime * 1000 - 3200;
  add(R2, 0, 'info', 'Tesseract', '运行状态', 'runtime.start_requested', '正在启动服务', '正在收集节点来源并准备代理核心。', 'Desktop runtime start requested (generation=42)', { runStart: true, gen: 42 });
  add(R2, 1.1, 'info', 'Mihomo', '运行状态', null, null, null, 'Start initial configuration in progress');
  add(R2, 3.9, 'info', 'Mihomo', '运行状态', null, null, null, 'Mixed(http+socks) proxy listening at: 127.0.0.1:7890');
  add(R2, 8.2, 'info', 'Tesseract', '常规', 'dns.resolver_qualified', 'DNS 解析器已就绪', '本次运行使用加密 DNS · dns.example.net（首选解析器可用）。', 'DNS resolver qualified: doh https://dns.example.net/dns-query (preferred)');
  noise(R2, 12, 'conn', 'info', [['api.example.com', '', 443, 6], ['cdn.example.net', '', 443, 5], ['static.example.org', '', 443, 3], ['push.example.org', '', 5223, 2]], 7, 'PROXY[香港 IEPL 03]');
  add(R2, 600, 'info', 'Tesseract', '层级健康', null, null, null, 'Health check round: tier1 2/2 ok avg=46ms tier2 4/5 ok avg=84ms');
  add(R2, 1600, 'warn', 'Tesseract', '层级健康', 'tier.node_suspended', '节点暂不可用', 'Sentinel · 法兰克福 04 连接超时，已暂停使用；恢复后会自动加回。', 'health check failed: proxy="法兰克福 04" dial tcp 198.51.100.44:8388: i/o timeout; suspended');
  add(R2, 2469, 'warn', 'Mihomo', '层级健康', 'mihomo.health_check_forced', '已重新检测全部节点', '经 Vanguard 的连接多次失败，代理核心已重新检测全部节点。', 'because PROXY-FALLBACK failed multiple times, activate health check');
  noise(R2, 3480, 'refused', 'warn', [['sub.nebula.example.com', '', 443, 12]], 1.1, 'PROXY[首尔 01]');
  [3495, 3615].forEach(t => add(R2, t, 'error', 'Tesseract', '更新', 'update.sources_failed', '节点来源刷新失败', '1 个来源失败：Nebula 订阅地址返回 403，令牌可能已过期。现有节点不会被空结果覆盖。', 'Node source refresh failed: source=nebula status=403 (token rejected)', { fix: 'sub403', meta: { event_params: { source: 'nebula', status: 403 }, logger_name: 'collector' } }));
  add(R2, 4840, 'warn', 'Tesseract', '层级健康', 'tier.vanguard_slow', 'Vanguard 延迟升高', '东京 CN2 01 · 912 ms，超过上限 800 ms；流量仍由 香港 IEPL 03 承载。', 'Latency over ceiling: proxy="东京 CN2 01" delay=912ms ceiling=800ms');
  add(R2, 4930, 'ok', 'Tesseract', '层级健康', 'tier.vanguard_healthy', 'Vanguard 全部可用', '可用 2/2，东京 CN2 01 的延迟已回到 68 ms。', 'Tier health: tier1=2/2 (recovered) tier2=4/5');
  add(R2, 5710, 'warn', 'Tesseract', '配置', 'proxy.system_overridden', '系统代理被其他程序改写', 'Windows 系统代理被改成了 127.0.0.1:1080，已自动改回 127.0.0.1:7890。', 'System proxy drift: expected=127.0.0.1:7890 found=127.0.0.1:1080 (restored)', { fix: 'drift', meta: { event_params: { expected: '127.0.0.1:7890', found: '127.0.0.1:1080', restored: true }, logger_name: 'system_proxy_runtime' } });
  add(R2, 6200, 'info', 'Tesseract', '层级健康', null, null, null, 'Health check round: tier1 2/2 ok avg=44ms tier2 4/5 ok avg=81ms');
  noise(R2, 7000, 'conn', 'info', [['api.example.com', '', 443, 3], ['cdn.example.net', '', 443, 3], ['mail.example.com', '', 993, 2]], 9, 'PROXY[香港 IEPL 03]');
  return L.sort((a, b) => a.ts - b.ts);
}

/* ---- Bastion's public sources (bastion.sources, config.snapshot values.sources) ----
   The contract's Source row has no display name (§13 A9: kind only until the backend adds a
   safe one), so `display` is the kind, as the real projection does. */
export const BASTION_SOURCES = [
  { id: 'src-github-1', kind: 'github_repos', display: 'github_repos', enabled: true },
  { id: 'src-github-2', kind: 'github_repos', display: 'github_repos', enabled: true },
  { id: 'src-telegram-1', kind: 'telegram_channels', display: 'telegram_channels', enabled: true },
  { id: 'src-subscription-1', kind: 'subscription_urls', display: 'subscription_urls', enabled: false },
];

/* ---- alert channels (config.snapshot values.alerts, alerts.delivery, alerts.deliveries) ---- */
export const ALERT_CHANNELS = [
  { id: 'telegram-main', type: 'telegram', label: '手机提醒', enabled: true,
    alert_types: ['WARNING', 'CRITICAL', 'EMERGENCY', 'RECOVERY', 'KILL_SWITCH_ON'],
    config: { enabled: true, timeout: 10, parse_mode: 'HTML', silent: false, disable_link_preview: true } },
  { id: 'desktop', type: 'desktop', label: '桌面通知', enabled: true, alert_types: [], config: { enabled: true, timeout: 5 } },
];
export const SECRET_FIELDS_SET = ['alerts.telegram-main.bot_token', 'alerts.telegram-main.chat_id'];

/* ---- config.snapshot values: the §16 safe projection (bridge_runtime_access.config_projection
   field names), filled with the prototype's defaults. Read-only in the design target:
   settings.apply answers a SaveResult and moves the rev, the values stay as they are. ---- */
export function configValues(F, lang) {
  return {
    network: {
      allow_lan: false, mihomo_mixed_port: 7890, ipv6: true, mode: F.mode,
      tun: { device: 'Tesseract', stack: 'mixed', auto_route: true, auto_detect_interface: true, mtu: 9000, strict_route: true, dns_hijack: ['any:53'] },
      system_proxy: { enabled: true, host: '127.0.0.1', port: 7890, startup_timeout: 10, health_failures_before_restore: 3, bypass: ['localhost', '127.*', '10.*', '192.168.*'] },
      controller: { enabled: true, host: '127.0.0.1', port: 25500 },
      subscription: { host: '127.0.0.1', port: 25501 },
    },
    dns: { mode: 'encrypted', enable: true, respect_rules: true, ipv6: false, listen: '127.0.0.1:1053', enhanced_mode: 'fake-ip',
      fake_ip_range: '198.18.0.1/16', fake_ip_filter: ['*.lan', '*.local'], nameserver: ['192.0.2.53'], default_nameserver: ['192.0.2.53'],
      proxy_server_nameserver: [], fallback: [] },
    routing: { rules: { direct: ['suffix:example.cn'], proxy: [], reject: [], raw: [], rule_sets: [], compatibility_defaults: [] }, rule_order: ['direct', 'proxy', 'reject'], rule_providers: {},
      // 配置整理 P2: the one list and the rule sets the sidecar projects of the lists above
      rule_list: [{ kind: 'suffix', value: 'example.cn', to: 'direct' }], providers: [] },
    latency: { primary_gateway_max_latency_ms: F.latCeiling },
    sources: BASTION_SOURCES.map(s => ({ ...s })),
    alerts: { channels: ALERT_CHANNELS.map(c => ({ ...c, alert_types: c.alert_types.slice(), config: { ...c.config } })) },
    ui: { language: lang, theme: 'light', overview_auto_exit_lookup: true },
    autostart: { enabled: false },
    traffic: { enabled: true, sample_interval: 1, history_hours: 24, retention_hours: 24, max_points: 86400 },
    killswitch: { enabled: F.ksEnabled },
  };
}

/* ---- alerts already recorded before this session (alerts.history, alerts.delivery) ---- */
export function seedAlerts(BOOT, uptimeS) {
  const day = new Date(BOOT - uptimeS * 1000); day.setHours(0, 0, 0, 0);
  const R1 = day.getTime() - (2 * 3600 + 2 * 60) * 1000;   // yesterday's evening run (the log seed's run 1)
  const items = [
    { id: 'alert-1', seq: 1, severity: 'warn', event_id: 'WARNING', title: '已回退到 Sentinel', detail: 'Vanguard 已全部不可用，流量切换到 Sentinel · 首尔 01。', ts_ms: R1 + 1013000 },
    { id: 'alert-2', seq: 2, severity: 'critical', event_id: 'KILL_SWITCH_ON', title: '熔断保护已触发', detail: '所有层级都不可用，已阻断出站流量。', ts_ms: R1 + 2152400 },
    { id: 'alert-3', seq: 3, severity: 'info', event_id: 'RECOVERY', title: 'Vanguard 已恢复', detail: '流量已从 Bastion 切回 Vanguard。', ts_ms: R1 + 2167000 },
  ];
  const deliveries = [
    { id: 'delivery-telegram-main', channel_id: 'telegram-main', state: 'healthy', last_attempt_ms: R1 + 2169000, last_success_ms: R1 + 2169000, error: null },
    { id: 'delivery-desktop', channel_id: 'desktop', state: 'healthy', last_attempt_ms: R1 + 2167000, last_success_ms: R1 + 2167000, error: null },
  ];
  return { items, deliveries };
}

export const LISTENERS = [
  { kind: 'mixed', host: '127.0.0.1', port: 7890 },
  { kind: 'controller', host: '127.0.0.1', port: 25500 },
  { kind: 'subscription', host: '127.0.0.1', port: 25501 },
];
export const CORE_VERSION = 'v1.19.27';
