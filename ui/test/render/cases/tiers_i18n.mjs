// Render cases of T-P4-04c (message extraction · tiers) and the helpers its unit test
// (ui/test/unit/tiers_i18n.test.mjs) shares; the fixture walk re-pointed by T-P4-05b.
//
// Default export: fixture-mode cases for the render matrix — the four tiers, an empty tier, Bastion on
// and off, a subscription drilled into, the pool, the side panel, a subscription menu and the import
// sheet's three forms. Since T-P4-05b the tiers page shows the runtime's data, so the walk runs on the
// T-P4-05b scenarios (ui/test/fixtures/scenarios/tiers_running / tiers_stopped / tiers_bastion_off /
// tiers_empty: ASCII data); the T-P4-04c scenarios tiers_i18n_* carry no tier data and are no longer
// walked. Besides the matrix's own checks (leak in pseudo, overflow, injection, console) each cell
// asserts that the tiers page and #layer show no visible CJK text outside translate="no" unless the
// language is zh_CN, and in pseudo that every aria-label / title / placeholder / data-tip there
// outside translate="no" is catalog text (⟦ ⟧; critique E2).
//
// Named exports (used by the unit test):
//   tiersWordsInPage   in-page: the visible words of the tiers page section and of #layer, in DOM
//                      order (a run of text split at whitespace, so a data wrapper added around a
//                      node name does not change it), their visible attributes, and the CJK /
//                      attribute report of the leak check
//   STATES, runState   the walk to each state (sim mode or fixture mode), from a page loaded on
//                      #profiles and settled
//   FIXTURE_STATES, FIXTURE_SCENARIOS  the fixture-mode walk and the scenarios each state runs on
//   wordProblems       the leak verdict of a report for a language
// The T-P4-04c regression baseline (BEFORE: the prototype's words per state, and the T-P4-01 baseline
// text tree) is retired by T-P4-05b: the prototype's synthetic data and its 地区 / 走势 columns are
// removed by design (mapping_tiers sections 4.1 and 13), so those words cannot come back.
import { readFileSync } from 'node:fs';
import { LEAK_WHITELIST } from '../checks.mjs';

/** In page: words and attributes of the tiers page (section[data-page=profiles]) and of #layer. */
export function tiersWordsInPage() {
  const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/;
  const ATTRS = ['aria-label', 'title', 'placeholder', 'data-tip'];
  const vis = el => el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const dnt = el => !!el.closest('[translate="no"]');
  const out = { words: {}, attrs: {}, cjk: [], dntAttrs: [], catalogAttrs: [], data: [] };
  const regions = { pages: document.querySelector('section[data-page="profiles"]'), layer: document.getElementById('layer') };
  const path = (el, root) => {
    const parts = [];
    for (let e = el; e && e !== root; e = e.parentElement) {
      let p = e.localName;
      if (e.id) { parts.unshift(p + '#' + e.id); break; }
      const cls = Array.from(e.classList).filter(c => !/^is-|^swap|^sw$/.test(c)).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      parts.unshift(p);
    }
    return parts.join('>');
  };
  for (const [key, root] of Object.entries(regions)) {
    const words = out.words[key] = [], attrs = out.attrs[key] = [];
    if (!root) continue;
    // the visible text as it reads: text nodes concatenated in DOM order, whitespace collapsed,
    // then split into words (so text split into a data wrapper reads the same)
    let text = '';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = n.parentElement;
      if (!el || el.closest('script,style,svg') || !vis(el)) continue;
      text += n.nodeValue;
      const t = n.nodeValue.replace(/\s+/g, ' ').trim();
      if (t && !dnt(el) && CJK.test(t)) out.cjk.push([key, path(el, root), t]);
    }
    words.push(...text.replace(/\s+/g, ' ').trim().split(' ').filter(Boolean));
    // data shown on the page (under translate="no"): names, regions, addresses
    for (const el of root.querySelectorAll('[translate="no"]')) {
      for (const t of [el.textContent, ...Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.nodeValue)]) {
        const v = (t || '').replace(/\s+/g, ' ').trim();
        if (v && !out.data.includes(v)) out.data.push(v);
      }
    }
    for (const el of [root, ...root.querySelectorAll('*')]) {
      const shown = vis(el);
      for (const a of ATTRS) {
        const v = el.getAttribute(a);
        if (!v) continue;
        if (shown) attrs.push(`${a}=${v.replace(/\s+/g, ' ').trim()}`);
        (dnt(el) ? out.dntAttrs : out.catalogAttrs).push([key, path(el, root), a, v]);
      }
    }
  }
  return out;
}

/** Leak verdict of a tiersWordsInPage() report: CJK text outside translate="no" (any language but
    zh_CN), and in pseudo every attribute outside translate="no" must be catalog text (⟦…⟧). An
    attribute is one string: a node name in it (a message parameter, e.g. a row's aria-label) is
    data, so the data the page shows under translate="no" is taken out before the tests; what is
    left must be catalog text in pseudo, unless it is only words of the matrix's leak whitelist
    (protocol names, IPs, …: render/checks.mjs LEAK_WHITELIST), e.g. the paste hint's scheme list.
    The #layer region is the tiers page's own overlays (panel, sheets, menus, confirmations). */
export function wordProblems(report, lang) {
  const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/;
  const problems = [];
  const data = (report.data || []).slice().sort((a, b) => b.length - a.length);
  const words = v => data.reduce((s, d) => s.split(d).join(' '), v);
  const WL = LEAK_WHITELIST.map(x => new RegExp(x));
  const techOnly = v => v.split(/\s+/).map(t => t.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '')).filter(t => /\p{L}/u.test(t)).every(t => WL.some(re => re.test(t)));
  // (T-P5-11: ja_JP writes kanji, so the CJK test is for the other languages)
  const cjkLeak = lang !== 'zh_CN' && lang !== 'ja_JP';
  if (cjkLeak) for (const [k, p, t] of report.cjk) problems.push(`CJK text outside translate="no" in ${k} at ${p}: ${t}`);
  for (const [k, p, a, v] of report.catalogAttrs) {
    if (cjkLeak && CJK.test(words(v))) problems.push(`CJK ${a} in ${k} at ${p}: ${v}`);
    else if (lang === 'pseudo' && !v.includes('⟦') && !techOnly(words(v))) problems.push(`${a} in ${k} at ${p} is not catalog text: ${v}`);
  }
  return problems;
}

// ---- states ----------------------------------------------------------------------------------
// A step: {go: tier} (T.go('profiles', {tier})), {click: selector} (the first match; it must exist),
// {fill: [selector, text]}, {sim: 'stop' | 'start'}; every step is followed by `ms` (default 1500)
// of virtual time.
export const STATES = {
  vanguard: [],
  sentinel: [{ go: 'sentinel' }],
  bastion_off: [{ go: 'bastion' }],
  gateway: [{ go: 'gateway' }],
  sentinel_drill: [{ go: 'sentinel' }, { click: '.tiers-grp[data-key="g:nebula"] .tiers-gn' }],
  vanguard_panel: [{ click: '.tiers-row[data-key="v-hk03"] .tiers-name' }],
  vanguard_diag: [{ click: '.tiers-row[data-key="v-hk03"] .tiers-name' }, { click: '[data-p="diag"]', ms: 2500 }],
  gateway_panel: [{ go: 'gateway' }, { click: '.tiers-row[data-key="g-osa02"] .tiers-name' }],
  sentinel_panel: [{ go: 'sentinel' }, { click: '.tiers-row[data-key="s-fra04"] .tiers-name' }],
  import_paste: [{ click: '.tiers-imp' }],
  import_manual: [{ click: '.tiers-imp' }, { click: '.tiers-imp-tabs [data-v="manual"]' }],
  import_multi: [{ click: '.tiers-imp' }, { click: '.tiers-imp-tabs [data-v="manual"]' }, { click: '.tiers-imp-kind [data-v="multi"]' }],
  import_sub: [{ go: 'sentinel' }, { click: '.tiers-imp' }, { click: '.tiers-imp-tabs [data-v="sub"]' }],
  search_empty: [{ fill: ['.tiers-search input', 'zzzz'] }],
  stopped: [{ sim: 'stop', ms: 8000 }],
};
// fixture mode (no simulator, the T-P4-05b scenarios): FIXTURE_SCENARIOS names the scenarios each
// state runs on (tiers_running unless listed)
export const FIXTURE_STATES = {
  vanguard: [],
  sentinel: [{ go: 'sentinel' }],
  bastion_off: [{ go: 'bastion' }],
  bastion_on: [{ go: 'bastion' }],
  gateway: [{ go: 'gateway' }],
  gateway_empty: [{ go: 'gateway' }],
  sentinel_drill: [{ go: 'sentinel' }, { click: '.tiers-grp[data-key="g:sub-nebula"] .tiers-gn' }],
  pool_drill: [{ go: 'bastion' }, { click: '[data-pp="go"]' }],
  vanguard_panel: [{ click: '.tiers-row[data-key="v-hk03"] .tiers-name' }],
  gateway_panel: [{ go: 'gateway' }, { click: '.tiers-row[data-key="g-osa02"] .tiers-name' }],
  sub_panel: [{ go: 'sentinel' }, { click: '.tiers-grp[data-key="g:sub-aurora"] [data-a="more"]' }],
  import_paste: [{ click: '.tiers-imp' }],
  import_manual: [{ click: '.tiers-imp' }, { click: '.tiers-imp-tabs [data-v="manual"]' }],
  import_sub: [{ go: 'sentinel' }, { click: '.tiers-imp' }, { click: '.tiers-imp-tabs [data-v="sub"]' }],
};
export const FIXTURE_SCENARIOS = Object.fromEntries(Object.keys(FIXTURE_STATES).map(state => [state,
  state === 'bastion_off' ? ['tiers_bastion_off'] : state === 'gateway_empty' ? ['tiers_empty']
    : state === 'vanguard' || state === 'vanguard_panel' ? ['tiers_running', 'tiers_stopped'] : ['tiers_running']]));

/** Run the steps of a state in a page loaded on #profiles and settled (fixture clock). */
export async function runState(page, steps) {
  const adv = ms => page.evaluate(m => window.__fixtureClock.advance(m), ms);
  for (const s of steps) {
    if (s.go) await page.evaluate(t => T.go('profiles', { tier: t }), s.go);
    else if (s.click) await page.evaluate(sel => { const el = document.querySelector(sel); if (!el) throw new Error('no ' + sel); el.click(); }, s.click);
    else if (s.fill) await page.fill(s.fill[0], s.fill[1]);
    else if (s.sim) await page.evaluate(k => T.sim[k](), s.sim);
    await adv(s.ms != null ? s.ms : 1500);
    await page.waitForTimeout(60);
    await adv(0);
  }
  await page.waitForTimeout(80);
  await adv(0);
}

// ---- fixture-mode cases for the matrix -------------------------------------------------------
const RUN_OF = { tiers_running: 'running', tiers_stopped: 'stopped', tiers_bastion_off: 'running', tiers_empty: 'running' };
const words = async ({ page, lang, scenario }) => {
  const problems = [];
  const run = await page.evaluate(() => document.getElementById('app').getAttribute('data-run'));
  const want = RUN_OF[scenario.name];
  if (want && run !== want) problems.push(`#app data-run is ${run}, expected ${want}`);
  const shown = await page.evaluate(() => { const s = document.querySelector('section[data-page="profiles"]'); return !!s && !s.hidden; });
  if (!shown) problems.push('the tiers page is not shown');
  const report = await page.evaluate(tiersWordsInPage);
  problems.push(...wordProblems(report, lang));
  if (!report.words.pages.length) problems.push('the tiers page rendered no words');
  return { problems, metrics: { tiers_attrs: report.catalogAttrs.length, tiers_cjk_text: report.cjk.length } };
};
// the matrix's actions: the same walk as runState, in its own vocabulary
const toActions = steps => steps.flatMap(s => {
  const a = [];
  if (s.go) a.push({ eval: t => T.go('profiles', { tier: t }), arg: s.go });
  else if (s.click) a.push({ click: s.click });
  else if (s.fill) a.push({ fill: s.fill });
  a.push({ page_ms: s.ms != null ? s.ms : 1500 });
  return a;
});
// The demo control of the design target carries a CSS badge (css/design.css .demo-link::before
// content "演示"; not this ticket's file, design target only): not a catalog word of this page.
export const DEMO_BADGE = '^演示$';
// Pseudo-length allowances (run.mjs overflow_allow; pseudo cells only, every entry says why): the
// zh_CN-based pseudo factor outgrows fixed-width places (ruling 5th wave T-P4-10 #3; the short-label
// floor comes with T-P4-H). Leaks are not allowed: a leak fails the cell (critique E2).
// T-P5-11: the box rule's pseudo-only findings on the tiers page (also spread into tiers.mjs's own
// pseudo list): one-token pseudo words (the tilde padding cannot wrap) in places where every real
// language fits
export const PSEUDO_BOX = [
  { selector: 'span\\.tiers-st[^ ]* > span\\.swap-box > span\\.sw > span\\.st', why: '伪语言：节点状态词（状态列 100px），伪语言的状态词连同波浪号补长是一个不能换行的词；真实语言在列内换行（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: 'span\\.tiers-ght[^ ]* > span\\.st', why: '伪语言：订阅行健康列的状态词，同上，一个不能换行的词（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: 'div\\.tiers-res[^ ]* > div\\.callout[^ ]* > div\\.callout-body', why: '伪语言：诊断结果提示框（失败原因与处理建议）里的波浪号补长不能换行，宽过提示框（T-P5-11 第 1 轮修复：盒子越界规则的「文字越出自身格」量到它；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: 'label\\.toggle\\.tiers-tg > span\\.toggle-t > small', why: '伪语言：节点面板开关说明末尾的波浪号补长不能换行（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: 'span\\.num-step[^ ]* > button\\.num-b', why: '伪语言：数字框的加减按钮定宽，伪语言的按钮名是一个不能换行的词（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: 'div\\.tiers-pool-hd[^ ]* > span\\.pill', why: '伪语言：应急池标题旁的状态胶囊单行，伪语言的「低于下限」宽过 1060 下的标题行（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
];
export const PSEUDO_LENGTH = [
  // (the shell footer's two entries are gone: the footer fits longer words since T-P4-06; 8th-wave
  // ruling T-P4-H2 item 6)
  { selector: 'div\\.ovl-sub', why: '伪语言（T-P5-13 改写理由）：侧板副标题在面板定宽里换行（T-P5-11），伪语言补长的波浪号词不能换行而越出一点；9 种真实语言同一用例完整显示，由全语言矩阵核对' },
  // T-P4-05b (7th-wave ruling, T-P4-H item 1): the two-character column heads 延迟 / 地区 fit since the
  // latency (and region) column is 88 px wide, so they need no allowance. (T-P5-13: the one-line
  // places that ellipsized by design, the switcher cells, the diagnosis note, a section label's hint,
  // the pool line and the drill bar's note, are gone: interface words wrap or step out now.)
  ...PSEUDO_BOX,
];
/**
 * In page (T-P5-03; 10th-wave ruling item 3; T-P5-06 rulings 1-2): the carrying pill of the tier
 * switcher is readable and covers no text of the carrying cell. arg = the carrying tier. The floating
 * pill is shown and sits on its twin in the cell's second line (.tiers-pill-ph), inside the cell; no
 * visible text of that line (the Gateway's name, the slot words, the count, the unit word) and no
 * visible count / dots box intersects it (text rects clipped to their overflow-hidden boxes), and no
 * box of that line runs past the cell's content.
 * Readability (T-P5-06): the pill shows its words whole (not cut by any fraction of a pixel, so no
 * ellipsis and no clip) or is in its dot form (.is-dot, the cell .tight-4: no words shown); in both
 * forms the whole words reach assistive technology as the carrying tab's description (T-P5-11
 * ruling 4, form (b)): the pill itself is aria-hidden with no role (no image inside the tablist), the
 * carrying cell (the tab) has aria-describedby naming a hidden node whose text is the twin's words,
 * no other cell has it; the dot form carries the words as its hover tip (data-tip). In the Gateway cell the
 * name, when cut, keeps the floor the stylesheet sets (the slot's min-width, 3.5em, less the chip's
 * dot and gap) and ends in an ellipsis. Without a twin only the geometry is judged (T-P5-03 fix
 * round 1).
 * The steps (T-P5-06 fix round 1): the cell's .tight-n classes are taken in order (the words form
 * tight-1 … tight-k, the dot form tight-1 … tight-j and tight-4), and each was needed: with the last
 * one undone the twin's words are cut and the Gateway's name is already at its floor.
 * Returns the list of problems; with arg {tier, metrics: true} it returns { problems, metrics:
 * { pill_form: 'words' | 'dot', pill_steps: <.tight-n count>, gw_name_* … } } (the per-cell form
 * statistics of the T-P5-06 report). arg = the carrying tier, or that object.
 */
export function pillClearInPage(arg) {
  const tier = typeof arg === 'string' ? arg : arg.tier, withMetrics = typeof arg === 'object' && !!arg.metrics;
  const out = [];
  const R = e => e.getBoundingClientRect();
  const hit = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
  const fmt = r => `${r.left.toFixed(1)}-${r.right.toFixed(1)}`;
  const pill = document.querySelector('.tiers-pill'), cell = document.getElementById(`tiers-tab-${tier}`);
  if (!pill || !cell) { const p0 = [`no pill (${!!pill}) or no cell #tiers-tab-${tier} (${!!cell})`]; return withMetrics ? { problems: p0, metrics: {} } : p0; }
  const p = R(pill), c = R(cell);
  const vis = e => e.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  if (Number(getComputedStyle(pill).opacity) < 0.99) out.push(`the pill is not shown (opacity ${getComputedStyle(pill).opacity})`);
  // the width the text of `el` needs (its Range: the laid-out text, an ellipsis does not shorten it)
  // over the width its box gives it; > 0 means the text is cut
  const short = el => { const rg = document.createRange(); rg.selectNodeContents(el); return rg.getBoundingClientRect().width - R(el).width; };
  const dot = pill.classList.contains('is-dot');
  const TIGHT = ['tight-1', 'tight-2', 'tight-3', 'tight-4'];
  const tight = TIGHT.filter(k => cell.classList.contains(k)), steps = tight.length;
  if (dot !== cell.classList.contains('tight-4')) out.push(`the pill's form (${dot ? 'dot' : 'words'}) does not match its cell (.tight-4 ${cell.classList.contains('tight-4')})`);
  // the order things step out (T-P5-06 fix round 1): the unit word, the dots, the count, the pill's
  // words. The words form carries tight-1 … tight-k; the dot form tight-1 … tight-j and tight-4 (what
  // came back into the room the words gave, last out first in)
  const kept = tight.filter(k => k !== 'tight-4');
  if (kept.some((k, i) => k !== TIGHT[i])) out.push(`the cell's steps (${tight.join(' ') || 'none'}) are not taken in order (unit word, dots, count, words)`);
  const twin = cell.querySelector('.tiers-pill-ph');
  // the twin is how this layout keeps the pill's room; another layout may do without one, so a
  // missing twin skips the twin checks and only the geometry below is judged
  if (twin && !twin.hidden) {
    const t = R(twin);
    if (Math.abs(t.left - p.left) > 1 || Math.abs(t.top - p.top) > 1 || Math.abs(t.width - p.width) > 1) out.push(`the pill (${fmt(p)}) is not on its twin (${fmt(t)})`);
    const tw = twin.querySelector('.tiers-pill-ph-t'), words = (tw || twin).textContent.trim();
    if (!words) out.push('the twin carries no words');
    // (T-P5-11 ruling 4, form (b)) the pill stays out of the accessibility tree; the whole words are
    // the carrying tab's description, in every form
    if (pill.getAttribute('aria-hidden') !== 'true' || pill.hasAttribute('role') || pill.hasAttribute('aria-label')) out.push(`the pill is not aria-hidden decoration (aria-hidden ${pill.getAttribute('aria-hidden')}, role ${pill.getAttribute('role')}, aria-label ${pill.getAttribute('aria-label')})`);
    const descIds = (cell.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    const desc = descIds.map(id => document.getElementById(id)).filter(Boolean).map(d => d.textContent.trim()).join(' ');
    if (desc !== words) out.push(`the carrying tab's description "${desc}" (aria-describedby ${cell.getAttribute('aria-describedby')}) is not the pill's words "${words}"`);
    for (const d of descIds.map(id => document.getElementById(id)).filter(Boolean)) if (!d.hidden || d.closest('[role="tablist"]')) out.push(`the description node #${d.id} is not a hidden node outside the tablist`);
    for (const other of document.querySelectorAll('.tiers-sw [role="tab"]')) if (other !== cell && other.hasAttribute('aria-describedby')) out.push(`${other.id} (not carrying) has aria-describedby ${other.getAttribute('aria-describedby')}`);
    if (dot) {
      if (pill.getAttribute('data-tip') !== words) out.push(`the dot-form pill's hover tip "${pill.getAttribute('data-tip')}" is not its words "${words}"`);
      const pt = pill.querySelector('.tiers-pill-t');
      if (pt && vis(pt) && R(pt).width > 0.5) out.push(`the dot-form pill still shows words (${R(pt).width.toFixed(1)} px)`);
    } else {
      // the words form: the words show whole, in the pill and in its twin (any cut, however small,
      // shows an ellipsis or a clip)
      const pw = [...pill.querySelectorAll('.tiers-pill-t > .sw')].filter(e => !e._out).pop();
      if (!pw || !vis(pw)) out.push('the pill shows no words and is not in its dot form');
      else {
        const ps = short(pw);
        if (ps > 0.02) out.push(`the pill's words "${pw.textContent.trim()}" are cut (${ps.toFixed(2)} px short)`);
        if (pw.textContent.trim() !== words) out.push(`the pill shows "${pw.textContent.trim()}", its twin "${words}"`);
      }
      if (tw && short(tw) > 0.02) out.push(`the twin's words are cut (${short(tw).toFixed(2)} px short) and the pill is not in its dot form`);
      if (pill.hasAttribute('data-tip')) out.push('the words-form pill carries a hover tip');
    }
  }
  if (p.left < c.left - 0.5 || p.right > c.right + 0.5) out.push(`the pill (${fmt(p)}) runs past its cell (${fmt(c)})`);
  const l2 = cell.querySelector('.tiers-l2');
  const clipOf = e => { for (let a = e; a && a !== cell; a = a.parentElement) if (getComputedStyle(a).overflowX !== 'visible') return R(a); return null; };
  const w = document.createTreeWalker(l2, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const el = n.parentElement;
    if (!n.nodeValue.trim() || el.closest('.tiers-pill-ph') || !vis(el)) continue;
    const clip = clipOf(el), rg = document.createRange();
    rg.selectNodeContents(n);
    for (const r0 of rg.getClientRects()) {
      const r = clip ? { left: Math.max(r0.left, clip.left), right: Math.min(r0.right, clip.right), top: Math.max(r0.top, clip.top), bottom: Math.min(r0.bottom, clip.bottom) } : r0;
      if (r.right - r.left > 0.5 && hit(r, p)) { out.push(`the pill (${fmt(p)}) covers "${n.nodeValue.trim().slice(0, 40)}" (${fmt(r)})`); break; }
    }
  }
  for (const sel of ['.tiers-cnt', '.tiers-dots', '.st-n']) for (const e of l2.querySelectorAll(sel)) if (vis(e) && R(e).width > 0 && hit(R(e), p)) out.push(`the pill (${fmt(p)}) covers ${sel} (${fmt(R(e))})`);
  // no box of the line runs past the cell's content
  const inner = R(cell.querySelector('.tiers-cell-in'));
  for (const e of l2.children) if (vis(e) && R(e).width > 0 && R(e).right > inner.right + 0.5) out.push(`${e.className} (${fmt(R(e))}) runs past the cell's content (${fmt(inner)})`);
  const metrics = { pill_form: dot ? 'dot' : 'words', pill_steps: steps, pill_tight: tight.join(' ') };
  let gwNm = null, gwFloor = null;
  if (tier === 'gateway') {
    const nm = cell.querySelector('.st-n'), slot = cell.querySelector('.tiers-slot'), st = nm && nm.closest('.st');
    if (!nm || !vis(nm)) out.push('the carrying Gateway cell shows no exit name');
    else {
      // the floor the stylesheet gives the slot (3.5em) less the chip's dot and gap: the name's own floor
      const fs = parseFloat(getComputedStyle(slot).fontSize), slotMin = parseFloat(getComputedStyle(slot).minWidth);
      const bf = getComputedStyle(st, '::before');
      const chrome = parseFloat(bf.width) + (parseFloat(bf.marginLeft) || 0) + (parseFloat(bf.marginRight) || 0) + (parseFloat(getComputedStyle(st).columnGap) || 0);
      const floor = slotMin - chrome, shown = R(nm).width, cut = short(nm) > 0.02;
      if (!(Math.abs(slotMin - 3.5 * fs) <= 0.5)) out.push(`the Gateway slot's floor is ${slotMin} px, not 3.5em (${(3.5 * fs).toFixed(1)} px)`);
      if (cut && shown < floor - 0.5) out.push(`the cut Gateway name keeps only ${shown.toFixed(1)} px (floor ${floor.toFixed(1)} px)`);
      if (cut && getComputedStyle(nm).textOverflow !== 'ellipsis') out.push('the Gateway name is cut without an ellipsis');
      Object.assign(metrics, { gw_name_px: Math.round(shown * 10) / 10, gw_name_cut: cut, gw_name_floor_px: Math.round(floor * 10) / 10 });
      gwNm = nm; gwFloor = floor;
    }
  }
  // every step taken was needed (T-P5-06 fix round 1): undo the last one (the words form: its last
  // tight-k; the dot form: the words back, with the unit word, the dots and the count out) and the
  // twin's words no longer fit, while the Gateway's name is already at its floor (it gives way
  // first); the cell's classes are put back before anything else runs
  if (twin && !twin.hidden && steps) {
    const saved = cell.className, last = tight[tight.length - 1];
    if (dot) { cell.classList.remove('tight-4'); cell.classList.add('tight-1', 'tight-2', 'tight-3'); }
    else cell.classList.remove(last);
    const tw = twin.querySelector('.tiers-pill-ph-t'), cutBy = short(tw || twin), nmNow = gwNm ? R(gwNm).width : null;
    cell.className = saved;
    const undone = dot ? 'the dot form (tight-4; tight-1 … tight-3 kept)' : last;
    if (!(cutBy > 0.02)) out.push(`a step was not needed: without ${undone} the pill's words fit (${cutBy.toFixed(2)} px short)`);
    if (nmNow != null && nmNow > gwFloor + 0.5) out.push(`a step was taken while the Gateway name had room to give: without ${undone} the name keeps ${nmNow.toFixed(1)} px (floor ${gwFloor.toFixed(1)} px)`);
  }
  return withMetrics ? { problems: out, metrics } : out;
}
const PILL_LANGS = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];
// the carrier is Vanguard (tiers_running) or the chosen Gateway (tiers_gateway_carrier) with the
// carrying pill, or Sentinel after an observed fallback with the warning pill (shell_tier_fallback,
// fix round 1: another pill text and colour); every language at both sizes (the words check above
// is zh_CN / pseudo only and is not repeated here). T-P5-06 fix round 1: the chosen Gateway with a
// long exit name carries too (tiers_gateway_carrier with its exit osa02-exit renamed everywhere it
// is named: an ASCII name of 64 characters), so the name giving way first is judged in every
// language, not only in malicious_tiers (zh_CN / pseudo)
const LONG_EXIT = 'osa02-exit-premium-iplc-tokyo-osaka-relay-backup-line-07-reserve';
const longGateway = () => {
  const sc = JSON.parse(readFileSync(new URL('../../fixtures/scenarios/tiers_gateway_carrier.json', import.meta.url), 'utf8').split('osa02-exit').join(LONG_EXIT));
  return { ...sc, name: 'tiers_gateway_carrier_long_exit', desc: `tiers_gateway_carrier with the exit osa02-exit named ${LONG_EXIT} (T-P5-06 fix round 1)` };
};
const PILL_CASES = [['tiers_running', 'vanguard', 'vanguard'], ['tiers_gateway_carrier', 'gateway', 'gateway'], [longGateway(), 'gateway', 'gateway_long'], ['shell_tier_fallback', 'sentinel', 'fallback']].flatMap(([scenario, tier, tag]) => {
  const base = { name: `tiers_i18n_pill_${tag}`, scenario, page: 'tiers', actions: [{ page_ms: 900 }],
    expect: async ({ page }) => page.evaluate(pillClearInPage, { tier, metrics: true }) };
  return [
    { ...base, langs: PILL_LANGS },
    { ...base, name: `${base.name}_pseudo`, langs: ['pseudo'], leak_allow: [DEMO_BADGE], overflow_allow: PSEUDO_LENGTH },
  ];
});
export default Object.entries(FIXTURE_STATES).flatMap(([state, steps]) => FIXTURE_SCENARIOS[state].flatMap(scenario => {
  const name = `tiers_i18n_${scenario.replace(/^tiers_/, '')}_${state}`;
  const base = { scenario, page: 'profiles', actions: toActions(steps), expect: words };
  return [
    // T-P5-11 (ruling 3): the real-language cell runs in all nine languages, no allowance
    { ...base, name, langs: PILL_LANGS },
    { ...base, name: `${name}_pseudo`, langs: ['pseudo'], leak_allow: [DEMO_BADGE], overflow_allow: PSEUDO_LENGTH },
  ];
})).concat(PILL_CASES);
