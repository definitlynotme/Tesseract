// Render cases of T-P4-04b (message extraction · overview) and the helpers its unit test
// (ui/test/unit/overview_i18n.test.mjs) shares.
//
// Default export: fixture-mode cases (ui/test/fixtures/scenarios/overview_i18n_*.json, ASCII data
// only, no simulator) for the render matrix: running, stopped, starting. Besides the matrix's own
// checks (leak in pseudo, overflow, injection, console) each cell asserts, in pseudo, that every
// aria-label / title / placeholder / data-tip of the overview page outside translate="no" came
// from the catalog (wrapped in ⟦ ⟧; critique E2), and in every language that the overview page
// shows no visible CJK text outside translate="no" unless the language is zh_CN.
//
// Named exports (the regression check, critique E1; used by the unit test):
//   overviewTreeInPage   the visible text tree (T-P4-10 / P4-01 baseline format) of the overview
//                        page section only, plus the data-tip of every element in it
//   SIM_STEPS            a fixed walk through the simulator (design target, sim mode): running,
//                        degraded, fallback, recovered, kill switch off, gated mode, mode confirm,
//                        restart, refresh, stopping, stopped, starting (phase 0 / 1), cancelling
//   walkSim(page)        runs SIM_STEPS in a page loaded with the deterministic fixture and returns
//                        {state: {tree, tips}}
//   baselineSlice(rows)  the overview section rows of ui/test/baseline/overview.textree.json
//   BEFORE               the prototype's trees (stopped, starting phase 0 / 1) and data-tips
//   overviewWordsInPage / wordProblems / relabelRoundTrip   the leak and language-switch checks
import { readFileSync } from 'node:fs';
import { cellChecksInPage, LEAK_WHITELIST } from '../checks.mjs';

// Runs in the page. Same row format as textree.mjs textTreeInPage (absolute depth from <body>),
// restricted to the overview page section (the section's own row included). tips: [path, data-tip]
// for every element of the section that carries a data-tip (visible or not), in DOM order.
// One normalisation: a data run wrapper (span.dt[translate=no], pages/overview.js data()) is
// transparent — its text counts as its parent's own text and it has no row — so a node name that
// T-P4-04b wrapped for machine translation compares equal to the prototype's bare text.
export function overviewTreeInPage() {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('section[data-page="overview"]');
  const isData = n => n.nodeType === 1 && n.localName === 'span' && n.classList.contains('dt') && n.getAttribute('translate') === 'no';
  const out = [];
  const visit = (el, depth, inside) => {
    for (const c of el.children) {
      if (isData(c)) continue;
      const inn = inside || c === root;
      if (!c.checkVisibility({ opacityProperty: true, visibilityProperty: true })) {
        if (getComputedStyle(c).display !== 'none') visit(c, depth + 1, inn);
        continue;
      }
      let own = '';
      for (const n of c.childNodes) if (n.nodeType === 3) own += n.nodeValue; else if (isData(n)) own += n.textContent;
      const row = [depth, c.tagName.toLowerCase(), norm(own), norm(c.getAttribute('aria-label')), norm(c.getAttribute('title')), norm(c.getAttribute('placeholder'))];
      if (inn && (row[2] || row[3] || row[4] || row[5])) out.push(row);
      visit(c, depth + 1, inn);
    }
  };
  visit(document.body, 0, false);
  const path = el => {
    const parts = [];
    for (let e = el; e && e !== root; e = e.parentElement) {
      let p = e.tagName.toLowerCase();
      if (e.id) { parts.unshift(p + '#' + e.id); break; }
      const cls = Array.from(e.classList).filter(c => !/^is-|^swap/.test(c)).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      if (e.dataset && e.dataset.v) p += `[v=${e.dataset.v}]`;
      if (e.dataset && e.dataset.tier) p += `[tier=${e.dataset.tier}]`;
      parts.unshift(p);
    }
    return parts.join('>');
  };
  const tips = root ? Array.from(root.querySelectorAll('[data-tip]')).map(e => [path(e), norm(e.getAttribute('data-tip'))]) : [];
  return { tree: out, tips };
}

/** Rows of the overview section in a whole-body text tree (the P4-01 baseline file). */
export function baselineSlice(rows) {
  const at = rows.findIndex(r => r[1] === 'section' && r[3] === '总览');
  if (at < 0) throw new Error('baseline: no overview section row');
  const d = rows[at][0];
  const out = [rows[at]];
  for (let i = at + 1; i < rows.length && rows[i][0] > d; i++) out.push(rows[i]);
  return out;
}

// The simulator walk. Each step: an action in the page, then virtual time; `snap` names the
// state captured after it. T.sim and T.store are the design target's (sim mode).
export const SIM_STEPS = [
  { snap: 'running' },
  { eval: () => T.sim.scenario('fallback'), ms: 300, snap: 'degraded' },
  { ms: 1500, snap: 'fallback' },
  { eval: () => T.sim.scenario('recover'), ms: 3000, snap: 'recovered' },
  { eval: () => T.store.set('ksEnabled', false), ms: 50, snap: 'ks_off' },
  { eval: () => T.store.set('ksEnabled', true), ms: 50 },
  { eval: () => T.store.set('ksBlocked', true), ms: 50, snap: 'ks_blocked' },
  { eval: () => T.store.set('ksBlocked', false), ms: 50 },
  { eval: () => document.querySelector('#ov-mode [data-v="tun"]').click(), ms: 300, snap: 'mode_confirm' },
  { eval: () => document.querySelector('.ov-confirm [data-a="no"]').click(), ms: 600 },
  { eval: () => T.store.set('admin', false), ms: 50, snap: 'gated' },
  { eval: () => document.querySelector('#ov-mode [data-v="tun"]').click(), ms: 100, snap: 'gated_explain' },
  { eval: () => T.store.set('admin', true), ms: 3500 },
  { eval: () => document.querySelector('#ov-restart').click(), ms: 200, snap: 'restarting' },
  { ms: 1400, snap: 'restarted' },
  { ms: 3000 },
  { eval: () => document.querySelector('#ov-refresh').click(), ms: 200, snap: 'refreshing' },
  { ms: 4000, snap: 'refreshed' },
  { eval: () => T.sim.stop(), ms: 100, snap: 'stopping' },
  { ms: 3000, snap: 'stopped' },
  { eval: () => document.querySelector('#ov-mode [data-v="tun"]').click(), ms: 300, snap: 'stopped_tun' },
  { eval: () => document.querySelector('#ov-mode [data-v="system"]').click(), ms: 300 },
  { eval: () => T.sim.start(), ms: 300, snap: 'starting0' },
  { ms: 1300, snap: 'starting1' },
  { ms: 1300, snap: 'starting2' },
  { ms: 3000, snap: 'running_again' },
  { eval: () => T.sim.stop(), ms: 3000 },
  { eval: () => T.sim.start(), ms: 300 },
  { eval: () => T.sim.stop(), ms: 100, snap: 'cancelling' },
  { ms: 3000, snap: 'cancelled' },
];

/** Run SIM_STEPS in a page already loaded (#overview) and settled. */
export async function walkSim(page, { steps = SIM_STEPS } = {}) {
  const out = {};
  for (const s of steps) {
    if (s.eval) await page.evaluate(s.eval);
    await page.evaluate(ms => window.__fixtureClock.advance(ms), s.ms || 0);
    await page.waitForTimeout(60);
    await page.evaluate(() => window.__fixtureClock.advance(0));
    if (s.snap) out[s.snap] = await page.evaluate(overviewTreeInPage);
  }
  return out;
}

export const BASELINE_FILE = new URL('../../baseline/overview.textree.json', import.meta.url);
export const readBaseline = () => JSON.parse(readFileSync(BASELINE_FILE, 'utf8'));

// Reference text trees of the prototype overview (T-P4-01 code, before T-P4-04b), recorded with
// SIM_STEPS at 1280x800 by the same overviewTreeInPage (evidence/T-P4-04b/before.json). The running
// state is the committed baseline itself (ui/test/baseline/overview.textree.json); the data-tips
// of all three are here.
export const BEFORE = {
  running: {
   tips: [
    ["button#ov-restart","重启代理核心，流量会短暂中断（约 1–2 秒）"],
    ["span#ov-prov","启动时自动查询；点刷新可完整验证"],
    ["button#ov-refresh","经当前出口完整验证一次"],
    ["button#ov-copy4","复制 IPv4"],
    ["section#ov-exit>div.exit-stage>div.exit-full>div.ip-block>div.ip-meta>span.ip-net","来源报告，仅供参考"],
    ["button#ov-copy6","复制 IPv6"],
    ["button#ov-demo","原型演示控件：模拟 Vanguard 变慢、回退到 Sentinel"],
    ["button#ov-gate","固定的最终出口，不参与回退"],
    ["span#ov-pool-dots>span.dots","Bastion 未启用"],
    ["div#ov-pbar>i","Shadowsocks · 6 个节点 · 46%"],
    ["div#ov-pbar>i","VMess · 3 个节点 · 23%"],
    ["div#ov-pbar>i","Hysteria2 · 2 个节点 · 15%"],
    ["div#ov-pbar>i","Trojan · 2 个节点 · 15%"],
   ],
  },
  stopped: {
   tree: [
    [4,"section","","总览","",""],
    [8,"div","当前层级","","",""],
    [10,"span","未连接","","",""],
    [10,"span","服务未启动 · 启动后优先使用","","",""],
    [12,"b","Vanguard","","",""],
    [12,"span","自建","","",""],
    [7,"div","","服务控制","",""],
    [10,"span","启动服务","","",""],
    [10,"span","启动服务","","",""],
    [8,"button","","启动服务","",""],
    [6,"section","","当前出口","",""],
    [8,"h3","当前出口","","",""],
    [10,"span","未查询","","",""],
    [11,"span","IPv4","","",""],
    [11,"span","—","","",""],
    [11,"span","IPv6","","",""],
    [11,"span","—","","",""],
    [12,"span","服务未启动","","",""],
    [11,"span","启动后会经当前出口自动查询，不暴露本机 IP","","",""],
    [9,"span","启动后经由","","",""],
    [10,"span","Vanguard","","",""],
    [10,"span","东京 Gateway 01","","",""],
    [6,"section","","回退顺序","",""],
    [8,"h3","回退顺序","","",""],
    [9,"span","服务未启动","","",""],
    [8,"button","层级回退","","",""],
    [8,"button","","Vanguard 自建，查看层级","",""],
    [13,"span","未连接","","",""],
    [12,"span","Vanguard → Sentinel；都不可用时阻断出站","","",""],
    [13,"span","启动后依次回退：","","",""],
    [10,"span","Vanguard","","",""],
    [10,"span","自建","","",""],
    [11,"span","2 个节点","","",""],
    [8,"button","","Sentinel 订阅，查看层级","",""],
    [10,"span","Sentinel","","",""],
    [10,"span","订阅","","",""],
    [11,"span","5 个节点","","",""],
    [8,"button","","Bastion 应急，查看层级","",""],
    [10,"span","Bastion","","",""],
    [10,"span","应急","","",""],
    [11,"span","未启用","","",""],
    [7,"button","","Gateway 固定出口，查看层级","",""],
    [8,"span","Gateway","","",""],
    [8,"span","固定出口","","",""],
    [9,"span","东京 Gateway 01","","",""],
    [6,"section","","流量","",""],
    [8,"h3","流量","","",""],
    [9,"span","上次会话","","",""],
    [9,"span","","曲线时间范围","",""],
    [10,"button","24 小时","","",""],
    [10,"button","5 分钟","","",""],
    [9,"div","下载","","",""],
    [10,"span","实时","","",""],
    [11,"span","—","","",""],
    [10,"span","","7.32 GB","",""],
    [11,"span","7.32","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [12,"span",".","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [11,"span","GB","","",""],
    [9,"div","上传","","",""],
    [10,"span","实时","","",""],
    [11,"span","—","","",""],
    [10,"span","","585.67 MB","",""],
    [11,"span","585.67","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [12,"span",".","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [11,"span","MB","","",""],
    [7,"div","","过去 24 小时的下载与上传速率曲线","",""],
    [10,"span","0","","",""],
    [10,"span","1.5 MB/s","","",""],
    [10,"span","3 MB/s","","",""],
    [9,"span","12:00","","",""],
    [9,"span","18:00","","",""],
    [9,"span","00:00","","",""],
    [9,"span","06:00","","",""],
    [9,"span","现在","","",""],
    [9,"span","服务已停止，暂停采样","","",""],
    [7,"div","","网络模式、熔断保护与节点池","",""],
    [10,"span","网络模式","","",""],
    [10,"span","","网络模式","",""],
    [11,"button","系统代理","","",""],
    [11,"button","全局","","",""],
    [11,"button","混合","","",""],
    [11,"span","未接管 · 启动后接管","","",""],
    [12,"span","127.0.0.1:7890","","",""],
    [8,"button","","熔断保护，前往配置","",""],
    [11,"span","熔断保护","","",""],
    [13,"span","未生效","","",""],
    [12,"span","启动后自动布防","","",""],
    [8,"button","","节点池，前往层级","",""],
    [11,"span","节点池","","",""],
    [13,"span","—","","",""],
    [12,"span","启动后检测","","",""],
    [7,"section","","协议分布","",""],
    [9,"h3","协议分布","","",""],
    [10,"span","上次加载的配置 · 启动后刷新","","",""],
    [11,"span","Shadowsocks","","",""],
    [11,"span","6","","",""],
    [11,"span","VMess","","",""],
    [11,"span","3","","",""],
    [11,"span","Hysteria2","","",""],
    [11,"span","2","","",""],
    [11,"span","Trojan","","",""],
    [11,"span","2","","",""],
   ],
   tips: [
    ["button#ov-restart","重启代理核心，流量会短暂中断（约 1–2 秒）"],
    ["span#ov-prov",""],
    ["button#ov-refresh","经当前出口完整验证一次"],
    ["button#ov-copy4","复制 IPv4"],
    ["section#ov-exit>div.exit-stage>div.exit-full>div.ip-block>div.ip-meta>span.ip-net","来源报告，仅供参考"],
    ["button#ov-copy6","复制 IPv6"],
    ["button#ov-demo","服务运行时可用（原型演示控件）"],
    ["button#ov-gate","固定的最终出口，不参与回退"],
    ["span#ov-pool-dots>span.dots","Bastion 未启用"],
    ["div#ov-pbar>i","Shadowsocks · 6 个节点 · 46%"],
    ["div#ov-pbar>i","VMess · 3 个节点 · 23%"],
    ["div#ov-pbar>i","Hysteria2 · 2 个节点 · 15%"],
    ["div#ov-pbar>i","Trojan · 2 个节点 · 15%"],
   ],
  },
  starting0: {
   tree: [
    [4,"section","","总览","",""],
    [8,"div","当前层级","","",""],
    [10,"span","启动中","","",""],
    [10,"span","正在收集节点…","","",""],
    [7,"div","","服务控制","",""],
    [12,"span","收集节点","","",""],
    [12,"span","1/3","","",""],
    [10,"span","取消","","",""],
    [12,"span","收集节点","","",""],
    [12,"span","1/3","","",""],
    [10,"span","取消","","",""],
    [8,"button","","取消启动","",""],
    [6,"section","","当前出口","",""],
    [8,"h3","当前出口","","",""],
    [10,"span","等待就绪","","",""],
    [11,"span","IPv4","","",""],
    [11,"span","—","","",""],
    [11,"span","IPv6","","",""],
    [11,"span","—","","",""],
    [12,"span","服务启动中","","",""],
    [11,"span","就绪后经当前出口自动查询，不暴露本机 IP","","",""],
    [9,"span","启动后经由","","",""],
    [10,"span","Vanguard","","",""],
    [10,"span","东京 Gateway 01","","",""],
    [6,"section","","回退顺序","",""],
    [8,"h3","回退顺序","","",""],
    [8,"button","层级回退","","",""],
    [8,"button","","Vanguard 自建，查看层级","",""],
    [13,"span","启动中","","",""],
    [12,"span","正在收集节点…","","",""],
    [10,"span","Vanguard","","",""],
    [10,"span","自建","","",""],
    [11,"span","收集中…","","",""],
    [8,"button","","Sentinel 订阅，查看层级","",""],
    [10,"span","Sentinel","","",""],
    [10,"span","订阅","","",""],
    [11,"span","收集中…","","",""],
    [8,"button","","Bastion 应急，查看层级","",""],
    [10,"span","Bastion","","",""],
    [10,"span","应急","","",""],
    [11,"span","未启用","","",""],
    [7,"button","","Gateway 固定出口，查看层级","",""],
    [8,"span","Gateway","","",""],
    [8,"span","固定出口","","",""],
    [9,"span","等待验证","","",""],
    [6,"section","","流量","",""],
    [8,"h3","流量","","",""],
    [9,"span","本次会话","","",""],
    [9,"span","","曲线时间范围","",""],
    [10,"button","24 小时","","",""],
    [10,"button","5 分钟","","",""],
    [9,"div","下载","","",""],
    [10,"span","实时","","",""],
    [11,"span","—","","",""],
    [10,"span","","0 B","",""],
    [11,"span","0","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [11,"span","B","","",""],
    [9,"div","上传","","",""],
    [10,"span","实时","","",""],
    [11,"span","—","","",""],
    [10,"span","","0 B","",""],
    [11,"span","0","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [11,"span","B","","",""],
    [7,"div","","过去 24 小时的下载与上传速率曲线","",""],
    [10,"span","0","","",""],
    [10,"span","1.5 MB/s","","",""],
    [10,"span","3 MB/s","","",""],
    [9,"span","12:00","","",""],
    [9,"span","18:00","","",""],
    [9,"span","00:00","","",""],
    [9,"span","06:00","","",""],
    [9,"span","现在","","",""],
    [9,"span","正在连接，就绪后继续采样","","",""],
    [7,"div","","网络模式、熔断保护与节点池","",""],
    [10,"span","网络模式","","",""],
    [10,"span","","网络模式","",""],
    [11,"button","系统代理","","",""],
    [11,"button","全局","","",""],
    [11,"button","混合","","",""],
    [11,"span","就绪后接管 ·","","",""],
    [12,"span","127.0.0.1:7890","","",""],
    [8,"button","","熔断保护，前往配置","",""],
    [11,"span","熔断保护","","",""],
    [13,"span","未生效","","",""],
    [12,"span","就绪后自动布防","","",""],
    [8,"button","","节点池，前往层级","",""],
    [11,"span","节点池","","",""],
    [13,"span","检测中","","",""],
    [12,"span","正在收集节点…","","",""],
    [7,"section","","协议分布","",""],
    [9,"h3","协议分布","","",""],
    [10,"span","正在加载配置…","","",""],
    [11,"span","Shadowsocks","","",""],
    [11,"span","6","","",""],
    [11,"span","VMess","","",""],
    [11,"span","3","","",""],
    [11,"span","Hysteria2","","",""],
    [11,"span","2","","",""],
    [11,"span","Trojan","","",""],
    [11,"span","2","","",""],
   ],
   tips: [
    ["button#ov-restart","重启代理核心，流量会短暂中断（约 1–2 秒）"],
    ["span#ov-prov",""],
    ["button#ov-refresh","经当前出口完整验证一次"],
    ["button#ov-copy4","复制 IPv4"],
    ["section#ov-exit>div.exit-stage>div.exit-full>div.ip-block>div.ip-meta>span.ip-net","来源报告，仅供参考"],
    ["button#ov-copy6","复制 IPv6"],
    ["button#ov-demo","服务运行时可用（原型演示控件）"],
    ["button#ov-gate","固定的最终出口，不参与回退"],
    ["span#ov-pool-dots>span.dots","Bastion 未启用"],
    ["div#ov-pbar>i","Shadowsocks · 6 个节点 · 46%"],
    ["div#ov-pbar>i","VMess · 3 个节点 · 23%"],
    ["div#ov-pbar>i","Hysteria2 · 2 个节点 · 15%"],
    ["div#ov-pbar>i","Trojan · 2 个节点 · 15%"],
   ],
  },
  starting1: {
   tree: [
    [4,"section","","总览","",""],
    [8,"div","当前层级","","",""],
    [10,"span","启动中","","",""],
    [10,"span","正在验证出口…","","",""],
    [7,"div","","服务控制","",""],
    [12,"span","验证出口","","",""],
    [12,"span","2/3","","",""],
    [10,"span","取消","","",""],
    [12,"span","验证出口","","",""],
    [12,"span","2/3","","",""],
    [10,"span","取消","","",""],
    [8,"button","","取消启动","",""],
    [6,"section","","当前出口","",""],
    [8,"h3","当前出口","","",""],
    [10,"span","等待就绪","","",""],
    [11,"span","IPv4","","",""],
    [11,"span","—","","",""],
    [11,"span","IPv6","","",""],
    [11,"span","—","","",""],
    [12,"span","服务启动中","","",""],
    [11,"span","就绪后经当前出口自动查询，不暴露本机 IP","","",""],
    [9,"span","启动后经由","","",""],
    [10,"span","Vanguard","","",""],
    [10,"span","东京 Gateway 01","","",""],
    [6,"section","","回退顺序","",""],
    [8,"h3","回退顺序","","",""],
    [8,"button","层级回退","","",""],
    [8,"button","","Vanguard 自建，查看层级","",""],
    [13,"span","启动中","","",""],
    [12,"span","正在验证出口…","","",""],
    [10,"span","Vanguard","","",""],
    [10,"span","自建","","",""],
    [11,"span","2/2 可用","","",""],
    [8,"button","","Sentinel 订阅，查看层级","",""],
    [10,"span","Sentinel","","",""],
    [10,"span","订阅","","",""],
    [11,"span","4/5 · 待命","","",""],
    [8,"button","","Bastion 应急，查看层级","",""],
    [10,"span","Bastion","","",""],
    [10,"span","应急","","",""],
    [11,"span","未启用","","",""],
    [7,"button","","Gateway 固定出口，查看层级","",""],
    [8,"span","Gateway","","",""],
    [8,"span","固定出口","","",""],
    [9,"span","验证中…","","",""],
    [6,"section","","流量","",""],
    [8,"h3","流量","","",""],
    [9,"span","本次会话","","",""],
    [9,"span","","曲线时间范围","",""],
    [10,"button","24 小时","","",""],
    [10,"button","5 分钟","","",""],
    [9,"div","下载","","",""],
    [10,"span","实时","","",""],
    [11,"span","—","","",""],
    [10,"span","","0 B","",""],
    [11,"span","0","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [11,"span","B","","",""],
    [9,"div","上传","","",""],
    [10,"span","实时","","",""],
    [11,"span","—","","",""],
    [10,"span","","0 B","",""],
    [11,"span","0","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [14,"span","0","","",""],
    [14,"span","1","","",""],
    [14,"span","2","","",""],
    [14,"span","3","","",""],
    [14,"span","4","","",""],
    [14,"span","5","","",""],
    [14,"span","6","","",""],
    [14,"span","7","","",""],
    [14,"span","8","","",""],
    [14,"span","9","","",""],
    [11,"span","B","","",""],
    [7,"div","","过去 24 小时的下载与上传速率曲线","",""],
    [10,"span","0","","",""],
    [10,"span","1.5 MB/s","","",""],
    [10,"span","3 MB/s","","",""],
    [9,"span","12:00","","",""],
    [9,"span","18:00","","",""],
    [9,"span","00:00","","",""],
    [9,"span","06:00","","",""],
    [9,"span","现在","","",""],
    [9,"span","正在连接，就绪后继续采样","","",""],
    [7,"div","","网络模式、熔断保护与节点池","",""],
    [10,"span","网络模式","","",""],
    [10,"span","","网络模式","",""],
    [11,"button","系统代理","","",""],
    [11,"button","全局","","",""],
    [11,"button","混合","","",""],
    [11,"span","就绪后接管 ·","","",""],
    [12,"span","127.0.0.1:7890","","",""],
    [8,"button","","熔断保护，前往配置","",""],
    [11,"span","熔断保护","","",""],
    [13,"span","未生效","","",""],
    [12,"span","就绪后自动布防","","",""],
    [8,"button","","节点池，前往层级","",""],
    [11,"span","节点池","","",""],
    [13,"span","检测中","","",""],
    [12,"span","正在收集节点…","","",""],
    [7,"section","","协议分布","",""],
    [9,"h3","协议分布","","",""],
    [10,"span","正在加载配置…","","",""],
    [11,"span","Shadowsocks","","",""],
    [11,"span","6","","",""],
    [11,"span","VMess","","",""],
    [11,"span","3","","",""],
    [11,"span","Hysteria2","","",""],
    [11,"span","2","","",""],
    [11,"span","Trojan","","",""],
    [11,"span","2","","",""],
   ],
   tips: [
    ["button#ov-restart","重启代理核心，流量会短暂中断（约 1–2 秒）"],
    ["span#ov-prov",""],
    ["button#ov-refresh","经当前出口完整验证一次"],
    ["button#ov-copy4","复制 IPv4"],
    ["section#ov-exit>div.exit-stage>div.exit-full>div.ip-block>div.ip-meta>span.ip-net","来源报告，仅供参考"],
    ["button#ov-copy6","复制 IPv6"],
    ["button#ov-demo","服务运行时可用（原型演示控件）"],
    ["button#ov-gate","固定的最终出口，不参与回退"],
    ["span#ov-pool-dots>span.dots","Bastion 未启用"],
    ["div#ov-pbar>i","Shadowsocks · 6 个节点 · 46%"],
    ["div#ov-pbar>i","VMess · 3 个节点 · 23%"],
    ["div#ov-pbar>i","Hysteria2 · 2 个节点 · 15%"],
    ["div#ov-pbar>i","Trojan · 2 个节点 · 15%"],
   ],
  },
};

// Runs in the page: what of the overview section is not from the catalog. cjk: visible text nodes
// with CJK outside translate="no" ([path, text]); attrs: every aria-label / title / placeholder /
// data-tip in the section, on an element not inside translate="no" ([path, attr, value]). Hidden
// elements are included for the attributes (a tooltip or a reader name is read when shown).
export function overviewWordsInPage() {
  const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/;
  const root = document.querySelector('section[data-page="overview"]');
  const out = { cjk: [], attrs: [] };
  if (!root) return out;
  const dnt = el => !!el.closest('[translate="no"]');
  const path = el => {
    const parts = [];
    for (let e = el; e && e !== root; e = e.parentElement) {
      let p = e.localName;
      if (e.id) { parts.unshift(p + '#' + e.id); break; }
      const cls = Array.from(e.classList).filter(c => !/^is-|^swap/.test(c)).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      parts.unshift(p);
    }
    return parts.join('>');
  };
  const vis = el => el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || dnt(el) || !CJK.test(n.nodeValue) || !vis(el)) continue;
    out.cjk.push([path(el), n.nodeValue.trim()]);
  }
  for (const el of [root, ...root.querySelectorAll('*')]) {
    if (dnt(el)) continue;
    for (const a of ['aria-label', 'title', 'placeholder', 'data-tip']) {
      const v = el.getAttribute(a);
      if (v) out.attrs.push([path(el), a, v]);
    }
  }
  return out;
}

/** Problems of a words report for a language: CJK outside translate="no" (not zh_CN), and in
    pseudo every attribute must be catalog text (⟦…⟧). */
export function wordProblems(report, lang) {
  const problems = [];
  // (T-P5-11: ja_JP writes kanji, so the CJK test is for the other languages; zh_CN text left in
  // a ja_JP cell is core_i18n's shellCjkInPage business)
  const cjkLeak = lang !== 'zh_CN' && lang !== 'ja_JP';
  if (cjkLeak) for (const [p, t] of report.cjk) problems.push(`CJK text outside translate="no" at ${p}: ${t}`);
  const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/;
  for (const [p, a, v] of report.attrs) {
    if (cjkLeak && CJK.test(v)) problems.push(`CJK ${a} at ${p}: ${v}`);
    else if (lang === 'pseudo' && !v.includes('⟦')) problems.push(`${a} at ${p} is not catalog text: ${v}`);
  }
  return problems;
}

// The demo control of the design target carries a CSS badge (css/design.css .demo-link::before
// content "演示"; not this ticket's file, design target only): not a catalog word of this page.
export const DEMO_BADGE = '^演示$';
// T-P5-11 (ruling 3): the nine product languages, for the real-language cells of the overview's cases
export const LANGS9 = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];

/** In a page on #overview (sim mode): switch the language to `lang`, report, switch back to zh_CN.
    Returns {report, leaks, tree0, tree1}: the words report and the matrix's leak check of the
    page region in `lang`, overviewTreeInPage() before and after the round trip. */
export async function relabelRoundTrip(page, lang = 'pseudo') {
  const settle = async () => { await page.evaluate(() => window.__fixtureClock.advance(0)); await page.waitForTimeout(80); await page.evaluate(() => window.__fixtureClock.advance(0)); };
  await settle();
  const tree0 = await page.evaluate(overviewTreeInPage);
  await page.evaluate(l => T.i18n.set(l), lang);
  await settle();
  const report = await page.evaluate(overviewWordsInPage);
  const checks = await page.evaluate(cellChecksInPage, { pseudo: lang === 'pseudo', whitelist: [...LEAK_WHITELIST, DEMO_BADGE] });
  await page.evaluate(() => T.i18n.set('zh_CN'));
  await settle();
  const tree1 = await page.evaluate(overviewTreeInPage);
  return { report, leaks: checks.leaks.filter(l => l.in_pages), tree0, tree1 };
}

// Fixture-mode cases (ASCII scenarios, no simulator): every cell must show no CJK outside
// translate="no" unless zh_CN, and in pseudo every attribute of the page must be catalog text.
const RUN_OF = { overview_i18n_running: 'running', overview_i18n_stopped: 'stopped', overview_i18n_starting: 'starting' };
const words = async ({ page, lang, scenario }) => {
  const problems = [];
  const run = await page.evaluate(() => document.getElementById('app').getAttribute('data-run'));
  const want = RUN_OF[scenario.name];
  if (want && run !== want) problems.push(`#app data-run is ${run}, expected ${want}`);
  const shown = await page.evaluate(() => { const s = document.querySelector('section[data-page="overview"]'); return !!s && !s.hidden; });
  if (!shown) problems.push('the overview page is not shown');
  const report = await page.evaluate(overviewWordsInPage);
  problems.push(...wordProblems(report, lang));
  return { problems, metrics: { overview_attrs: report.attrs.length, overview_cjk_text: report.cjk.length } };
};

// zh_CN: every check of the matrix (overflow included). pseudo: the screenshot is for leaks only
// (ticket: overflow belongs to T-P4-10). Each pseudo overflow is allowed by name with its reason
// (ruling 5th wave T-P4-10 #8; the short-label factor is #3, T-P4-H), so every other check of the
// matrix (leaks, injection, console, garbage, host_scroll) still counts. T-P4-05a fixed the
// sideways scroll of #host (the 网络模式 segments, the strip labels and 重启服务 now yield inside
// their column) and dropped the known_fail; it reviewed the entries below and keeps them: each
// is a one-line control that ellipsizes (or clips inside a fixed shape) by design, and the long
// form is in a tip or the next page. The leak verdict in pseudo is also hard in
// ui/test/unit/overview_i18n.test.mjs (same scenarios, the matrix's own leak check).
export const PSEUDO_OVERFLOW = [
  // (T-P5-13: narrowed to the failure word; the run word steps out instead, overview.js fitRun.
  // Fix round 1: the checker measures the ellipsis on the content box to a fraction of a pixel and
  // finds the 重试 link of the same pill cut by about 2 px as well)
  { selector: '^#run > span\\.run-layer[^ >]* > span\\.run-row > span\\.run-read[^ >]* > span\\.run-(?:err|link)[^ >]*$', why: '伪语言（T-P5-13 省略号规则）：启动失败、核心仍在运行时（G05），运行胶囊一行里是失败词、「重试」链接和停止段；胶囊定宽（重启服务此时让出的位置已经给了它），伪语言补长后失败词差约 3px、「重试」差约 2px（修复第 1 轮起按内容盒、小数精度量），以省略号结束，完整原因在总览提示行，「重试」是整个胶囊的点击区（run-main）。9 种真实语言同一用例完整显示，由全语言矩阵核对' },
  // narrowed in the 05a fix round 1 (review 05a), again in T-P5-13: one face each, never a whole
  // box or card (an overflow elsewhere in a card or a hint box still fails)
  // (T-P5-13: the hint, the idle line and the route line wrap; the Gateway line's node name
  // gives way first: what is left is the pseudo stale suffix)
  { selector: '^#ov-gate-stat > span\\.sw > span\\.ov-stale[^ >]*$', why: '伪语言（T-P5-13）：Gateway 行（层卡下方，高度是层卡的叠放步长，单行）在数据陈旧时的「数据未更新」后缀；节点名（数据）先让到 2em，伪语言补长的后缀仍放不下而被行裁掉。9 种真实语言同一用例完整显示，由全语言矩阵核对' },
  { selector: '^#ov-deck > button\\.dcard[^ >]* > span\\.dcard-id[^ >]* > span\\.dc-stat[^ >]* > span\\.sw( |$)', why: '伪语言（T-P5-13 改写理由）：层卡底边的状态句单行（底边高度是层卡的叠放步长）；角色词、点阵先让（overview.js fitBands）之后，伪语言补长仍放不下而被裁。9 种真实语言同一用例完整显示（T-P5-11 起由全语言矩阵核对）' },
  { selector: '^#ov-chart > div\\.ch-y', why: 'chart y-axis unit labels in a fixed gutter; pseudo length (T-P4-H)' },
  { selector: '^#ov-live-[du] > span$', why: '伪语言：流量合计旁「实时」速率的标签，伪语言的波浪号补长是一个不能换行的词，1060 下宽过一列合计；真实语言的速率换到标签下一行（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
];
export default Object.keys(RUN_OF).flatMap(name => [
  { name, scenario: name, page: 'overview', langs: LANGS9, expect: words },
  {
    name: `${name}_pseudo`, scenario: name, page: 'overview', langs: ['pseudo'], expect: words, leak_allow: [DEMO_BADGE],
    overflow_allow: PSEUDO_OVERFLOW,
  },
]);
