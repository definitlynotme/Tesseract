/* ==========================================================================
   data.js — shared synthetic data (T.data). Plausible, not real:
   RFC 5737 / 3849 addresses, example domains, AS64500.
   Pages may read this; the simulator (sim.js) mutates live fields
   (node.lat, node.state) in place and emits events when it does.
   ========================================================================== */
import { MODES, TIERS } from '../data/static.js';

(function () {
  'use strict';
  const T = window.T;
  const MB = 1024 * 1024, KB = 1024;

  /* Tier and mode metadata live in data/static.js (T-P4-02b): the same objects, plus the demo's
     `enabled` default (a runtime fact: tiers.summary; pages and the simulator flip it here). */
  const ENABLED = { vanguard: true, sentinel: true, bastion: false, gateway: true };
  const tiers = TIERS.map(t => ({ ...t, enabled: ENABLED[t.id] }));

  /* state: ok · slow · down · pending · draft; lat in ms (null = unreachable) */
  const nodes = {
    vanguard: [
      { id: 'v-hk03', name: '香港 IEPL 03', region: '香港', protocols: ['Hysteria2', 'Shadowsocks'], host: 'hk03.vps.example.net', port: 443, lat: 42, state: 'ok', current: true },
      { id: 'v-tyo01', name: '东京 CN2 01', region: '东京', protocols: ['Shadowsocks', 'Trojan'], host: 'tyo01.vps.example.net', port: 8443, lat: 68, state: 'ok' },
    ],
    sentinel: [
      { id: 's-sel01', name: '首尔 01', region: '首尔', protocols: ['VMess'], host: 'sel01.nebula.example.com', port: 443, lat: 49, state: 'ok', group: 'nebula' },
      { id: 's-osa03', name: '大阪 03', region: '大阪', protocols: ['Shadowsocks'], host: 'osa03.nebula.example.com', port: 8388, lat: 58, state: 'ok', group: 'nebula' },
      { id: 's-sin01', name: '新加坡 01', region: '新加坡', protocols: ['VMess'], host: 'sin01.nebula.example.com', port: 443, lat: 71, state: 'ok', group: 'nebula' },
      { id: 's-lax02', name: '洛杉矶 02', region: '洛杉矶', protocols: ['VMess'], host: 'lax02.nebula.example.com', port: 443, lat: 162, state: 'ok', group: 'nebula' },
      { id: 's-fra04', name: '法兰克福 04', region: '法兰克福', protocols: ['Shadowsocks'], host: 'fra04.nebula.example.com', port: 8388, lat: null, lastLat: 214, state: 'down', group: 'nebula' },
    ],
    bastion: [],
    gateway: [
      { id: 'g-tyo01', name: '东京 Gateway 01', region: '东京', protocols: ['Shadowsocks'], host: 'gw-tyo01.example.org', port: 443, lat: 36, state: 'ok', current: true },
      { id: 'g-osa02', name: '大阪 Gateway 02', region: '大阪', protocols: ['Hysteria2'], host: 'gw-osa02.example.org', port: 443, lat: 44, state: 'ok' },
      { id: 'g-sel03', name: '首尔 Gateway 03', region: '首尔', protocols: ['Shadowsocks', 'Trojan'], host: 'gw-sel03.example.org', port: 443, lat: 52, state: 'ok' },
    ],
  };

  const subscriptions = [
    { id: 'nebula', tier: 'sentinel', name: 'Nebula 订阅', count: 24, used: 5, updatedAgoMin: 120, url: 'https://sub.nebula.example.com/api/v1/client/token' },
  ];

  /* current exit identity (as last verified) */
  const exit = {
    v4: { ip: '203.0.113.24', country: '日本', city: '东京', tz: 'Asia/Tokyo', asn: 'AS64500', org: 'Example Hosting K.K.', type: '托管网络' },
    v6: { ip: '2001:db8:4f::1a', sameAsV4: true },
    route: { via: '香港 IEPL 03', exit: '东京 Gateway 01' },
    routeFallback: { via: '首尔 01', exit: '东京 Gateway 01' },
  };

  /* protocols actually loaded by the running config (sums to 13) */
  const protocols = [
    { name: 'Shadowsocks', count: 6 },
    { name: 'VMess', count: 3 },
    { name: 'Hysteria2', count: 2 },
    { name: 'Trojan', count: 2 },
  ];

  const modes = MODES;   // data/static.js
  /* one confirmation wording for every place a mode is switched while running (总览's strip,
     配置, the palette) */
  const modeSwitch = v => ({
    title: `切换到${modes[v].label}？`,
    message: `代理服务会重启，流量会短暂中断（约 1–2 秒）。${v === 'tun' ? '全局模式会接管本机全部流量。' : ''}`,
  });

  /* the protocol mix of the pool (your own + subscription nodes; Bastion's pool apart), read from
     the nodes themselves so it follows imports. A multi-protocol node counts once per protocol, so
     the entries (13) exceed the nodes (10); the label says both. */
  const PROTO_ORDER = ['Shadowsocks', 'VMess', 'Hysteria2', 'Trojan', 'VLESS', 'TUIC', 'SOCKS5'];
  function protocolMix() {
    const list = [].concat(nodes.vanguard, nodes.sentinel, nodes.gateway).filter(n => n && !n.draft);
    const count = {};
    list.forEach(n => (n.protocols || []).forEach(p => { count[p] = (count[p] || 0) + 1; }));
    const rank = p => { const i = PROTO_ORDER.indexOf(p); return i < 0 ? 99 : i; };
    const out = Object.keys(count).map(name => ({ name, count: count[name] })).sort((a, b) => b.count - a.count || rank(a.name) - rank(b.name));
    return { nodes: list.length, entries: out.reduce((a, p) => a + p.count, 0), list: out };
  }

  /* settings that more than one surface reads; sim.js writes them into the store at boot
     (store keys admin · ksEnabled · ksBlocked · bastion · update) so every page agrees before
     配置 has ever rendered. admin: the prototype runs elevated (配置's demo link 以普通身份运行
     shows the unelevated state everywhere: gated 全局 / 混合, kill switch 未生效). */
  const defaults = {
    admin: true,
    killswitch: true,                     // 熔断保护 switched on (config killswitch.enabled)
    bastionPool: { size: 60, min: 3 },    // Bastion 应急池: 60 places; 最少可用节点 1–60, default 3
  };

  /* seeded 24 h history (288 × 5 min) and a 5 min live window (300 × 1 s) */
  function seedHistory(now) {
    const rnd = T.util.rng(20260925);
    const g = (h, c, w) => Math.exp(-((h - c) ** 2) / (2 * w * w));
    const raw = [];
    for (let i = 287; i >= 0; i--) {
      const t = now - i * 300000, d = new Date(t), h = d.getHours() + d.getMinutes() / 60;
      const base = 0.1 + 2.0 * g(h, 11, 1.8) + 1.55 * g(h, 15.3, 1.5) + 1.3 * g(h, 21.5, 1.4) + 0.6 * g(h, 13.3, 0.5);
      raw.push({ t, d: MB * base * (0.74 + rnd() * 0.52) });
    }
    const h24 = raw.map((r, i) => {
      const a = raw[Math.max(0, i - 1)].d, c = raw[Math.min(raw.length - 1, i + 1)].d, d = (a + 2 * r.d + c) / 4;
      return { t: r.t, d, u: d * (0.055 + rnd() * 0.03) + 5 * KB, n: 300 };
    });
    const live = [];
    let d = 1.3 * MB, tg = 1.5 * MB;
    for (let i = 299; i >= 0; i--) {
      if (rnd() < 0.08) tg = MB * (0.9 + Math.pow(rnd(), 1.3) * 1.5);
      d = Math.max(60 * KB, d + (tg - d) * 0.26 + (rnd() - 0.5) * 0.16 * MB);
      live.push({ t: now - i * 1000, d, u: d * (0.064 + rnd() * 0.012) });
    }
    live[live.length - 1].d = 1.55 * MB; live[live.length - 1].u = 109.16 * KB;
    // the seeded day ends wherever the clock is; ease its last hour toward the live rate so
    // the 24 h curve meets 现在 instead of ending in a one-bucket cliff
    // (a smoothstep over the last 3 hours: at 24 h scale one hour is only ~20 px wide)
    const n = h24.length, TAIL = 36;
    for (let k = TAIL; k >= 1; k--) {
      const p = h24[n - k], x = (TAIL + 1 - k) / TAIL, w = x * x * (3 - 2 * x);
      const wob = 1 + 0.12 * Math.sin(k * 1.7) * (1 - w);   // keep a little texture in the ramp
      p.d = (p.d + (1.55 * MB - p.d) * w) * wob; p.u = p.u + (109.16 * KB - p.u) * w;
    }
    return { h24, live };
  }

  T.data = {
    version: '0.1.0',
    listen: '127.0.0.1:7890',
    controller: '127.0.0.1:25500',
    tiers,
    tier: id => tiers.find(t => t.id === id),
    nodes,
    subscriptions,
    exit,
    protocols,
    protocolTotal: protocols.reduce((a, p) => a + p.count, 0),
    protocolMix,
    modes,
    modeSwitch,
    defaults,
    seedHistory,
    session: { down: 7.29 * 1024 * MB, up: 583.65 * MB, rateDown: 1.55 * MB, rateUp: 109.16 * KB, uptime: 2 * 3600 + 14 * 60 + 13 },
  };
})();
