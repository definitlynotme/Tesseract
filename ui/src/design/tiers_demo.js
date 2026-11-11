/* ==========================================================================
   design/tiers_demo.js — the 层级 page's synthetic data (T-P4-04c)
   --------------------------------------------------------------------------
   Moved out of pages/tiers.js unchanged (映射_tiers §13): the city table, the page's own
   synthetic subscriptions and nodes, the protocol chains, the pool's places, the sample
   links of 「填入示例」, the synthetic technical parameters and the run-log lines the page
   writes into the simulator. This is demo data (P4 architecture §6): it renders under
   translate="no" and is deleted with the page's demo code (T-P4-05b / T-P4-07).
   Reasons use the v1 reason codes (tiers.reason.<code>), so the page names them through the
   catalog.
   ========================================================================== */

export const MIN = 60000, HOUR = 3600000;

/* city → place code (host names of the synthetic nodes) */
export const CODE = { 香港: 'hkg', 东京: 'tyo', 台北: 'tpe', 新加坡: 'sin', 大阪: 'osa', 首尔: 'sel', 洛杉矶: 'lax', 圣何塞: 'sjc', 西雅图: 'sea', 伦敦: 'lhr', 法兰克福: 'fra', 阿姆斯特丹: 'ams', 悉尼: 'syd', 多伦多: 'yyz', 孟买: 'bom', 圣保罗: 'gru', 伊斯坦布尔: 'ist', 迪拜: 'dxb' };
export const guessRegion = name => Object.keys(CODE).find(c => String(name).includes(c)) || '—';

/* the shared nodes get page-side extras: protocol chains (a second protocol's extra latency d,
   or a protocol that is down with its last value) */
export const CHAINS = {
  'v-hk03': [{ p: 'Hysteria2', port: 443 }, { p: 'Shadowsocks', port: 8388, d: 13 }],
  'v-tyo01': [{ p: 'Shadowsocks', port: 8443 }, { p: 'Trojan', port: 443, down: 'tls_failed', last: 91 }],
};

/* rows: [name, region, protocol, latency, reason when down, last latency] */
export function mkStandby(T, T0, pref, dom, rows, seed) {
  const r = T.util.rng(seed);
  return rows.map(([name, region, proto, lat, reason, last]) => {
    const num = (name.match(/(\d+)\s*$/) || [0, '1'])[1].padStart(2, '0');
    const host = `${CODE[region] || 'node'}${num}.${dom}`;
    const down = lat == null || lat > 800;
    return { id: `${pref}-${CODE[region] || 'n'}${num}`, name, region, protocols: [proto], host, port: proto === 'Shadowsocks' ? 8388 : 443,
      lat: down ? null : lat, lastLat: down ? (last || lat) : lat, base: lat != null ? lat : last, state: down ? 'down' : lat > 400 ? 'slow' : 'ok',
      reason: lat == null ? reason : lat > 800 ? 'latency_limit_exceeded' : null, checkedAt: T0 - Math.round(6 + r() * 40) * MIN };
  });
}
export const NEB = [['香港 02', '香港', 'Shadowsocks', 38], ['香港 05', '香港', 'VMess', 44], ['东京 04', '东京', 'VMess', 56], ['台北 01', '台北', 'Shadowsocks', 63],
  ['新加坡 03', '新加坡', 'Trojan', 77], ['大阪 01', '大阪', 'Shadowsocks', 61], ['首尔 03', '首尔', 'VMess', 52], ['洛杉矶 05', '洛杉矶', 'VMess', 171],
  ['圣何塞 01', '圣何塞', 'Shadowsocks', 164], ['西雅图 02', '西雅图', 'Trojan', 182], ['伦敦 02', '伦敦', 'VMess', 236], ['法兰克福 01', '法兰克福', 'Shadowsocks', 228],
  ['阿姆斯特丹 02', '阿姆斯特丹', 'Trojan', 241], ['悉尼 01', '悉尼', 'VMess', 148], ['多伦多 01', '多伦多', 'Shadowsocks', 214], ['孟买 01', '孟买', 'VMess', 462],
  ['圣保罗 01', '圣保罗', 'Trojan', 538], ['伊斯坦布尔 01', '伊斯坦布尔', 'VMess', null, 'tls_failed', 276], ['迪拜 01', '迪拜', 'Shadowsocks', null, 'configuration_invalid', 251]];
export const AUR = [['香港 A1', '香港', 'Trojan', 71], ['东京 A2', '东京', 'Shadowsocks', 84], ['新加坡 A3', '新加坡', 'Trojan', 96], ['首尔 A4', '首尔', 'VMess', 88],
  ['台北 A5', '台北', 'Shadowsocks', 102], ['大阪 A6', '大阪', 'Trojan', 91], ['洛杉矶 A7', '洛杉矶', 'VMess', 188], ['圣何塞 A8', '圣何塞', 'Trojan', 196],
  ['法兰克福 A9', '法兰克福', 'Shadowsocks', 251], ['伦敦 A10', '伦敦', 'VMess', 262], ['阿姆斯特丹 A11', '阿姆斯特丹', 'Trojan', null, 'dns_resolution_failed', 244], ['悉尼 A12', '悉尼', 'Shadowsocks', 436]];
export const PUB_A = [['新加坡 11', '新加坡', 'Shadowsocks', 142], ['东京 27', '东京', 'VMess', 118], ['首尔 09', '首尔', 'Trojan', null, 'connection_timeout', 160]];

/* the page's synthetic subscriptions (auto: an update interval key, see the page's AUTO list) */
export const NEBULA_FALLBACK = { name: 'Nebula 订阅', url: 'https://sub.nebula.example.com/api/v1/client/token', updatedAgoMin: 120 };
export const AURORA = { name: 'Aurora 备用订阅', url: 'https://aurora.example.net/link/7f3c9a2e?target=clash', auto: '12h' };
export const PUBLIC_A = { name: '公共源 A', url: 'https://raw.example.org/public/nodes-a.yaml', auto: '30m' };

/* drafts, your own Bastion node and the Gateway draft */
export const LOCAL = {
  vanguardDraft: { id: 'v-fra02', tier: 'vanguard', name: '法兰克福 备用', region: '法兰克福', protocols: ['Trojan'], host: 'fra02.vps.example.net', lastLat: 188, base: 188, draft: true },
  bastionNode: { id: 'b-socks', tier: 'bastion', name: '临时 SOCKS5', region: '—', protocols: ['SOCKS5'], host: '198.51.100.23', port: 1080, base: 212, state: 'pending', checkedAt: null },
  gatewayDraft: { id: 'g-fra05', tier: 'gateway', name: '法兰克福 Gateway 05', region: '法兰克福', protocols: ['Trojan'], host: 'gw-fra05.example.org', lastLat: 231, base: 231, draft: true },
};

/* the automatic pool's places (a region and a number within it, never an internal id) */
export const POOL_REGIONS = ['新加坡', '东京', '首尔', '香港', '洛杉矶', '法兰克福', '伦敦', '阿姆斯特丹', '多伦多', '悉尼', '孟买', '圣保罗'];
export const poolName = (reg, k) => `${reg} ${String(k).padStart(2, '0')}`;

/* a subscription added in the import sheet: its synthetic rows */
export function subRows(r, n) {
  const cities = Object.keys(CODE);
  return Array.from({ length: n }, (_, i) => { const c = cities[Math.floor(r() * cities.length)]; return [`${c} ${String(i + 1).padStart(2, '0')}`, c, ['Shadowsocks', 'VMess', 'Trojan'][i % 3], Math.round(40 + Math.pow(r(), 1.5) * 380)]; });
}

/* 「填入示例」: a paste with duplicates and problems (the design target only) */
const b64e = s => btoa(unescape(encodeURIComponent(s)));
export function sample(t) {
  const ssv = b64e('aes-256-gcm:example-pass');
  const vm = (ps, add) => 'vmess://' + b64e(JSON.stringify({ v: '2', ps, add, port: '443', id: '0f2a6c1e-5b7d-4c3e-9a1f-2e4d6b8a0c11', net: 'ws', tls: 'tls' }));
  const S0 = {
    vanguard: [`hysteria2://s3cret@sgp01.vps.example.net:443?sni=sgp01.vps.example.net#${encodeURIComponent('新加坡 VPS 01')}`,
      `vless://1f6d2c1e-9b7a-4c3e-8d2f-6a5b4c3d2e1f@lax01.vps.example.net:443?security=reality&pbk=Zx9q3vKjW2&sid=6ba8#${encodeURIComponent('洛杉矶 VPS 01')}`,
      `trojan://p4ss@hkg02.vps.example.net:443?sni=hkg02.vps.example.net#${encodeURIComponent('香港 VPS 02')}`,
      `ss://${ssv}@tyo01.vps.example.net:8443#${encodeURIComponent('东京 CN2 01')}`,
      `vless://2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d@fra03.vps.example.net:443?security=reality#${encodeURIComponent('法兰克福 VPS 03')}`],
    sentinel: [`ss://${ssv}@hkg09.orion.example.com:8388#${encodeURIComponent('香港 09')}`, vm('首尔 06', 'sel06.orion.example.com'),
      `trojan://p4ss@sin07.orion.example.com:443#${encodeURIComponent('新加坡 07')}`, vm('首尔 01', 'sel01.nebula.example.com'), 'https://sub.orion.example.com/api/v1/client?token=•••'],
    bastion: [`socks5://198.51.100.61:1080#${encodeURIComponent('公共 SOCKS5 02')}`, `ss://${ssv}@203.0.113.77:8388#${encodeURIComponent('公共 · 大阪 14')}`,
      `socks5://198.51.100.23:1080#${encodeURIComponent('临时 SOCKS5')}`, 'wireguard://pub@@bad'],
    gateway: [`ss://${ssv}@gw-sin04.example.org:443#${encodeURIComponent('新加坡 Gateway 04')}`, `trojan://p4ss@gw-lhr06.example.org:443#${encodeURIComponent('伦敦 Gateway 06')}`,
      `ss://${ssv}@gw-tyo01.example.org:443#${encodeURIComponent('东京 Gateway 01')}`],
  };
  return S0[t].join('\n');
}

/* 完整技术参数: synthetic values (the panel shows them under translate="no"); [label, value, secret] */
export function params(n, r) {
  const hex = k => Array.from({ length: k }, () => '0123456789abcdef'[Math.floor(r() * 16)]).join('');
  const uuid = `${hex(8)}-${hex(4)}-4${hex(3)}-a${hex(3)}-${hex(12)}`, pass = hex(24);
  const p = (n.protocols[0] || '').replace(/ .*/, '');
  const common = [['服务器', n.host], ['端口', String(n.port)]];
  const map = {
    Hysteria2: [['密码', pass, 1], ['SNI', n.host], ['混淆', '无'], ['ALPN', 'h3'], ['跳过证书验证', '否']],
    Shadowsocks: [['加密方式', '2022-blake3-aes-128-gcm'], ['密码', pass, 1], ['UDP 转发', '开启']],
    Trojan: [['密码', pass, 1], ['SNI', n.host], ['ALPN', 'h2, http/1.1'], ['指纹', 'chrome'], ['跳过证书验证', '否']],
    VMess: [['UUID', uuid, 1], ['alterId', '0'], ['加密', 'auto'], ['传输方式', 'WebSocket'], ['Path', '/ws'], ['TLS', '开启']],
    VLESS: [['UUID', uuid, 1], ['Flow', 'xtls-rprx-vision'], ['Public Key', hex(43), 1], ['Short ID', hex(8)], ['SNI', 'www.example.com'], ['指纹', 'chrome']],
    SOCKS5: [['用户名', 'user-' + hex(4)], ['密码', pass, 1], ['UDP 转发', '开启']],
  };
  return common.concat(map[p] || [['密码', pass, 1]]);
}

/* what the user did on the page goes into the simulator's run log (运行日志 lists it). The lines
   are simulated runtime records (data): the real runtime writes its own (B-19). */
const LOG = {
  bastion_on: () => ['tier', 'Bastion 应急池已启用', '只在 Vanguard 与 Sentinel 都不可用时接管 · 请勿登录任何账号'],
  bastion_off: () => ['tier', 'Bastion 应急池已关闭', '不再收集公共节点'],
  sub_failed: p => ['source', '订阅更新失败', `${p.name} · 订阅地址返回 403，令牌可能已过期`],
  sub_updated: p => ['source', '订阅已更新', `${p.name} · ${p.n} 个节点`],
  gateway_switched: p => ['exit', 'Gateway 出口已切换', `${p.name} · 所有层级的流量改从它离开`],
  gateway_off: () => ['exit', 'Gateway 已停用', '流量从当前层级的节点直接离开'],
  to_draft: p => ['tier', '节点已设为草稿', `${p.tier} · ${p.name} · 不再参与${p.gateway ? '出口选择' : '回退'}`],
  from_draft: p => ['tier', '节点已加入运行', `${p.tier} · ${p.name}${p.ms != null ? ` · ${p.ms} ms` : ' · 服务启动后检测'}`],
  node_deleted: p => ['source', '节点已删除', `${p.tier} · ${p.name}`],
  sub_deleted: p => ['source', '订阅已删除', `${p.tier} · ${p.name}`],
  imported: p => ['source', `已导入 ${p.names.length} 个节点`, `${p.tier} · ${p.names.slice(0, 3).join('、')}${p.names.length > 3 ? ' 等' : ''}`],
  sub_added: p => ['source', '订阅已添加', `${p.tier} · ${p.name} · ${p.n} 个节点`],
};
let logN = 0;
export function log(T, kind, p) {
  const [category, title, detail] = LOG[kind](p || {});
  const level = kind === 'sub_failed' ? 'error' : 'info';
  const ts = Date.now(), L = T.sim.logs;
  const rec = { id: 'LT' + (++logN), ts, level, source: 'Tesseract', category, title, detail: detail || '',
    raw: `${T.fmt.time(ts)} ${level.toUpperCase().padEnd(5)} [${category}] ${title}${detail ? ' · ' + detail : ''}` };
  if (Array.isArray(L)) { L.push(rec); if (L.length > 240) L.shift(); }
  T.emit('sim:log', rec);
}
