// Shell-layer cases of the render matrix (T-P4-04a): the sidebar, the top bar, the command
// palette, a toast, a banner and the save bar, on the four pages of the design target, in the
// fixture mode (rt_connect_replay with a fuller traffic.history, as in smoke.mjs).
//
// Regression check (critique E1): in zh_CN every case compares its *shell text tree* (the
// textree rows of .side, .topbar, #banners, .savebar and the command palette, same row format
// as the P4-01 baseline) with the tree captured from the prototype code before the extraction
// (EXPECTED below, recorded at both sizes with CORE_I18N_RECORD=<dir>). The only accepted
// differences are listed in ALLOWED (each with its reason; T-P4-04a report §5).
// In the pseudo language the leak check of run.mjs is hard for the shell region (index.html,
// core.js and app.js no longer carry @i18n-pending); expect() additionally asserts that no shell
// row carries a CJK character outside translate="no". Pseudo-only cases (no pre-extraction tree):
// an rt error and a wordless error in the save bar, the bar's leave question, a drawer's close
// button. SHELL_OVERFLOW lists the shell truncations that only longer words show (the save
// bar's effect line; the sidebar footer's two words left it in T-P4-06, 6th-wave ruling): not
// this ticket's layout, reported for a ruling; the leaves only, and a zh_CN cell that hits one of
// them fails (expect()).
// zh_CN also checks what the row tree leaves out (fix round 1): the shell roots' own names
// (.side, .savebar) and the dialogs in #layer (the save bar's leave question, the mode switch
// confirmation), against the prototype's words (EXTRA_* below).
// The page regions (#pages) belong to their own 04 / 05 tickets. The shell_<page> cells show a
// real page, so its leaks and truncations show up there (they are the page tickets' to fix);
// every other cell that runs in the pseudo language first moves to an empty page (BLANK, with
// the case page's title, so the crumb and the window title are as recorded), so its verdict is
// the shell's alone.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readCatalogs } from '../../../build/locales.mjs';
// the overview page's own documented pseudo allowances (T-P4-04b / 05a), for the shell_overview
// pseudo cells as smoke.mjs uses them (6th-wave precedent; 8th-wave ruling T-P4-H2 item 6)
import { DEMO_BADGE, LANGS9, PSEUDO_OVERFLOW } from './overview_i18n.mjs';
// ...and the settings page's (T-P4-04e / 05d), for the shell_settings pseudo cells
import { PSEUDO_LENGTH as SETTINGS_PSEUDO, pseudoBreaks } from './settings_i18n.mjs';

function runningScenario() {
  const sc = JSON.parse(readFileSync(new URL('../../fixtures/scenarios/rt_connect_replay.json', import.meta.url), 'utf8'));
  const step = sc.steps.find(s => s.deliver && s.deliver.topic === 'traffic.history');
  const at = step.deliver.data.sampled_at_ms;
  const wave = (i, base) => Math.round(base * (1 + 0.5 * Math.sin(i / 2)));
  step.deliver.data.h24 = Array.from({ length: 12 }, (_, i) => ({ t_ms: at - (12 - i) * 300000, down_bps: wave(i, 4096), up_bps: wave(i + 3, 1024), sample_count: 300 }));
  step.deliver.data.live = Array.from({ length: 30 }, (_, i) => ({ t_ms: at - (30 - i) * 1000, down_bps: wave(i, 8192), up_bps: wave(i + 5, 2048) }));
  // T-P4-06: the palette's mode commands follow hello.caps (a command the runtime does not offer
  // is disabled, mapping_core S12); the mode confirmation case needs mode.set offered
  const hello = sc.steps.find(s => s.deliver && s.deliver.t === 'hello').deliver;
  hello.caps.commands = [...hello.caps.commands, 'mode.set'];
  return { ...sc, name: 'core_i18n_running', desc: `${sc.desc} (core_i18n: fuller traffic.history, as smoke.mjs; mode.set offered)` };
}
const RUNNING = runningScenario();

// Runs in the page: the textree rows (same format as textree.mjs) of the shell regions only.
function shellTreeInPage() {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const ROOTS = ['.side', '.topbar', '#banners', '.savebar', '.cmdk'];
  const out = [];
  const visit = (el, depth) => {
    for (const c of el.children) {
      if (!c.checkVisibility({ opacityProperty: true, visibilityProperty: true })) {
        if (getComputedStyle(c).display !== 'none') visit(c, depth + 1);
        continue;
      }
      let own = '';
      for (const n of c.childNodes) if (n.nodeType === 3) own += n.nodeValue;
      const row = [depth, c.tagName.toLowerCase(), norm(own), norm(c.getAttribute('aria-label')), norm(c.getAttribute('title')), norm(c.getAttribute('placeholder'))];
      if (row[2] || row[3] || row[4] || row[5]) out.push(row);
      visit(c, depth + 1);
    }
  };
  for (const sel of ROOTS) {
    for (const r of document.querySelectorAll(sel)) {
      if (!r.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
      out.push([0, '#root', sel, '', '', '']);
      visit(r, 1);
    }
  }
  // the document title and the brand's title are shell text too
  out.push([0, '#title', norm(document.title), '', norm(document.querySelector('.brand') && document.querySelector('.brand').getAttribute('title')), '']);
  return out;
}

// zh_CN catalog text that a non-zh_CN cell must not show (T-P5-03 #5, P5 §6 #35). The old rule
// (any CJK character outside translate="no") flagged correct Japanese (概要 / 階層 / 設定). Now a
// shell text is "zh_CN fallback" when
//   (a) trace: its message id (data-i18n-id on it or an ancestor; for T.t strings and attribute
//       values the T.i18n.traced() index) is in no catalog of the current language, and the
//       text has a CJK character (en_US fallback text has none), or
//   (b) text: it contains a zh_CN catalog fragment (a run of 2+ Han characters of a zh_CN
//       message; ICU placeholders and tags are ASCII, so every run is literal text) that no
//       message of the current language contains.
// (b) catches what the trace cannot attribute (a zh_CN string concatenated into another one,
// a build without --i18n-trace). Data under translate="no" is never read. Proof that it still
// reports a real fallback: the case core_i18n_leak_selftest below.
const HAN_RUN = /[㐀-䶿一-鿿豈-﫿]{2,}/gu;
const UI_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
let catalogs = null;
const leakFrags = new Map(); // lang → [fragment], longest first
export function zhFragmentsAbsentFrom(lang) {
  if (leakFrags.has(lang)) return leakFrags.get(lang);
  catalogs = catalogs || readCatalogs(UI_ROOT);
  const zh = new Set();
  for (const { text } of Object.values(catalogs.zh_CN || {})) for (const m of String(text || '').match(HAN_RUN) || []) zh.add(m);
  const mine = Object.values(catalogs[lang] || {}).map(e => String(e.text || '')).join('\n');
  const out = [...zh].filter(f => !mine.includes(f)).sort((a, b) => b.length - a.length);
  leakFrags.set(lang, out);
  return out;
}

// Runs in the page: shell text (text nodes, text attributes, the document title) that is zh_CN
// catalog text in a cell of another language (see above). arg = {lang, frags}.
function shellCjkInPage({ lang, frags }) {
  const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/;
  const ROOTS = ['.side', '.topbar', '#banners', '.savebar', '.cmdk'];
  const traced = window.T && T.i18n && typeof T.i18n.traced === 'function' ? T.i18n.traced() : {};
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const tracedIds = s => traced[s] || traced[norm(s)] || [];
  const missing = id => !T.i18n.has(id, lang);
  const why = (text, ids) => {
    if (!CJK.test(text)) return null;
    const gone = ids.filter(missing);
    if (ids.length && gone.length === ids.length) return `id ${gone[0]} not in ${lang}`;
    const f = frags.find(x => text.includes(x));
    return f ? `zh_CN text "${f}"` : null;
  };
  const bad = [];
  for (const sel of ROOTS) {
    for (const r of document.querySelectorAll(sel)) {
      const w = document.createTreeWalker(r, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let n = w.currentNode; n; n = w.nextNode()) {
        const el = n.nodeType === 1 ? n : n.parentElement;
        if (el.closest('[translate="no"]')) continue;
        if (n.nodeType === 3) {
          if (!el.checkVisibility()) continue;
          const host = el.closest('[data-i18n-id]');
          const own = [...el.childNodes].filter(c => c.nodeType === 3).map(c => c.nodeValue).join('');
          const r2 = why(n.nodeValue, host ? [host.getAttribute('data-i18n-id')] : tracedIds(own));
          if (r2) bad.push(`${sel}: ${n.nodeValue.trim().slice(0, 40)} (${r2})`);
          continue;
        }
        for (const a of ['aria-label', 'title', 'placeholder', 'data-tip']) {
          const v = n.getAttribute(a);
          const r2 = v && why(v, tracedIds(v));
          if (r2) bad.push(`${sel} [${a}]: ${v.slice(0, 40)} (${r2})`);
        }
      }
    }
  }
  // the document title is composed (product · page): checked by its text only
  const tw = why(document.title, []);
  if (tw) bad.push(`title: ${document.title} (${tw})`);
  return bad;
}

// Accepted differences against the pre-extraction tree (zh_CN). Each entry: a row index-free
// rewrite applied to the *expected* rows before the comparison, with the reason.
// T-P4-06 (runtime facts instead of the prototype's demo data; T-P4-06 report, ruling asked):
const swapCol = (col, from, to) => rows => rows.map(r => (r[col] === from ? [...r.slice(0, col), to, ...r.slice(col + 1)] : r));
const dropRow = pred => rows => rows.filter(r => !pred(r));
export const ALLOWED = [
  // the footer's version is the running app's (Update.current_version / hello.app_version: 1.0.0
  // in the fixture), not the design data's 0.1.0
  { why: 'footer version from hello.app_version', apply: swapCol(2, '0.1.0', '1.0.0') },
  // no update.state in the fixture: the footer claims nothing (「检查更新」), never 「已是最新」
  { why: 'footer update word without update.state', apply: rows => swapCol(3, '已是最新，前往配置 · 应用', '检查更新，前往配置 · 应用')(swapCol(2, '已是最新', '检查更新')(rows)) },
  // the palette's copy command shows a verified current IPv4 only (G07): the fixture has no exit.identity
  { why: 'no copyable exit address in the fixture', apply: dropRow(r => r[1] === 'span' && r[2] === '203.0.113.24') },
  // mode hints are facts of network.mode (absent in the fixture): no 「当前」 for an unknown mode, no
  // 「需要管理员权限」 unless network.mode.admin is false
  { why: 'mode hints only from network.mode', apply: dropRow(r => r[0] === 4 && r[1] === 'span' && (r[2] === '当前' || r[2] === '需要管理员权限')) },
  // tier notices say what was observed (PROTOCOL §5.3; T-P4-06 requirement 11)
  { why: 'observed wording of the tier notices', apply: rows => [
    ['已回退到 Sentinel 订阅', '观察到层级回退：现在由 Sentinel 订阅 承载'],
    ['Vanguard 自建 暂不可用，流量已由 Sentinel 订阅 承载；Vanguard 恢复后会自动切回。', '观察到 Vanguard 自建 暂不可用，流量已由 Sentinel 订阅 承载；Vanguard 恢复后会自动切回。'],
    ['Vanguard 自建 已恢复，流量已切回 Vanguard。', '观察到 Vanguard 自建 已恢复，流量已切回 Vanguard。'],
  ].reduce((out, [a, b]) => swapCol(2, a, b)(out), rows) },
  // F-01 (user feedback 10-27): a notice says what happened, not how the product works (the
  // fallback banner's title says who carries now)
  { why: 'F-01: notices without explanations', apply: rows => [
    ['服务已启动，可以正常连接。', '服务已启动。'],
    ['观察到 Vanguard 自建 暂不可用，流量已由 Sentinel 订阅 承载；Vanguard 恢复后会自动切回。', 'Vanguard 自建 暂不可用。'],
  ].reduce((out, [a, b]) => swapCol(2, a, b)(out), rows) },
  // F-03 (user 10-28): a tier is named by its code name alone (自建 · 订阅 · 应急 · 固定出口 were the
  // product's advice, not interface text): the role rows go, and a name + role keeps the name
  { why: 'F-03: no tier role words', apply: rows => rows
    .filter(r => !(r[1] === 'span' && ['自建', '订阅', '应急', '固定出口'].includes(r[2])))
    .map(r => r.map(v => (typeof v === 'string' ? v.replace(/\b(Vanguard|Sentinel|Bastion|Gateway) (自建|订阅|应急|固定出口)/g, '$1') : v))) },
  // F-04 (user 11-01): the tier notices drop the 观察到 hedge: the banner title is the notice card's
  // 已回退到 {tier}, a recovery says where traffic is back
  { why: 'F-04: tier notices without the observed hedge', apply: rows => [
    ['观察到层级回退：现在由 Sentinel 承载', '已回退到 Sentinel'],
    ['观察到 Vanguard 已恢复，流量已切回 Vanguard。', '已回到 Vanguard。'],
  ].reduce((out, [a, b]) => swapCol(2, a, b)(out), rows) },
  // F-05 (user 11-04): banners float in a stack under the top bar and a capsule in the top bar
  // stands for them. An empty stack is tucked away (no #banners root row); with banners on
  // screen the capsule (left of 搜索与命令) is named by the newest banner's title (+ 另有 N 条提醒)
  { why: 'F-05: the banner stack and its capsule', apply: rows => {
    const i = rows.findIndex(r => r[0] === 0 && r[2] === '#banners');
    if (i < 0) return rows;
    if (!rows[i + 1] || rows[i + 1][0] === 0) return rows.filter((r, k) => k !== i);
    const after = rows.slice(i + 1), end = after.findIndex(r => r[0] === 0);
    const stack = end < 0 ? after : after.slice(0, end);
    const n = stack.filter(r => r[1] === 'button' && r[3] === '关闭通知').length;
    const label = stack[0][2] + (n > 1 ? ` · 另有 ${n - 1} 条提醒` : '');
    const cmd = rows.findIndex(r => r[1] === 'button' && r[3] === '搜索与命令（Ctrl+K）');
    return cmd < 0 ? rows : [...rows.slice(0, cmd), [1, 'button', '', label, '', ''], ...rows.slice(cmd)];
  } },
  // 配置整理 P1 (user 11-04): 网络模式 is 接管方式; the TUN mode is 虚拟网卡模式 (全局 meant TUN, not
  // Clash's global) and mixed is 系统代理 + 虚拟网卡: the mode notice and the palette's mode group say so
  { why: 'P1: 接管方式 wording', apply: rows => [
    ['网络模式已切换为全局模式。', '接管方式已切换为虚拟网卡模式。'],
    ['切换网络模式', '切换接管方式'],
    ['全局模式（TUN）', '虚拟网卡模式（TUN）'],
    ['混合模式', '系统代理 + 虚拟网卡'],
  ].reduce((out, [a, b]) => swapCol(3, a, b)(swapCol(2, a, b)(out)), rows) },
];

const rowKey = r => JSON.stringify(r);
function applyAllowed(rows) {
  let out = rows.map(r => r.slice());
  for (const a of ALLOWED) out = a.apply(out);
  return out;
}
function diffRows(want, got) {
  const problems = [];
  const n = Math.max(want.length, got.length);
  for (let i = 0; i < n; i++) {
    if (rowKey(want[i]) !== rowKey(got[i])) { problems.push(`row ${i}: expected ${rowKey(want[i])}, got ${rowKey(got[i])}`); if (problems.length > 12) break; }
  }
  return problems;
}

// Runs in the page: the shell roots' own names and the dialogs open in #layer (title, message,
// buttons), the parts the row tree does not carry.
function shellExtraInPage() {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => el && el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const roots = {};
  for (const sel of ['.side', '.topbar', '#banners', '.savebar', '.cmdk']) {
    const r = document.querySelector(sel);
    if (vis(r)) roots[sel] = norm(r.getAttribute('aria-label'));
  }
  const dialogs = [...document.querySelectorAll('#layer .dlg')].filter(vis).map(d => ({
    title: norm(d.querySelector('.dlg-title') && d.querySelector('.dlg-title').textContent),
    message: norm(d.querySelector('.dlg-msg') && d.querySelector('.dlg-msg').textContent),
    buttons: [...d.querySelectorAll('.dlg-actions button')].map(b => norm(b.textContent)),
  }));
  return { roots, dialogs };
}
// The prototype's words (.build/claude_ui_prototype: index.html aside, core.js saveBar and
// confirmLeave, data.js modeSwitch with app.js confirmLabel 切换), zh_CN.
const EXTRA_ROOTS = { '.side': '侧边栏', '.topbar': '', '#banners': '', '.savebar': '未保存的更改', '.cmdk': '' };
const EXTRA_DIALOGS = {
  // (F-04: without 可以留下来保存，或放弃这些更改: the two buttons say it)
  savebar_leave: [{ title: '有 3 项更改未保存', message: '离开后这些更改不会生效。', buttons: ['留下', '放弃更改'] }],
  // (F-01: the consequence without its duration, mode.switch.confirm.body)
  // (配置整理 P1, 11-04: the TUN mode is 虚拟网卡模式, not 全局模式)
  confirm_mode: [{ title: '切换到虚拟网卡模式？', message: '代理服务会重启，流量会短暂中断。虚拟网卡会接管本机全部流量。', buttons: ['取消', '切换'] }],
};

function shellExpect(name) {
  return async ({ page, lang, size, checks }) => {
    const tree = await page.evaluate(shellTreeInPage);
    const problems = [];
    // SHELL_OVERFLOW and DLG_MSG are for longer languages only: in zh_CN those truncations fail
    if (lang === 'zh_CN') {
      for (const t of (checks && checks.truncations) || []) {
        if (LONG_ONLY().some(a => new RegExp(a.selector, 'u').test(t.selector))) problems.push(`zh_CN truncation in an allowed-for-longer-languages place: ${t.selector}`);
      }
    }
    if (process.env.CORE_I18N_RECORD && lang === 'zh_CN') {
      mkdirSync(process.env.CORE_I18N_RECORD, { recursive: true });
      writeFileSync(join(process.env.CORE_I18N_RECORD, `${name}.${size}.json`), JSON.stringify(tree) + '\n');
      return { problems };
    }
    if (lang === 'zh_CN') {
      const want = EXPECTED[`${name}.${size}`];
      // (the dialog cases added in fix round 1 have no pre-extraction row tree: their dialog is
      // checked against EXTRA_DIALOGS, the prototype's words)
      if (!want) { if (!EXTRA_DIALOGS[name]) problems.push(`no expected shell tree for ${name}.${size}`); }
      else problems.push(...diffRows(applyAllowed(want), tree));
      const extra = await page.evaluate(shellExtraInPage);
      for (const [sel, label] of Object.entries(extra.roots)) if (label !== EXTRA_ROOTS[sel]) problems.push(`root ${sel}: aria-label ${JSON.stringify(label)}, expected ${JSON.stringify(EXTRA_ROOTS[sel])}`);
      const wantDlg = EXTRA_DIALOGS[name] || [];
      if (JSON.stringify(extra.dialogs) !== JSON.stringify(wantDlg)) problems.push(`dialogs: ${JSON.stringify(extra.dialogs)}, expected ${JSON.stringify(wantDlg)}`);
    } else {
      for (const b of await page.evaluate(shellCjkInPage, { lang, frags: lang === 'pseudo' ? [] : zhFragmentsAbsentFrom(lang) })) problems.push(`zh_CN catalog text in ${lang}: ${b}`);
    }
    return { problems, metrics: { shell_rows: tree.length } };
  };
}

const ev = (fn, arg) => ({ eval: fn, arg });
const settle = { page_ms: 1200 };

// Truncations of the shell's own layout that only longer words show (the pseudo language: 3
// Latin letters per CJK character + brackets). Not clipping by design: the layout is not this
// ticket's (css/shell.css, css/components.css) and the save bar is reworked by T-P4-05d. Listed
// so the cases keep checking everything else; T-P4-04a report §8 asks for a ruling.
// (T-P5-13, 决定_P5裁定 P5-e #5: the save bar's effect line wraps now and is read whole in every
// language, so its allowance is gone; the list stays for LONG_ONLY)
const SHELL_OVERFLOW = [];

// a confirmation's sentence in the pseudo language: built from zh_CN it has no spaces (one long
// token), so it cannot wrap; real languages wrap at spaces (pseudo.js, T-P4-H)
const DLG_MSG = { selector: '^#layer > div\\.ovl[^ ]* > div\\.dlg[^ ]* > p\\.dlg-msg$', why: 'pseudo text built from zh_CN has no spaces (one long token): a confirmation sentence cannot wrap; real languages wrap at spaces. Ruling asked in the T-P4-04a report (pseudo.js, T-P4-H)' };
// allowed for longer languages only: a zh_CN cell that truncates in one of these places fails
const LONG_ONLY = () => [...SHELL_OVERFLOW, DLG_MSG];

// the empty page the shell-only cells move to (with the case page's title: the crumb and the
// window title stay as recorded)
const BLANK = page => [ev(id => { T.page('t04a_blank', { titleId: `shell.page.${id}.title` }); T.go('t04a_blank', { instant: true }); }, page), { page_ms: 600 }];

// the cells with no recorded zh_CN tree run in the other eight languages and the pseudo language
const REAL8_PSEUDO = [...LANGS9.filter(l => l !== 'zh_CN'), 'pseudo'];

const CASES = [
  // the four pages: sidebar (nav, footer), top bar (crumb, search button), document title
  // zh_CN strict; the pseudo cell carries the overview page's own allowances (its region shows a
  // real page: the overview's one-line faces ellipsize by design, the design-only 演示 badge)
  // (T-P5-11, ruling 3: the real-language cells run in all nine languages, no page allowance)
  { name: 'shell_overview', page: 'overview', langs: LANGS9 },
  { name: 'shell_overview_pseudo', page: 'overview', langs: ['pseudo'], leak_allow: [DEMO_BADGE], overflow_allow: PSEUDO_OVERFLOW },
  { name: 'shell_profiles', page: 'profiles' },
  { name: 'shell_logs', page: 'logs', overflow_allow: [{ selector: '^#logs-events ', why: 'logs page region (T-P4-04d / 05c): a tier tag in an event row is clipped at 1060 px; present before the extraction too' }] },
  // the same split for the settings page: its pseudo cell with the page's own allowances and its
  // test-only break opportunities (settings_i18n.mjs pseudoBreaks)
  { name: 'shell_settings', page: 'settings', langs: LANGS9 },
  { name: 'shell_settings_pseudo', page: 'settings', langs: ['pseudo'], leak_allow: [DEMO_BADGE], overflow_allow: SETTINGS_PSEUDO,
    actions: [{ eval: pseudoBreaks }, { page_ms: 100 }] },
  // the command palette, full list (no pseudo cell: in pseudo the page groups' rows would be page
  // leaks in #layer; T-P5-13: the nine languages, zh_CN against its recorded tree)
  { name: 'palette_full', page: 'overview', langs: LANGS9, actions: [{ click: '#cmd-open' }, settle] },
  // the palette filtered to the shell's own commands (both languages)
  { name: 'palette_mode', page: 'logs', actions: [{ click: '#cmd-open' }, settle, { fill: ['.cmdk-in input', 'mode'] }, settle] },
  { name: 'palette_empty', page: 'logs', actions: [{ click: '#cmd-open' }, settle, { fill: ['.cmdk-in input', 'zzzz'] }, settle] },
  // a toast (service started, seen from another page) and a banner (observed tier fallback)
  { name: 'notices', page: 'logs', actions: [
    ev(() => { T.emit('rt:state', { run: 'running', prev: 'starting', instant: false }); }), settle,
    ev(() => { T.store.set('fallback', true); T.emit('rt:tier', { kind: 'fallback', carrier: 'sentinel', from: 'vanguard', instant: false }); }), settle,
  ] },
  { name: 'notice_recover', page: 'logs', actions: [
    ev(() => { T.emit('rt:tier', { kind: 'recover', carrier: 'vanguard', wasFallback: true, instant: false }); }), settle,
  ] },
  { name: 'notice_mode', page: 'logs', actions: [
    ev(() => { T.emit('rt:mode', { mode: 'tun', busy: false, instant: false }); }), settle,
  ] },
  // the save bar: dirty, then a failed save, then a held one
  { name: 'savebar_dirty', page: 'logs', actions: [
    ev(() => { T.ui.saveBar({ count: 3, effect: 'reload', onSave: () => Promise.resolve(true), onUndo: () => {} }); }), settle,
  ] },
  { name: 'savebar_restart', page: 'logs', actions: [
    ev(() => { T.ui.saveBar({ count: 1, effect: 'restart', onSave: () => Promise.resolve(true), onUndo: () => {} }); }), settle,
  ] },
  // (the caller's own error text 'x' is shown as given; zh_CN is the regression row against its
  // recorded tree, T-P5-13: the nine languages)
  { name: 'savebar_failed', page: 'logs', langs: LANGS9, actions: [
    ev(() => { T.ui.saveBar({ count: 2, effect: 'instant', onSave: () => Promise.reject(new Error('x')), onUndo: () => {} }); }), settle,
    ev(() => { const b = T.ui.saveBarCurrent; b.save(); }), settle,
  ] },
  // an rt error: the bar says what failed through its message_key (catalog), not error.message;
  // and an error without words: the default reason (no zh_CN cell: no pre-extraction tree; T-P5-11
  // fix round 1: the eight other languages too, the failure sentence is length-sensitive)
  { name: 'savebar_failed_rt', page: 'logs', langs: REAL8_PSEUDO, actions: [
    ev(() => { T.ui.saveBar({ count: 2, effect: 'instant', onSave: () => Promise.reject(Object.assign(new Error('internal'), { code: 'internal', message_key: 'errors.internal' })), onUndo: () => {} }); }), settle,
    ev(() => { const b = T.ui.saveBarCurrent; b.save(); }), settle,
  ] },
  { name: 'savebar_failed_default', page: 'logs', langs: REAL8_PSEUDO, actions: [
    ev(() => { T.ui.saveBar({ count: 2, effect: 'instant', onSave: () => Promise.reject(new Error('')), onUndo: () => {} }); }), settle,
    ev(() => { const b = T.ui.saveBarCurrent; b.save(); }), settle,
  ] },
  // the leave question of the save bar (T.confirm with the bar's words) and a drawer's close button
  { name: 'savebar_leave', page: 'logs', overflow_allow: [DLG_MSG], actions: [
    ev(() => { T.ui.saveBar({ count: 3, effect: 'reload', onSave: () => Promise.resolve(true), onUndo: () => {} }); T.ui.saveBarCurrent.confirmLeave(); }), settle,
  ] },
  // the mode switch confirmation (palette → 全局模式 while running; administrator, so it is not gated)
  { name: 'confirm_mode', page: 'logs', overflow_allow: [DLG_MSG], actions: [
    ev(() => { T.store.set('admin', true); }), { click: '#cmd-open' }, settle, { fill: ['.cmdk-in input', 'tun'] }, settle, { press: 'Enter' }, settle,
  ] },
  { name: 'drawer_close', page: 'logs', langs: REAL8_PSEUDO, actions: [
    ev(() => { T.drawer({ title: 'Tesseract', content: document.createElement('div') }); }), settle,
  ] },
  { name: 'savebar_held', page: 'logs', actions: [
    ev(() => { T.ui.saveBar({ count: 2, effect: 'instant', onSave: () => Promise.reject({ hold: true }), onUndo: () => {} }); }), settle,
    ev(() => { const b = T.ui.saveBarCurrent; b.save(); }), settle,
  ] },
  { name: 'savebar_saved', page: 'logs', actions: [
    ev(() => { T.ui.saveBar({ count: 2, effect: 'restart', onSave: () => Promise.resolve(true), onUndo: () => {} }); }), settle,
    ev(() => { const b = T.ui.saveBarCurrent; b.save(); }), { page_ms: 300 },
  ] },
];

// Recorded from the prototype code before the extraction (zh_CN, fixture mode; CORE_I18N_RECORD).
export const EXPECTED = {
  'notice_mode.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [3, "span", "网络模式已切换为全局模式。", "", "", ""],
    [3, "button", "", "关闭通知", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'notice_recover.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [3, "span", "Vanguard 自建 已恢复，流量已切回 Vanguard。", "", "", ""],
    [3, "button", "", "关闭通知", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'notices.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [3, "span", "服务已启动，可以正常连接。", "", "", ""],
    [3, "button", "", "关闭通知", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [4, "span", "已回退到 Sentinel 订阅", "", "", ""],
    [4, "span", "警告", "", "", ""],
    [3, "div", "Vanguard 自建 暂不可用，流量已由 Sentinel 订阅 承载；Vanguard 恢复后会自动切回。", "", "", ""],
    [4, "span", "查看层级", "", "", ""],
    [2, "button", "", "关闭通知", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'palette_empty.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".cmdk", "", "", ""],
    [1, "div", "", "命令面板", "", ""],
    [3, "input", "", "搜索命令", "", "搜索命令或页面…"],
    [3, "kbd", "Esc", "", "", ""],
    [3, "div", "没有匹配的命令", "", "", ""],
    [3, "span", "选择", "", "", ""],
    [4, "kbd", "↑", "", "", ""],
    [4, "kbd", "↓", "", "", ""],
    [3, "span", "执行", "", "", ""],
    [4, "kbd", "Enter", "", "", ""],
    [3, "span", "– 切换页面", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "1", "", "", ""],
    [4, "kbd", "4", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'palette_full.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "总览", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".cmdk", "", "", ""],
    [1, "div", "", "命令面板", "", ""],
    [3, "input", "", "搜索命令", "", "搜索命令或页面…"],
    [3, "kbd", "Esc", "", "", ""],
    [3, "div", "服务", "", "", ""],
    [3, "button", "", "停止服务", "", ""],
    [4, "span", "停止服务", "", "", ""],
    [3, "button", "", "重启服务", "", ""],
    [4, "span", "重启服务", "", "", ""],
    [3, "button", "", "验证出口 IP", "", ""],
    [4, "span", "验证出口 IP", "", "", ""],
    [3, "button", "", "复制出口 IPv4", "", ""],
    [4, "span", "复制出口 IPv4", "", "", ""],
    [4, "span", "203.0.113.24", "", "", ""],
    [3, "div", "前往", "", "", ""],
    [3, "button", "", "总览", "", ""],
    [4, "span", "总览", "", "", ""],
    [5, "kbd", "Ctrl", "", "", ""],
    [5, "kbd", "1", "", "", ""],
    [3, "button", "", "层级", "", ""],
    [4, "span", "层级", "", "", ""],
    [5, "kbd", "Ctrl", "", "", ""],
    [5, "kbd", "2", "", "", ""],
    [3, "button", "", "运行日志", "", ""],
    [4, "span", "运行日志", "", "", ""],
    [5, "kbd", "Ctrl", "", "", ""],
    [5, "kbd", "3", "", "", ""],
    [3, "button", "", "配置", "", ""],
    [4, "span", "配置", "", "", ""],
    [5, "kbd", "Ctrl", "", "", ""],
    [5, "kbd", "4", "", "", ""],
    [3, "button", "", "本次运行的问题", "", ""],
    [4, "span", "本次运行的问题", "", "", ""],
    [4, "span", "运行日志", "", "", ""],
    [3, "div", "切换网络模式", "", "", ""],
    [3, "button", "", "系统代理", "", ""],
    [4, "span", "系统代理", "", "", ""],
    [4, "span", "当前", "", "", ""],
    [3, "button", "", "全局模式（TUN）", "", ""],
    [4, "span", "全局模式（TUN）", "", "", ""],
    [4, "span", "需要管理员权限", "", "", ""],
    [3, "button", "", "混合模式", "", ""],
    [4, "span", "混合模式", "", "", ""],
    [4, "span", "需要管理员权限", "", "", ""],
    [3, "div", "层级", "", "", ""],
    [3, "button", "", "Vanguard 自建", "", ""],
    [6, "b", "Vanguard", "", "", ""],
    [6, "span", "自建", "", "", ""],
    [3, "button", "", "Sentinel 订阅", "", ""],
    [6, "b", "Sentinel", "", "", ""],
    [6, "span", "订阅", "", "", ""],
    [3, "button", "", "Bastion 应急", "", ""],
    [6, "b", "Bastion", "", "", ""],
    [6, "span", "应急", "", "", ""],
    [3, "button", "", "Gateway 固定出口", "", ""],
    [6, "b", "Gateway", "", "", ""],
    [6, "span", "固定出口", "", "", ""],
    // (re-recorded, T-P4-H2 item 6: the tiers page's 「更新 Sentinel 的全部订阅」 is B-06, behind
    // T.rt.feature('B-06') since T-P4-05b (P4 rule 14; tiers.js palette `when`), so a default build
    // does not list it; the order of the rest is the registration order, the same on every boot:
    // app.js registers its groups at boot, the tiers page's commands follow in setTimeout 0)
    [3, "div", "演示（原型）", "", "", ""],
    [3, "button", "", "模拟层级回退", "", ""],
    [4, "span", "模拟层级回退", "", "", ""],
    [3, "span", "选择", "", "", ""],
    [4, "kbd", "↑", "", "", ""],
    [4, "kbd", "↓", "", "", ""],
    [3, "span", "执行", "", "", ""],
    [4, "kbd", "Enter", "", "", ""],
    [3, "span", "– 切换页面", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "1", "", "", ""],
    [4, "kbd", "4", "", "", ""],
    [0, "#title", "Tesseract · 总览", "", "Tesseract · 运行中", ""],
  ],
  'palette_mode.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".cmdk", "", "", ""],
    [1, "div", "", "命令面板", "", ""],
    [3, "input", "", "搜索命令", "", "搜索命令或页面…"],
    [3, "kbd", "Esc", "", "", ""],
    [3, "div", "切换网络模式", "", "", ""],
    [3, "button", "", "系统代理", "", ""],
    [4, "span", "系统代理", "", "", ""],
    [4, "span", "当前", "", "", ""],
    [3, "button", "", "全局模式（TUN）", "", ""],
    [4, "span", "全局模式（TUN）", "", "", ""],
    [4, "span", "需要管理员权限", "", "", ""],
    [3, "button", "", "混合模式", "", ""],
    [4, "span", "混合模式", "", "", ""],
    [4, "span", "需要管理员权限", "", "", ""],
    [3, "span", "选择", "", "", ""],
    [4, "kbd", "↑", "", "", ""],
    [4, "kbd", "↓", "", "", ""],
    [3, "span", "执行", "", "", ""],
    [4, "kbd", "Enter", "", "", ""],
    [3, "span", "– 切换页面", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "1", "", "", ""],
    [4, "kbd", "4", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'savebar_dirty.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".savebar", "", "", ""],
    [4, "span", "3 项更改未保存", "", "", ""],
    [4, "span", "保存后立即生效，连接不中断", "", "", ""],
    [4, "span", "撤销", "", "", ""],
    [5, "span", "保存", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "S", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'savebar_failed.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".savebar", "", "", ""],
    [4, "span", "保存失败", "", "", ""],
    // T-P4-05d: the reason span carries its whole text as title (T.ui.saveBar); 8th-wave ruling T-P4-H2 item 6
    [3, "span", "", "", "x", ""],
    [4, "span", "x", "", "", ""],
    [4, "span", "撤销", "", "", ""],
    [5, "span", "重试", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "S", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'savebar_held.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".savebar", "", "", ""],
    [4, "span", "还不能保存", "", "", ""],
    // T-P4-05d: the reason span carries its whole text as title (T.ui.saveBar); 8th-wave ruling T-P4-H2 item 6
    [3, "span", "", "", "有一项设置需要修改", ""],
    [4, "span", "有一项设置需要修改", "", "", ""],
    [4, "span", "撤销", "", "", ""],
    [5, "span", "保存", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "S", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'savebar_restart.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".savebar", "", "", ""],
    [4, "span", "1 项更改未保存", "", "", ""],
    [4, "span", "保存后会重启代理服务，流量中断约 1–2 秒", "", "", ""],
    [4, "span", "撤销", "", "", ""],
    [5, "span", "保存", "", "", ""],
    [4, "kbd", "Ctrl", "", "", ""],
    [4, "kbd", "S", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'savebar_saved.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#root", ".savebar", "", "", ""],
    [4, "span", "已保存", "", "", ""],
    [4, "span", "代理服务已重启，连接已恢复", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'shell_logs.1060x700': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'shell_logs.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "运行日志", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 运行日志", "", "Tesseract · 运行中", ""],
  ],
  'shell_overview.1060x700': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "总览", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 总览", "", "Tesseract · 运行中", ""],
  ],
  'shell_overview.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "总览", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 总览", "", "Tesseract · 运行中", ""],
  ],
  'shell_profiles.1060x700': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "层级", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 层级", "", "Tesseract · 运行中", ""],
  ],
  'shell_profiles.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "0.1.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "层级", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 层级", "", "Tesseract · 运行中", ""],
  ],
  'shell_settings.1060x700': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "1.0.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "配置", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 配置", "", "Tesseract · 运行中", ""],
  ],
  'shell_settings.1280x800': [
    [0, "#root", ".side", "", "", ""],
    [1, "div", "", "", "Tesseract · 运行中", ""],
    [2, "span", "Tesseract", "", "", ""],
    [1, "nav", "", "主导航", "", ""],
    [3, "span", "总览", "", "", ""],
    [3, "span", "层级", "", "", ""],
    [3, "span", "运行日志", "", "", ""],
    [3, "span", "配置", "", "", ""],
    [3, "span", "1.0.0", "", "", ""],
    [4, "span", "Tesseract", "", "", ""],
    [2, "button", "", "已是最新，前往配置 · 应用", "", ""],
    [4, "span", "已是最新", "", "", ""],
    [0, "#root", ".topbar", "", "", ""],
    [2, "span", "配置", "", "", ""],
    [1, "button", "", "搜索与命令（Ctrl+K）", "", ""],
    [2, "span", "搜索与命令", "", "", ""],
    [3, "kbd", "Ctrl", "", "", ""],
    [3, "kbd", "K", "", "", ""],
    [0, "#root", "#banners", "", "", ""],
    [0, "#title", "Tesseract · 配置", "", "Tesseract · 运行中", ""],
  ],
};

// Self-check of the zh_CN-fallback detector (T-P5-03 #5): in a ja_JP cell, after every other
// check of the cell has run (expect() runs last), plant shell text and check what is reported:
// zh_CN text under an id the ja_JP catalog lacks (trace path), zh_CN catalog text without an id
// (text path, in a text node and in an aria-label) are reported; kanji-only Japanese catalog
// text (設定, 実行中) and zh_CN text under translate="no" are not. The planted nodes are removed
// again before the cell is closed.
const LEAK_SELFTEST = {
  name: 'core_i18n_leak_selftest', page: 'logs', langs: ['ja_JP'], sizes: ['1280x800'], scenario: RUNNING,
  expect: async ({ page, lang }) => {
    const problems = [];
    const before = await page.evaluate(shellCjkInPage, { lang, frags: zhFragmentsAbsentFrom(lang) });
    if (before.length) problems.push(`the shell reports zh_CN text before planting: ${before.join(' | ')}`);
    await page.evaluate(() => {
      const side = document.querySelector('.side'), top = document.querySelector('.topbar');
      const add = (host, html) => { host.insertAdjacentHTML('beforeend', html); host.lastElementChild.classList.add('__st_leak'); };
      add(side, '<span data-i18n-id="__selftest.zh_only">测试退回</span>'); // 测试退回: id not in ja_JP
      add(top, '<span>总览</span>'); // 总览: zh_CN nav text (ja_JP 概要)
      add(top, '<button aria-label="前往配置">x</button>'); // 前往配置: zh_CN words
      add(side, '<span>設定</span>'); // 設定: ja_JP catalog text
      add(side, '<span>実行中</span>'); // 実行中: ja_JP catalog text
      add(top, '<span translate="no">香港节点 总览</span>'); // data: never read
    });
    const got = await page.evaluate(shellCjkInPage, { lang, frags: zhFragmentsAbsentFrom(lang) });
    await page.evaluate(() => { for (const e of document.querySelectorAll('.__st_leak')) e.remove(); });
    const want = [/^\.side: 测试退回 \(id __selftest\.zh_only not in ja_JP\)$/, /^\.topbar: 总览 \(zh_CN text "[^"]+"\)$/, /^\.topbar \[aria-label\]: 前往配置 \(zh_CN text "[^"]+"\)$/];
    for (const re of want) if (!got.some(g => re.test(g))) problems.push(`detector missed ${re.source}; reported: ${got.join(' | ') || '(nothing)'}`);
    for (const g of got) if (!want.some(re => re.test(g))) problems.push(`detector reported correct or data text: ${g}`);
    return { problems, metrics: { planted: 6, reported: got.length } };
  },
};

// the page cases at both sizes; the palette, notice and save-bar cases at 1280x800 (ticket)
// (the shell-only cells that run in the pseudo language start on the empty page: see BLANK;
// palette_full has no pseudo cell, its recorded zh_CN tree has the overview page's palette)
const blank = c => !/^shell_/.test(c.name) && (!c.langs || c.langs.includes('pseudo'));
export default CASES.map(c => ({ scenario: RUNNING, sizes: /^shell_/.test(c.name) ? ['1060x700', '1280x800'] : ['1280x800'], ...c,
  actions: blank(c) ? [...BLANK(c.page), ...(c.actions || [])] : c.actions,
  overflow_allow: [...SHELL_OVERFLOW, ...(c.overflow_allow || [])], expect: shellExpect(c.name) })).concat([LEAK_SELFTEST]);
