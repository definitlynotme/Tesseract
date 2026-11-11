/* ==========================================================================
   design/demo.js — the design target's demo controls (T-P4-07; 决定_第八波裁定, T-P4-07
   Claude 补充). Imported by boot/data-design.js only, so the app target carries none of it
   (scan.mjs --rule demo: no demo marker in the app bundle or in ui/src outside design/).
   It registers at the pages' extension points (T.ext) and on the shell's command palette:
     overview/deck   the 层级回退 / 恢复 Vanguard control after the deck header (design-time
                     literals: the overview.demo.* messages were deleted from the catalog,
                     T-P4-07 fix round 1, like shell.demo.*)
     logs/advice     B-22's prototype suggestion cards, by event key (design/logs_demo.js FIX);
                     the card itself is the logs page's, shown only when T.rt.feature('B-22')
     palette         the 演示（原型） block, last (groupLast), its words marked as not catalog
                     words (data: translate="no"), and window.demo for QA
   The words of the deck control, the palette block and window.demo are the prototype's own
   (design target only, never in a catalog: the shell.demo.* and overview.demo.* messages were
   deleted with this move; scan.mjs --rule demo keeps both prefixes out of the app bundle). In fixture mode
   (render tests) the controls are drawn the same but do nothing: the simulator is not running.
   ========================================================================== */
import { FIX } from './logs_demo.js';

(function () {
  'use strict';
  const T = window.T;
  // (a partial T, e.g. a unit test that boots only the design hooks: nothing to register at)
  if (!T || !T.ext || !T.cmd || !T.store) return;
  const S = T.store;
  const live = () => !window.__TESSERACT_FIXTURE__;
  const sim = () => (live() && T.sim ? T.sim : null);
  const scenario = name => { const s = sim(); return s && typeof s.scenario === 'function' ? s.scenario(name) : false; };
  const inFallback = () => !!(S.get('fallback') || S.get('degraded'));
  const runState = () => S.get('run') || 'unknown';
  const tierName = id => (T.data.tier(id) ? T.data.tier(id).name : String(id));
  const tierLabel = tierName;
  function goThen(id, page, params) {
    if (T.go(page, params)) { T.dismiss(id); return; }
    const off = T.on('page:change', e => { off(); if (e.id === page) T.dismiss(id); });
  }

  /* ---- overview: the 层级回退 control after the deck header (mapping_overview §5) ---- */
  let deckBtn = null;
  T.ext.set('overview/deck', {
    // after: the deck header element (#ov-deck-meta); called on every render of the page
    mount(after) {
      // (the prototype's words, not catalog words: translate="no" like the palette block)
      deckBtn = T.h('button', { class: 'demo-link', id: 'ov-demo', type: 'button', translate: 'no' });
      deckBtn.addEventListener('click', () => { scenario(inFallback() ? 'recover' : 'fallback'); });
      after.after(deckBtn);
    },
    // called whenever the deck repaints: run state and whether PROXY-FALLBACK is in fallback
    sync({ run, fallback }) {
      if (!deckBtn) return;
      const scen = !!fallback || !!S.get('degraded');
      deckBtn.textContent = scen ? `恢复 ${tierName('vanguard')}` : '层级回退';
      deckBtn.disabled = run !== 'running';
      deckBtn.setAttribute('data-tip', run !== 'running' ? '服务运行时可用（原型演示控件）'
        : scen ? `原型演示控件：让 ${tierName('vanguard')} 恢复，流量切回` : `原型演示控件：模拟 ${tierName('vanguard')} 变慢、回退到 ${tierName('sentinel')}`);
    },
  });

  /* ---- logs: B-22's prototype suggestion cards by event key (the contract has no advice field) ---- */
  const ADVICE_KEYS = { 'runtime.desktop_failed': 'port', 'update.sources_failed': 'selfheal', 'proxy.system_overridden': 'drift' };
  T.ext.set('logs/advice', r => {
    const k = r && ADVICE_KEYS[r.key];
    return k && FIX[k] ? FIX[k] : null;
  });

  /* ---- palette: the demo block, after every other block ---- */
  T.cmd.register({
    id: 'demo-fallback', group: '演示（原型）', groupLast: true, data: true, icon: 'tiers', mutates: true,
    label: () => (inFallback() ? `恢复 ${tierName('vanguard')}` : '模拟层级回退'),
    keywords: 'demo fallback recover yanshi 演示 回退',
    disabled: () => runState() !== 'running',
    run: () => scenario(inFallback() ? 'recover' : 'fallback'),
  });

  /* ---- QA hooks: window.demo ---- */
  const instant = fn => {
    document.documentElement.classList.add('no-anim');
    try { fn(); } finally { requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.classList.remove('no-anim'))); }
  };
  window.demo = {
    setState(s) { if (s !== 'running' && s !== 'stopped') return; const m = sim(); if (m) instant(() => { T.notices.clearAll(true); m.setState(s); }); },
    notify(kind) {
      if (kind === 'success') return void T.notify({ kind: 'success', message: T.t('shell.notice.started') });
      if (kind === 'error') {
        return void T.notify({ kind: 'error', id: 'demo-error', title: '无法启动服务', message: '当前网络无法访问任何节点来源：3 个订阅地址均请求超时。请检查网络连接，或完成登录认证后重试。',
          actions: [{ label: T.t('common.action.retry'), primary: true, run: () => { const m = sim(); if (m && S.get('run') === 'stopped') m.start(); } }, { label: T.t('common.action.view_logs'), keep: true, run: () => goThen('demo-error', 'logs') }] });
      }
      // (the prototype's sentences with the tier names filled in; T.notify shows a message as text)
      if (kind === 'warning') {
        const body = `${tierName('vanguard')} 与 ${tierName('sentinel')} 暂不可用，流量已由 ${tierLabel('bastion')} 承载。应急节点为公共节点，请勿登录任何账号。`;
        return void T.notify({ kind: 'warning', id: 'demo-warning', title: T.t('shell.notice.fallback.title', { to: tierLabel('bastion') }),
          message: body,
          actions: [{ label: T.t('shell.notice.fallback.action'), keep: true, run: () => goThen('demo-warning', 'profiles', { tier: 'bastion' }) }] });
      }
      const info = `订阅已更新：${tierName('sentinel')} 新增 2 个节点。`;
      T.notify({ kind: 'info', message: info });
    },
    fallback() { if (S.get('run') !== 'running') window.demo.setState('running'); return scenario('fallback'); },
    recover() { return scenario('recover'); },
    admin(on) { S.set('admin', on !== false); },            // what 配置's 以普通身份运行 / 以管理员身份运行 demo link does
    go: (id, o) => T.go(id, o),
  };
})();
