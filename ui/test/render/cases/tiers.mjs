// Render cases of T-P4-05b (the tiers page on real data, mapping_tiers sections 1-12) for the
// render matrix: ui/test/fixtures/scenarios/tiers_*.json (fixture mode, ASCII data) on page `tiers`
// (alias of profiles), zh_CN and pseudo, 1060x700 and 1280x800.
//
// Every cell: the matrix's own checks (leak in pseudo, overflow, injection, console, unmatched
// fixture invokes) plus the case's expectations below, and in every language the words check of
// tiers_i18n.mjs (no CJK outside translate="no" unless zh_CN; in pseudo every attribute outside
// translate="no" is catalog text). The boundary states of mapping_tiers section 12 each have a case,
// named after its G item where it has one (G15-G19, G34-G40; G16, G38, G40 as cases of their own).
// With a build of `--h-features all` (run.mjs --build-flags "--h-features all") the B-item controls
// appear: the cases check either face by T.rt.feature (B-05 toolbar test, B-12 pool diagnosis).
//
// Confirmation dialogs (T.confirm) name a node in core-built plain text (no translate="no"; a core
// change handed on in the T-P4-04c report): the cases answer them inside their actions, so no
// dialog is on screen when a cell is captured.
import { readFileSync } from 'node:fs';
import { PSEUDO_BOX, tiersWordsInPage, wordProblems } from './tiers_i18n.mjs';

const SC = name => JSON.parse(readFileSync(new URL(`../../fixtures/scenarios/${name}.json`, import.meta.url), 'utf8'));
const SECRET = 'FIXTURE-SECRET-7f3a';

// ---- helpers ------------------------------------------------------------------------------------
const go = tier => [{ go: 'tiers', params: { tier } }, { page_ms: 900 }];
const click = sel => [{ click: sel }, { page_ms: 700 }];
const yes = [{ click: '.dlg [data-a="yes"]' }, { page_ms: 900 }];
const deliver = f => [{ deliver: f }, { page_ms: 1200 }];
const row = id => `.tiers-row[data-key="${id}"] .tiers-name`;
const grp = id => `.tiers-grp[data-key="g:${id}"] .tiers-gn`;
const panelBtn = id => ({ eval: label => { const b = [...document.querySelectorAll('.tiers-panel .ovl-foot button')].find(x => x.textContent.trim() === T.t(label)); if (!b) throw new Error('no panel button ' + label); b.click(); }, arg: id });
/** the words check of T-P4-04c, then the case's own checks (page.evaluate of `fn`, returning a list of problems) */
const check = (fn, arg) => async ({ page, lang }) => {
  const problems = [];
  const shown = await page.evaluate(() => { const s = document.querySelector('section[data-page="profiles"]'); return !!s && !s.hidden; });
  if (!shown) problems.push('the tiers page is not shown');
  const report = await page.evaluate(tiersWordsInPage);
  problems.push(...wordProblems(report, lang));
  // fn runs in the page and returns [ok, message] pairs; a false ok is a problem
  if (fn) for (const [ok, msg] of await page.evaluate(fn, arg === undefined ? null : arg)) if (!ok) problems.push(msg);
  return { problems, metrics: { tiers_attrs: report.catalogAttrs.length } };
};

// Pseudo-length allowances (pseudo cells only; every entry says why). The zh_CN-based pseudo factor
// outgrows the fixed-width places the design gives one line (ruling 5th wave T-P4-10 #3).
const PSEUDO_LENGTH = [
  { selector: 'div\\.ovl-sub', why: '伪语言（T-P5-13 改写理由）：侧板副标题在面板定宽里换行（T-P5-11），伪语言补长的波浪号词不能换行而越出一点；9 种真实语言同一用例完整显示，由全语言矩阵核对' },
];

// the manual form of the import sheet: socks5 (import.specs), its three required fields
const manualSocks5 = [...click('.tiers-imp-tabs [data-v="manual"]'), { advance_ms: 20 }, { page_ms: 300 },
  { click: '#tiers-imp-manual [data-f="protocol"] .select' }, { page_ms: 300 },
  { eval: id => { const b = [...document.querySelectorAll('.menu .menu-i')].find(x => x.querySelector('.menu-l').textContent.trim() === T.t(id)); if (!b) throw new Error('no menu item ' + id); b.click(); }, arg: 'catalog.socks5' }, { page_ms: 400 },
  { fill: ['#tiers-imp-manual [data-f="name"] input', 'manual-one'] }, { fill: ['#tiers-imp-manual [data-f="server"] input', 'm1.example.net'] },
  { fill: ['#tiers-imp-manual [data-f="port"] input', '1080'] }, { page_ms: 200 }];
// Sentinel, drill into nebula, 管理, reveal its address (the eval fails the case unless it is shown)
const manageReveal = [...go('sentinel'), ...click(grp('sub-nebula')), ...click('.tiers-bar [data-b="mng"]'), { page_ms: 300 },
  { click: '.tiers-url .icon-btn' }, { page_ms: 500 },
  { eval: s => { const v = document.querySelector('.tiers-url .tiers-host'); if (!v || !v.textContent.includes(s)) throw new Error('the address was not revealed: ' + (v && v.textContent)); }, arg: 'FIXTURE-SECRET-7f3a' }];
// import.apply failed after it saved (runs in the page: self-contained)
const UNAPPLIED = ({ rows, reason }) => {
  const title = T.t('tiers.outcome', { kind: 'unapplied', what: 'saved' });
  const banner = [...document.querySelectorAll('.banner')].find(b => b.textContent.includes(title));
  const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'import.apply');
  return [
    [inv.length === 1, `one import.apply (${inv.length})`],
    [!document.querySelector('.sheet.tiers-imp'), 'saved: the import sheet closes'],
    [!!banner, 'saved=true, applied=false: "saved, not applied" is said'],
    [banner && banner.textContent.includes(T.t(`errors.import.${reason}`)), `with why (${reason})`],
    ...rows.map(id => [!!document.querySelector(`.tiers-row[data-key="${id}"].is-new`), `the new row ${id} (by difference) glows`]),
  ];
};

const base = (name, scenario, actions, expect, o = {}) => ({ name, scenario, page: 'tiers', actions, expect: check(expect, o.arg), ...o.extra });
// tiers_diagnose whose first nodes.cancelDiagnose is refused (rate_limited); the next one is answered
// as in the fixture (T-P4-H2 item 4)
function cancelFailsOnce() {
  const sc = SC('tiers_diagnose');
  sc.name = 'tiers_diagnose_cancel_failed';
  const at = sc.steps.findIndex(x => x.on_invoke && x.on_invoke.cmd === 'nodes.cancelDiagnose');
  sc.steps.splice(at, 0, { on_invoke: { cmd: 'nodes.cancelDiagnose' }, reject: 'rate_limited' });
  return sc;
}

// T-P5-10 (ruling 3): the nine languages, for the two busy-fail cases (here and malicious.mjs)
export const LANGS9 = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];
/* Runs in the page (an eval action before the click): records every label face the button shows
   while it is in its fail state (window.__busyFaces: its words; __busyGlyph: whether those words
   are only the .sr-only name of a glyph-alone face), since a page may relabel the button right
   after the fail face is in (the import sheet repaints its footer with the error in its body); and
   how far the button stuck out of its parent's content box before the click (window.__busyPre). */
export function busyFaceWatch(sel) {
  const b = document.querySelector(sel);
  if (!b) throw new Error('no button ' + sel);
  // px the element sticks out of its parent's content box (left or right)
  const busyOut = el => {
    const p = el.parentElement, r = el.getBoundingClientRect(), pr = p.getBoundingClientRect(), cs = getComputedStyle(p);
    const l = pr.left + p.clientLeft + (parseFloat(cs.paddingLeft) || 0), rr = pr.left + p.clientLeft + p.clientWidth - (parseFloat(cs.paddingRight) || 0);
    return Math.max(0, l - r.left, r.right - rr);
  };
  window.__busyOut = busyOut;
  window.__busyFaces = []; window.__busyGlyph = [];
  window.__busyPre = busyOut(b);
  new MutationObserver(recs => {
    for (const r of recs) for (const n of r.addedNodes) {
      if (n.nodeType !== 1 || !n.matches('.sw') || !b.classList.contains('is-fail')) continue;
      window.__busyFaces.push(n.textContent.trim());
      window.__busyGlyph.push(!!n.querySelector('.sr-only') && !Array.from(n.childNodes).some(x => x.nodeType === 3 && x.textContent.trim()));
    }
  }).observe(b, { childList: true, subtree: true });
}
/* Runs in the page: what the fail face was (whole words or the short word) and the widths, for
   the cell's metrics */
export function busyFailMetricsInPage({ btn: bsel, box: xsel, want: id }) {
  const b = document.querySelector(bsel), box = b && b.closest(xsel);
  if (!b || !box) return {};
  const seen = window.__busyFaces || [], face = seen[0] || '';
  return { busy_fail_face: face.includes(T.t(id)) ? 'whole' : face === T.t('core.busy.failed', { code: 'other' }) ? ((window.__busyGlyph || [])[0] ? 'glyph' : 'short') : 'other',
    busy_btn_px: Math.round(b.getBoundingClientRect().width), busy_box_px: Math.round(box.getBoundingClientRect().width) };
}
/* Runs in the page (after busyFaceWatch): a T.ui.busy button that failed with `want` (a message id)
   stays inside `box` (its closest ancestor matching it: the sheet's footer, the row's fixed action
   cell .li-act), or, when its idle label already stuck out of its parent before the click (a long
   label in a narrow cell, the page's own layout), sticks out no further than it did then; every
   `keep` control beside it (the sheet's 取消, the row's 更多) stays inside `box`; its fail face (the
   first face busyFaceWatch saw in the fail state, else the one shown) is the whole words, or the
   short core.busy.failed word (shown, or the .sr-only name of the glyph alone) with the whole words
   as its hover tip and accessible description (T-P5-10 ruling 1, fix round 1). [ok, message] pairs. */
export function busyFailFitInPage({ btn: bsel, box: xsel, keep, want: id }) {
  const want = T.t(id), short = T.t('core.busy.failed', { code: 'other' });
  const b = document.querySelector(bsel), box = b && b.closest(xsel);
  if (!b || !box) return [[false, `no button ${bsel} inside ${xsel}`]];
  const x = box.getBoundingClientRect(), px = v => Math.round(v * 10) / 10;
  const inside = el => { const r = el.getBoundingClientRect(); return !r.width || (r.left >= x.left - 0.5 && r.right <= x.right + 0.5); };
  const at = el => { const r = el.getBoundingClientRect(); return `${px(r.left)}-${px(r.right)} in ${px(x.left)}-${px(x.right)}`; };
  const pre = window.__busyPre, now = window.__busyOut ? window.__busyOut(b) : NaN;
  const out = [[b.classList.contains('is-fail'), 'the button is in its fail state'],
    [inside(b) || (pre > 0 && now <= pre + 0.5), `the button stays inside ${xsel}, or no further out of its parent than before the click (${at(b)}; out ${px(now)} px, before ${px(pre)} px)`]];
  for (const k of document.querySelectorAll(keep)) if (k !== b) out.push([inside(k), `${keep} "${k.textContent.trim()}" stays inside ${xsel} (${at(k)})`]);
  const shown = [...b.querySelectorAll('.lab .sw')].filter(s => !s.closest('[aria-hidden="true"]')).pop();
  const seen = window.__busyFaces || [];
  const text = seen.length ? seen[0] : shown ? shown.textContent.trim() : '';
  const tip = b.getAttribute('data-tip') || '';
  const desc = (b.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean).map(i => (document.getElementById(i) || {}).textContent || '').join(' ');
  const whole = text.includes(want);
  out.push([whole || (text === short && tip === want && desc.includes(want)),
    `the fail face is the whole words, or the short word with them as tip and description: face "${text}", tip "${tip}", description "${desc}"`]);
  out.push([!/…|\.\.\./.test(text), `no ellipsis on the fail face: "${text}"`]);
  if (whole) out.push([!b.querySelector('.busy-why') && tip !== want, 'the whole words on the face: no tip or description of them besides']);
  return out;
}
/* T-P5-15: runs in the page (an eval action, self-contained) after a view of the tiers page opened;
   records the search box's placeholder in window.__ph: the shown words, the view's own words, the
   short name, the words' width (a span in the input's computed font, fractional px), the input's
   content box, where the words end, where the Ctrl F keycap starts (and whether it is shown), the
   input's hover tip (and the box's, which must stay empty), the input's description (aria-describedby),
   the box's width, and (stepped down) the tip a focus on the input shows */
export function placeholderInPage(view) {
  const rec = { view };
  (window.__ph = window.__ph || []).push(rec);
  const box = document.querySelector('section[data-page="profiles"] .tiers-search'), input = box && box.querySelector('input');
  const drill = document.querySelector('.tiers-bar');
  rec.ok_view = !!input && ((view === 'sub' || view === 'pool') ? !!drill : !drill && !!document.querySelector(`#tiers-tab-${view}[aria-selected="true"]`));
  if (!input) { rec.why = 'no search box'; return; }
  const cs = getComputedStyle(input), m = document.createElement('span');
  for (const p of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontStretch', 'fontVariant', 'fontFeatureSettings', 'fontKerning', 'letterSpacing', 'wordSpacing', 'textTransform']) m.style[p] = cs[p];
  Object.assign(m.style, { position: 'fixed', left: '0', top: '0', visibility: 'hidden', whiteSpace: 'pre' });
  const want = T.t(`tiers.search.placeholder.${view}`);
  document.body.append(m);
  const width = s => { m.textContent = s; return s ? m.getBoundingClientRect().width : 0; };
  const w = width(input.placeholder), wantW = width(want);
  m.remove();
  const r = input.getBoundingClientRect(), f = v => parseFloat(v) || 0;
  const left = r.left + f(cs.borderLeftWidth) + f(cs.paddingLeft), right = r.right - f(cs.borderRightWidth) - f(cs.paddingRight);
  const kbd = box.querySelector('.search-kbd'), kr = kbd && kbd.getBoundingClientRect();
  const r2 = x => Math.round(x * 100) / 100;
  Object.assign(rec, {
    ph: input.placeholder, want, short: T.t('tiers.toolbar.search_aria'),
    text_w: r2(w), want_w: r2(wantW), room: r2(right - left), end: r2(left + w),
    kbd_shown: !!kbd && kbd.checkVisibility({ opacityProperty: true, visibilityProperty: true }) && kr.width > 0, kbd_left: kr ? r2(kr.left) : null,
    tip: input.getAttribute('data-tip'), box_tip: box.getAttribute('data-tip'),
    desc: (input.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean).map(i => (document.getElementById(i) || {}).textContent || '').join(' '),
    box_w: r2(box.getBoundingClientRect().width),
  });
  // stepped down: a focus on the input (what Tab does) shows the whole words as the tip (fix round 1)
  if (rec.tip) {
    input.focus({ preventScroll: true });
    const on = document.querySelector('.tip.on');
    rec.focus_tip = on ? on.firstChild && on.firstChild.nodeType === 3 ? on.firstChild.textContent : on.textContent : null;
    input.blur();
  }
}

/* F-01 L2 (the import sheet's panes): runs in the page (an eval action, self-contained) while the
   import sheet is open; records in window.__imp which tab is selected and which panes are on screen.
   The sheet's tabs hide the panes they do not show (T.ui.tabs, by aria-controls); the bug was that
   the sheet is built before it is in the document, so on opening every pane showed at once (the
   paste and the subscription panes one under the other) until a tab was clicked */
export function importPanesInPage(label) {
  const sh = document.querySelector('.sheet.tiers-imp');
  const rec = { label, open: !!sh };
  (window.__imp = window.__imp || []).push(rec);
  if (!sh) return;
  const tabs = [...sh.querySelectorAll('.tiers-imp-tabs .tab')];
  rec.tabs = tabs.map(b => b.dataset.v);
  rec.selected = tabs.filter(b => b.getAttribute('aria-selected') === 'true').map(b => b.dataset.v);
  rec.shown = [...sh.querySelectorAll('.tiers-imp-p')].filter(p => p.checkVisibility()).map(p => p.id.replace('tiers-imp-', ''));
  rec.hidden_attr = [...sh.querySelectorAll('.tiers-imp-p')].filter(p => p.hidden).map(p => p.id.replace('tiers-imp-', ''));
}
const impPanes = label => ({ eval: importPanesInPage, arg: label });
const closeImport = [{ click: '.sheet.tiers-imp .ovl-x' }, { page_ms: 600 }];
/* the overview's deck card of a tier, clicked (the card may sit behind the front one: clicked in the page) */
const fromOverview = t => [{ go: 'overview' }, { page_ms: 900 },
  { eval: tier => { const c = document.querySelector(`.dcard[data-tier="${tier}"]`); if (!c) throw new Error('no deck card ' + tier); c.click(); }, arg: t }, { page_ms: 900 }];
/* the case's check, run in the page (self-contained; its arg: { label: the tab that must be the only
   one shown }): window.__imp, recorded by importPanesInPage */
function impPanesCheck(want) {
  const seen = window.__imp || [], out = [];
  out.push([seen.map(r => r.label).join(',') === Object.keys(want).join(','), `the sheets measured in order: ${Object.keys(want)}, got ${seen.map(r => r.label)}`]);
  for (const r of seen) {
    const at = `${r.label}: tabs ${r.tabs}, selected ${r.selected}, shown ${r.shown}`;
    out.push([r.open, `${r.label}: the import sheet is open`]);
    if (!r.open) continue;
    out.push([JSON.stringify(r.tabs) === JSON.stringify(r.label.startsWith('vanguard') ? ['paste', 'manual'] : ['paste', 'manual', 'sub']), `${at}: the tier's tabs`]);
    out.push([JSON.stringify(r.selected) === JSON.stringify([want[r.label]]), `${at}: the ${want[r.label]} tab is selected`]);
    out.push([JSON.stringify(r.shown) === JSON.stringify([want[r.label]]), `${at}: only the selected tab's pane is shown`]);
    out.push([r.hidden_attr.length === r.tabs.length - 1 && !r.hidden_attr.includes(want[r.label]), `${at}: the other panes are hidden (${r.hidden_attr})`]);
  }
  return out;
}

/* the check after an import into Bastion, run in the page (self-contained). arg: { apply: the args the
   one import.apply carries (a subset), mine / msrc: the row keys under 你添加的节点 / 你添加的来源
   (sorted), fresh: the keys that glow (new by difference) } */
function bastionSections(want) {
  const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'import.apply');
  const sub = (w, g) => Object.keys(w).every(k => JSON.stringify(w[k]) === JSON.stringify(g && g[k]));
  const at = {}, keys = [];
  let sec = null;
  for (const e of document.querySelectorAll('.tiers-list > [data-key]')) {
    const k = e.dataset.key;
    if (k.startsWith('l:')) { sec = k.slice(2); continue; }
    (at[sec] = at[sec] || []).push(k); keys.push(k);
  }
  const sorted = a => JSON.stringify((a || []).slice().sort());
  const glow = [...document.querySelectorAll('.tiers-list > .is-new')].map(e => e.dataset.key);
  return [
    [inv.length === 1 && sub(want.apply, inv[0].args), `one import.apply with ${JSON.stringify(want.apply)} (${JSON.stringify(inv.map(i => i.args))})`],
    [!document.querySelector('.sheet.tiers-imp'), 'imported: the sheet closes'],
    [sorted(at.mine) === sorted(want.mine), `你添加的节点: ${sorted(at.mine)}, want ${sorted(want.mine)}`],
    [sorted(at.msrc) === sorted(want.msrc), `你添加的来源: ${sorted(at.msrc)}, want ${sorted(want.msrc)}`],
    [Object.keys(at).every(s => s === 'mine' || s === 'msrc'), `only the two sections (${Object.keys(at)})`],
    [new Set(keys).size === keys.length, `every row once (${keys})`],
    [want.fresh.every(k => glow.includes(k)), `the new rows glow: ${want.fresh} (glowing: ${glow})`],
    // F-01 round 1: a node's second line names its source (data), or nothing for a node you added alone
    ...Object.entries(want.src || {}).map(([k, name]) => {
      const e = document.querySelector(`.tiers-row[data-key="${k}"] .tiers-sub`), got = e ? e.textContent : null;
      return [got === name, `${k} names its source "${name}" (got ${JSON.stringify(got)})`];
    }),
  ];
}

/* the case's metrics: each view's placeholder form (whole / short / none) with its width and room */
const phMetrics = c => ({ ...c, expect: async ctx => {
  const r = await c.expect(ctx);
  const ph = await ctx.page.evaluate(() => window.__ph || []);
  const form = x => (x.ph === x.want ? 'whole' : x.ph === '' ? 'none' : x.ph === x.short ? 'short' : 'other');
  return { ...r, metrics: { ...r.metrics, search_box_px: ph.length ? ph[0].box_w : null, placeholders: ph.map(x => `${x.view} ${form(x)} ${x.text_w}/${x.room}${form(x) === 'whole' ? '' : ` (whole ${x.want_w})`}`) } };
} });

const CASES = [
  // ---- lists ---------------------------------------------------------------------------------------
  base('tiers_running_vanguard', 'tiers_running', [{ page_ms: 600 }], () => {
    const q = s => document.querySelector(s), r = id => q(`.tiers-row[data-key="${id}"]`);
    const head = q('.tiers-head');
    return [
      [r('v-hk03') && r('v-hk03').classList.contains('is-cur'), 'hk03-vps carries the traffic (route.published leaf): marked current'],
      [r('v-tyo01') && /st-slow|slow/.test(r('v-tyo01').querySelector('.tiers-st').innerHTML), 'tyo01-vps is slow (Node.slow, K-5)'],
      [r('v-sg02') && r('v-sg02').querySelector('.tiers-why') && r('v-sg02').querySelector('.tiers-why').textContent === T.t('tiers.reason.tls_failed'), 'sg02-vps says why it failed (tiers.reason)'],
      [r('v-old') && r('v-old').classList.contains('is-off'), 'old-vps (status disabled) is a draft'],
      [!!r('cfg-draft2'), 'draft-two (config.items, not in nodes.list) is a draft row'],
      [head && head.children.length === (T.rt.feature('B-03') ? 1 : 0) + (T.rt.feature('B-04') ? 1 : 0) + 5, 'the list head has name, latency, state, protocol, actions (region B-03, trend B-04 only behind their features)'],
      [!q('.tiers-row .tiers-reg') === !T.rt.feature('B-03'), 'no region cell without B-03'],
      [!!q('.tiers-test') === T.rt.feature('B-05'), 'the test-all button only with B-05'],
    ];
  }),
  base('tiers_running_sentinel', 'tiers_running', go('sentinel'), () => {
    const g = id => document.querySelector(`.tiers-grp[data-key="g:${id}"]`);
    const sub = id => (g(id) ? g(id).querySelector('.tiers-gsub').textContent : '');
    // the health column's words (F-01 L2: the row's second line no longer repeats them, nor the count)
    const health = id => (g(id) ? g(id).querySelector('.tiers-ght').textContent.trim() : '');
    return [
      [document.querySelectorAll('.tiers-grp').length === 5, 'five subscription rows'],
      [g('sub-orion') && g('sub-orion').classList.contains('is-off') && health('sub-orion') === T.t('tiers.sub.disabled_short') && sub('sub-orion') === '', `G17: orion is disabled, said once in its health column (line "${sub('sub-orion')}")`],
      [g('sub-vega') && g('sub-vega').classList.contains('is-busy'), 'vega is refreshing (an indeterminate bar)'],
      [sub('sub-aurora') === T.t('tiers.drill.rechecking', { done: 1, total: 2 }), 'G35: aurora is waiting for a recheck, 1/2 (K-6 recheck)'],
      [health('sub-lyra') === T.t('core.status.pending') && sub('sub-lyra') === '', `G35: lyra not checked yet (visibility unknown), said once in its health column ("${health('sub-lyra')}" / line "${sub('sub-lyra')}")`],
      [sub('sub-nebula') === '' && g('sub-nebula').querySelector('.tiers-gc').textContent === T.t('tiers.sub.count', { n: 3 }), 'nebula: its count beside its name only, no second line'],
      [!!document.querySelector('.tiers-row[data-key="s-fra04"]'), 'the subscription nodes are listed'],
      [g('sub-nebula') && /\S/.test(g('sub-nebula').querySelector('.tiers-gup').textContent), 'K-6: the tier refresh time is shown'],
    ];
  }),
  // G40: a subscription's name finds its nodes (Node.subscription_id present, C1 item 3)
  base('tiers_g40_search_subscription', 'tiers_running', [...go('sentinel'), { fill: ['.tiers-search input', 'nebula'] }, { page_ms: 600 }], () => {
    const keys = [...document.querySelectorAll('.tiers-row')].map(r => r.dataset.key).sort();
    return [[JSON.stringify(keys) === JSON.stringify(['s-fra04', 's-osa03', 's-sel01']), `G40: nebula finds its three nodes, got ${keys}`]];
  }),
  // T-P5-15 (决定_P5裁定 P5-e #5; T-P5-13 S2): the search box's placeholder in all six views (the four
  // tiers, a subscription drilled into, the pool), nine languages and pseudo, both sizes. The render
  // checks do not read placeholders, so the case measures them (placeholderInPage, after each view):
  // the shown placeholder lies wholly inside the input's content box and ends before the Ctrl F
  // keycap; it is the view's own words, or (when those do not fit) the box's short name or none, and
  // then the whole words are the input's hover tip and its description; the box keeps one width
  // in all six views; zh_CN shows every placeholder whole in the old width (260 / 240 px)
  phMetrics(base('tiers_search_placeholder', 'tiers_running', [
    ...go('vanguard'), { eval: placeholderInPage, arg: 'vanguard' },
    ...go('sentinel'), { eval: placeholderInPage, arg: 'sentinel' },
    ...click(grp('sub-nebula')), { eval: placeholderInPage, arg: 'sub' },
    ...go('bastion'), { eval: placeholderInPage, arg: 'bastion' },
    ...click('[data-pp="go"]'), { eval: placeholderInPage, arg: 'pool' },
    ...go('gateway'), { eval: placeholderInPage, arg: 'gateway' },
  ], () => {
    const seen = window.__ph || [], out = [];
    const views = seen.map(r => r.view).join(',');
    out.push([views === 'vanguard,sentinel,sub,bastion,pool,gateway', `six views measured in order, got ${views}`]);
    for (const r of seen) {
      const at = `${r.view}: ${JSON.stringify(r.ph)} ${r.text_w}px in ${r.room}px`;
      out.push([r.ok_view, `${r.view}: the view did not open (${r.why || ''})`]);
      out.push([r.ph === r.want || r.ph === r.short || r.ph === '', `${at}: the placeholder is the view's words, the short name or none`]);
      out.push([r.text_w <= r.room + 0.02, `${at}: the placeholder is cut by the input (${(r.text_w - r.room).toFixed(2)} px)`]);
      out.push([!r.kbd_shown || r.end <= r.kbd_left + 0.02, `${at}: the placeholder runs under the Ctrl F keycap (ends ${r.end}, keycap at ${r.kbd_left})`]);
      out.push([r.box_tip === null, `${at}: the tip sits on the input (a keyboard focus shows it), not on the box (${JSON.stringify(r.box_tip)})`]);
      if (r.ph === r.want) out.push([r.tip === null && r.desc === '', `${at}: whole words, so no tip or description (${JSON.stringify([r.tip, r.desc])})`]);
      else {
        out.push([r.tip === r.want && r.desc === r.want, `${at}: stepped down, the whole words must be the tip and the description (${JSON.stringify([r.tip, r.desc])})`]);
        out.push([r.focus_tip === r.want, `${at}: stepped down, a keyboard focus on the input must show the whole words as the tip (${JSON.stringify(r.focus_tip)})`]);
        out.push([r.want_w > r.room + 0.02, `${at}: stepped down although the whole words (${r.want_w}px) fit`]);
      }
    }
    const ws = seen.map(r => r.box_w);
    out.push([ws.length && Math.max(...ws) - Math.min(...ws) <= 0.5, `the box keeps one width in the six views (${ws.join(', ')})`]);
    if (T.i18n.lang === 'zh_CN') {
      out.push([seen.every(r => r.ph === r.want), 'zh_CN: every placeholder whole']);
      out.push([ws.every(w => Math.abs(w - (innerWidth <= 1180 ? 240 : 260)) <= 0.5), `zh_CN: the box keeps its old width (${ws.join(', ')})`]);
    }
    return out;
  })),
  base('tiers_running_sentinel_drill', 'tiers_running', [...go('sentinel'), ...click(grp('sub-nebula'))], () => [
    [!!document.querySelector('.tiers-bar'), 'K-4: the subscription is drilled into'],
    [document.querySelectorAll('.tiers-row').length === 3, 'its three member nodes'],
  ]),
  base('tiers_running_bastion', 'tiers_running', go('bastion'), () => [
    [!!document.querySelector('.tiers-pool'), 'Bastion on: the pool panel'],
    [!!document.querySelector('.tiers-pool-min input'), 'K-7 delivered (config.snapshot has bastion): the minimum is an input'],
    [!!document.querySelector('.tiers-pool-off'), 'K-7 delivered: 停用 Bastion is offered'],
    [!!document.querySelector('.tiers-row[data-key="b-u1"]') && !!document.querySelector('.tiers-grp[data-key="g:sub-mysrc"]'), 'your own node and source are listed'],
    // F-01 round 1: a node of your source names it (data, as Sentinel's subscription nodes); your own node names none
    [(() => { const s = k => { const e = document.querySelector(`.tiers-row[data-key="${k}"] .tiers-sub`); return e ? e.textContent : null; };
      return s('b-s1') === 'my-source' && s('b-u1') === '' && !!document.querySelector('.tiers-row[data-key="b-s1"] .tiers-sub [translate="no"]'); })(), 'src-node-1 names its source my-source (translate="no"); my-emergency names none'],
    [!document.querySelector('.tiers-row[data-key="b-p1"]'), 'the automatic pool nodes are behind the pool panel'],
  ]),
  // T-P4-H2: a chosen Gateway is the carrier itself (tiers_gateway_carrier); tiers_running chose none
  base('tiers_running_gateway', 'tiers_gateway_carrier', go('gateway'), () => [
    [!document.querySelector('.tiers-row.is-cur'), 'no selected_node_id: no row is marked current (no guess by name)'],
    [!!document.querySelector('[data-key="gw-named"]') && document.querySelector('[data-key="gw-named"] .tiers-offrow-n').textContent === 'osa02-exit', 'the current exit (gateway.current.effective) is named as text'],
    [!!document.querySelector('.tiers-row[data-key="g-osa02"] [data-a="use"]'), 'every candidate offers 设为当前'],
  ]),
  base('tiers_running_gateway_none', 'tiers_running', go('gateway'), () => [
    [!document.querySelector('[data-key="gw-named"]') && !document.querySelector('.tiers-row.is-cur'), 'no Gateway chosen: no exit is named, no row is current'],
    [!!document.querySelector('.tiers-row[data-key="g-osa02"] [data-a="use"]'), 'every candidate offers 设为当前'],
  ]),
  // T-P4-H2 item 4 (05a review 1 S2): carrier gateway, the fallback tier under it is not reported:
  // no fallback tier row is current; the Gateway tab names gateway.current.effective
  base('tiers_gateway_carrier', 'tiers_gateway_carrier', go('gateway'), () => [
    [!!document.querySelector('[data-key="gw-named"]') && document.querySelector('[data-key="gw-named"] .tiers-offrow-n').textContent === 'osa02-exit', 'the Gateway tab names the effective exit'],
  ]),
  base('tiers_gateway_carrier_vanguard', 'tiers_gateway_carrier', [{ page_ms: 600 }], () => [
    [!!document.querySelector('.tiers-row[data-key="v-hk03"]'), 'the Vanguard rows are listed'],
    [!document.querySelector('.tiers-row.is-cur'), 'carrier gateway: no Vanguard row is marked current (the route leaf is the Gateway node)'],
  ]),
  base('tiers_gateway_selected_node_id', (() => {
    const sc = SC('tiers_gateway_carrier');
    const st = sc.steps.find(s => s.deliver && s.deliver.topic === 'gateway.current');
    st.deliver.data.selected_node_id = 'g-osa02';
    return { ...sc, name: 'tiers_gateway_selected_node_id', desc: `${sc.desc} The case adds gateway.current.selected_node_id (T-P2-H8). It is added here, not in the fixture file: the frame invariant wants a 24-hex Node.id there, and the fixture node ids are readable names.` };
  })(), go('gateway'), () => [
    [document.querySelector('.tiers-row[data-key="g-osa02"]') && document.querySelector('.tiers-row[data-key="g-osa02"]').classList.contains('is-cur'), 'selected_node_id: the row of osa02-exit is marked current'],
    [!document.querySelector('[data-key="gw-named"]'), 'no text-only row'],
  ]),
  // ---- the side panel -----------------------------------------------------------------------------
  base('tiers_running_panel', 'tiers_running', click(row('v-hk03')), () => {
    const p = document.querySelector('.tiers-panel');
    return [
      [!!p, 'the side panel is open'],
      [p && /h•••\.net:443/.test(p.textContent), 'K-1: the masked endpoint from nodes.detail'],
      [p && !!p.querySelector('.tiers-chain'), 'K-3: the protocol order (two protocols)'],
      [p && !p.querySelector('.tiers-dopt[hidden] .tiers-dsel') && !!p.querySelector('.tiers-dsel'), 'K-9: the protocol choice of the diagnosis'],
      [p && !!p.querySelector('.tiers-res-ok.is-last'), 'the last diagnosis (nodes.detail.last_diagnostic)'],
      [p && !/data-sec/.test(p.outerHTML), 'no secret attribute (js:1469 removed)'],
    ];
  }),
  // F-04 follow-up (11-01): Bastion's figures are the available count alone (the target is named in
  // the panel, never a denominator); bastion.pool going stale while running (a gap: no fresh value)
  // keeps them, dimmed, and the cell and the Garrison pill say 数据未更新 (as the overview deck does)
  base('tiers_bastion_pool_stale', 'tiers_running', [...go('bastion'),
    { deliver: { t: 'evt', epoch: 'rt-fixture-e1', produced_at_ms: 1790000000000, sent_at_ms: 1790000000001, topic: 'bastion.pool', rev: 2, kind: 'gap', data: { from_seq: 1, to_seq: 1, dropped: 1 }, seq: 2 } },
    { advance_ms: 50 }, { page_ms: 700 }], () => {
    const cell = document.getElementById('tiers-tab-bastion'), cnt = cell && cell.querySelector('.tiers-cnt');
    const n = cell && cell.querySelector('.tiers-cnt-n'), pool = document.querySelector('.tiers-pool');
    const face = e => (e ? ([...e.querySelectorAll('.sw')].filter(x => !x._out).pop() || e).textContent.trim() : '');
    return [
      [!!(T.store.get('stale') || {})['bastion.pool'], 'bastion.pool really went stale (the marks are not vacuous)'],
      [face(cell && cell.querySelector('.tiers-slot')) === T.t('tiers.list.stale'), `the cell says 数据未更新: "${face(cell && cell.querySelector('.tiers-slot'))}"`],
      [!!cnt && cnt.classList.contains('is-old') && !/\//.test(n.textContent) && /2/.test(n.textContent), `the cell keeps its count, dimmed, without a denominator: "${n && n.textContent}"`],
      [!!pool && pool.classList.contains('is-old') && face(pool.querySelector('[data-pp="pt"]')) === T.t('tiers.list.stale'), 'the Garrison pill says 数据未更新'],
      [!!pool && pool.querySelector('[data-pp="of"]').textContent === T.t('tiers.pool.of', { target: 24 }), 'the target is named after the figure, not a denominator'],
    ];
  }),
  base('tiers_running_pool_panel', 'tiers_running', [...go('bastion'), ...click('[data-pp="go"]'), ...click(row('b-p1'))], () => {
    const p = document.querySelector('.tiers-panel');
    const b12 = T.rt.feature('B-12');
    return [
      [!!p, 'the pool node panel'],
      [p && !!p.querySelector('[data-p="diag"]') === b12, 'B-12: a pool node is diagnosed only behind its feature'],
      // F-01 L2: no read-only note any more; a pool node simply offers nothing to edit, switch or delete
      [p && !p.querySelector('.tiers-ro') && !p.querySelector('[data-p="tg"]') && ![...p.querySelectorAll('.ovl-foot button')].some(b => b.offsetParent), 'read-only without a note: no switch, no footer actions'],
    ];
  }),
  // K-11: a draft is switched on again by its ConfigItem id (T-P4-C1 item 8)
  base('tiers_running_draft_panel', 'tiers_running', click(row('v-old')), () => {
    const sw = document.querySelector('.tiers-panel [data-p="tg"]');
    return [[sw && !sw.disabled && sw.getAttribute('aria-checked') === 'false', 'K-11: the draft switch can be turned on (ConfigItem cfg-old)']];
  }),
  // ---- stopped, empty, Bastion off, degraded, paging --------------------------------------------------
  base('tiers_stopped_vanguard', 'tiers_stopped', [{ page_ms: 600 }], () => [
    [document.querySelector('.tiers').classList.contains('is-stale'), 'not running: the figures step back'],
    [!document.querySelector('.tiers-row.is-cur'), 'not running: no current marker'],
    [!!document.querySelector('.tiers-row .st-last, .tiers-row [class*="last"]'), 'the last known status shapes'],
  ]),
  base('tiers_stopped_gateway', 'tiers_stopped', go('gateway'), () => [
    [!!document.querySelector('[data-key="gw-named"]'), 'stopped: the saved choice (gateway.current.selected) is named'],
  ]),
  ...['vanguard', 'sentinel', 'gateway'].map(t => base(`tiers_g34_empty_${t}`, 'tiers_empty', t === 'vanguard' ? [{ page_ms: 600 }] : go(t), (tier) => {
    const e = document.querySelector('[data-key="empty-tier"]');
    return [
      [!!e, `G34: the empty ${tier} says what to do`],
      [e && !!e.querySelector('button'), 'G34: with the action that does it'],
    ];
  }, { arg: t })),
  base('tiers_g34_empty_bastion', 'tiers_empty', go('bastion'), () => [
    [!!document.querySelector('.tiers-bx'), 'Bastion off: the explanation card'],
    [!!document.querySelector('[data-key="off"]'), 'no own source yet: the empty line'],
    [!document.querySelector('.tiers-imp').hidden, 'import stays (G39)'],
  ]),
  base('tiers_g39_bastion_off', 'tiers_bastion_off', go('bastion'), () => [
    [!!document.querySelector('.tiers-bx'), 'Bastion off: the explanation card'],
    [!!document.querySelector('.tiers-row[data-key="b-u1"]') && !!document.querySelector('.tiers-grp[data-key="g:sub-mysrc"]'), 'G39: your own node and source are still listed'],
    [!document.querySelector('.tiers-bx [data-bx="on"]') && !document.querySelector('.tiers-bx input'), 'K-7 absent: no switch and no input'],
    [!!document.querySelector('.tiers-bx-ro') && !!document.querySelector('.tiers-pool-ro'), 'K-7 absent: the minimum read-only, the note'],
    // K-7 absent: no link, navigation control or wording that sends the user to the settings page
    [(() => { const bx = document.querySelector('.tiers-bx'); return !!bx && !bx.querySelector('a[href], [data-go], [data-nav], [data-page]') && !bx.textContent.includes(T.t('shell.nav.settings')); })(), 'no way to the settings page'],
    [!document.querySelector('.tiers-search').closest('[hidden]') && !document.querySelector('.tiers-imp').hidden, 'G39: search and import stay'],
  ]),
  // section 4.3: subscriptions.list not published; G16 / G40 absent face (no subscription_id)
  base('tiers_degraded_sentinel', 'tiers_degraded', go('sentinel'), () => {
    const g = document.querySelector('.tiers-grp[data-key="g:sub-nebula"]');
    return [
      [g && g.querySelector('.tiers-gsub').textContent === T.t('tiers.sub.degraded'), 'the subscription row from config.items says its state is not available'],
      [document.querySelectorAll('.tiers-row').length > 0, 'the node rows still render'],
    ];
  }),
  base('tiers_g40_absent_no_subscription_id', 'tiers_degraded', [...go('sentinel'), { fill: ['.tiers-search input', 'nebula'] }, { page_ms: 600 }], () => [
    [!document.querySelector('.tiers-row'), 'G40 absent: without Node.subscription_id a subscription name finds no node'],
    [!!document.querySelector('[data-key="empty"]'), 'the search empty state'],
  ]),
  base('tiers_degraded_refresh', 'tiers_degraded', [...go('sentinel'), { click: '.tiers-grp[data-key="g:sub-nebula"] [data-a="gupd"]' }, { advance_ms: 50 }, { page_ms: 2000 }], () => [
    [document.querySelector('.tiers-grp[data-key="g:sub-nebula"] .tiers-gsub').textContent.includes(T.t('tiers.refresh.result', { kind: 'applied' })), 'the refresh still works and shows its result once'],
  ]),
  base('tiers_paging_mismatch', 'tiers_paging', [{ page_ms: 600 }, ...deliver(SC('tiers_paging').steps.find(s => s.deliver && s.deliver.kind === 'patch').deliver && (() => {
    const f = JSON.parse(JSON.stringify(SC('tiers_paging').steps.find(s => s.deliver && s.deliver.kind === 'patch').deliver));
    f.rev = 9; f.base_rev = 7; f.data.changed[0] = { ...f.data.changed[0], latency_ms: 999 };
    return f;
  })())], () => {
    const r = document.querySelector('.tiers-row[data-key="v-hk03"] .tiers-lat');
    return [
      [document.querySelectorAll('.tiers-row').length === 6, `the three pages are one list: 4 live rows, the draft of nodes.list and the one of config.items (got ${document.querySelectorAll('.tiers-row').length})`],
      [r && !/999/.test(r.textContent) && /51/.test(r.textContent), 'the merged patch shows 51 ms; the mismatching patch did not overwrite (no last-wins)'],
      [!document.querySelector('.tiers-stale').hidden, 'the list says it is not up to date'],
    ];
  }),
  // ---- Gateway: missing, needs_route, off ------------------------------------------------------------
  base('tiers_g37_gateway_missing', 'tiers_gateway_missing', go('gateway'), () => [
    [!!document.querySelector('[data-key="gw-missing"].is-warn'), 'G37: the missing row'],
    [!!document.querySelector('[data-key="gw-missing"] [data-a="gwpick"]') && !!document.querySelector('[data-key="gw-missing"] [data-a="gwoff"]'), 'G37: pick a new exit, or turn Gateway off'],
    [document.querySelector('#tiers-tab-gateway .tiers-slot').textContent.includes(T.t('tiers.cell.slot.missing')), 'the switcher cell says so'],
  ]),
  // T-P5-10: nine languages and pseudo; the 设为当前 button's fail face stays inside the row
  // (the words check of check() is zh_CN / pseudo only, as in tiers_i18n.mjs: ja_JP's own kanji
  // are CJK, so in the other languages the page check and the case's own checks run). Fix round 1:
  // the box is the row's fixed action cell, not the row (the fail face never spreads over the
  // row's columns); the pointer still rests on the button, so its tip (the whole words) shows
  { name: 'tiers_gateway_needs_route', scenario: 'tiers_gateway_missing', page: 'tiers', langs: [...LANGS9, 'pseudo'],
    actions: [...go('gateway'), { eval: busyFaceWatch, arg: '.tiers-row[data-key="g-tyo01"] .tiers-use' }, { click: '.tiers-row[data-key="g-tyo01"] .tiers-use' }, { advance_ms: 50 }, { page_ms: 300 }],
    expect: async ({ page, lang }) => {
      const arg = { btn: '.tiers-row[data-key="g-tyo01"] .tiers-use', box: '.li-act', keep: '.tiers-row[data-key="g-tyo01"] .li-act > *', want: 'errors.needs_route' };
      const metrics = await page.evaluate(busyFailMetricsInPage, arg);
      if (lang === 'zh_CN' || lang === 'pseudo') { const r = await check(busyFailFitInPage, arg)({ page, lang }); return { ...r, metrics: { ...r.metrics, ...metrics } }; }
      const shown = await page.evaluate(() => { const s = document.querySelector('section[data-page="profiles"]'); return !!s && !s.hidden; });
      const problems = (await page.evaluate(busyFailFitInPage, arg)).filter(([ok]) => !ok).map(([, msg]) => msg);
      return { problems: shown ? problems : ['the tiers page is not shown', ...problems], metrics };
    } },
  base('tiers_gateway_off', 'tiers_gateway_missing', [...go('gateway'), { click: '[data-key="gw-missing"] [data-a="gwoff"]' }, { page_ms: 400 }, ...yes, { advance_ms: 50 }, { page_ms: 1500 }], () => {
    const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'gateway.select').map(i => i.args);
    return [
      [inv.some(a => a.proxy_name === ''), 'Gateway off is gateway.select{proxy_name: ""}'],
      [!!document.querySelector('[data-key="off"]'), 'then no exit is in use'],
    ];
  }),
  // ---- diagnosis (section 8.3, G15, G36) --------------------------------------------------------------
  base('tiers_diagnose_ok', 'tiers_diagnose', [...click(row('v-hk03')), { click: '.tiers-panel [data-p="diag"]' }, { advance_ms: 50 }, { page_ms: 400 }], () => {
    const res = document.querySelector('.tiers-panel .tiers-res');
    return [
      [res && /47/.test(res.textContent) && !/47.4/.test(res.textContent), 'the latency from nodes.detail after the job (rounded)'],
      [res && !res.querySelector('p'), 'F-04: no footnote under the result (status_applied false is not said)'],
    ];
  }),
  base('tiers_diagnose_failed', 'tiers_diagnose', [...click(row('v-sg02')), { click: '.tiers-panel [data-p="diag"]' }, { advance_ms: 50 }, { page_ms: 400 }], () => {
    const res = document.querySelector('.tiers-panel .tiers-res');
    return [
      [res && res.textContent.includes(T.t('tiers.reason.tls_failed')), 'G36: the reason word'],
      [res && !res.textContent.includes(T.t('tiers.reason.tls_failed.fix')), 'F-04: the 原因 row above gives the advice, the callout does not repeat it (nor cleanup_pending)'],
      [!!document.querySelector('.tiers-panel .tiers-kv-why') && document.querySelector('.tiers-panel .tiers-kv-why').textContent.includes(T.t('tiers.reason.tls_failed.fix')), 'the advice is still in the 原因 row'],
    ];
  }),
  base('tiers_diagnose_cancel', 'tiers_diagnose', [...click(row('v-tyo01')), { click: '.tiers-panel [data-p="diag"]' }, { advance_ms: 50 }, { page_ms: 200 },
    { click: '.tiers-panel [data-p="dstop"]' }, { advance_ms: 50 }, { page_ms: 400 }], () => {
    const res = document.querySelector('.tiers-panel .tiers-res');
    const c = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.cancelDiagnose');
    const d = T.rt.__fake.invocations.find(i => i.cmd === 'nodes.diagnose');
    return [
      [c.length === 1 && d && c[0].args.request_id === d.args.request_id, 'the cancel names the diagnosis request_id'],
      [res && res.textContent.includes(T.t('tiers.panel.diag.cancelled')), 'the diagnosis ends cancelled'],
    ];
  }),
  // T-P4-H2 item 4 (05b review cont. 1 S1): a stop whose cancel fails (rate_limited) can be tried
  // again; the second cancel goes out and ends the diagnosis
  base('tiers_diagnose_cancel_failed', cancelFailsOnce(), [...click(row('v-tyo01')), { click: '.tiers-panel [data-p="diag"]' }, { advance_ms: 50 }, { page_ms: 200 },
    { click: '.tiers-panel [data-p="dstop"]' }, { advance_ms: 50 }, { page_ms: 2800 },
    { click: '.tiers-panel [data-p="dstop"]' }, { advance_ms: 50 }, { page_ms: 400 }], () => {
    const res = document.querySelector('.tiers-panel .tiers-res');
    const c = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.cancelDiagnose');
    const d = T.rt.__fake.invocations.find(i => i.cmd === 'nodes.diagnose' && i.args.node_id === 'v-tyo01');
    return [
      [c.length === 2 && d && c.every(x => x.args.request_id === d.args.request_id), `the failed cancel did not block a second one (${c.length} sent)`],
      [res && res.textContent.includes(T.t('tiers.panel.diag.cancelled')), 'the diagnosis ends cancelled'],
    ];
  }, { extra: { langs: LANGS9 } }),
  // criticism B5: the panel closes while its diagnosis runs -> the owner revoke sends nodes.cancelDiagnose
  base('tiers_diagnose_panel_closed', 'tiers_diagnose', [...click(row('v-new01')), { click: '.tiers-panel [data-p="diag"]' }, { advance_ms: 50 }, { page_ms: 200 },
    { click: '.tiers-panel .ovl-x' }, { advance_ms: 50 }, { page_ms: 600 }], () => {
    const c = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.cancelDiagnose');
    const d = T.rt.__fake.invocations.find(i => i.cmd === 'nodes.diagnose' && i.args.node_id === 'v-new01');
    return [
      [!document.querySelector('.tiers-panel'), 'the panel is closed'],
      [c.length === 1 && d && c[0].args.request_id === d.args.request_id, 'closing the panel sent nodes.cancelDiagnose{request_id} of the running diagnosis'],
    ];
  }),
  // review A S1: a row's 诊断 while another node's edit has unsaved changes asks first; "no" diagnoses
  // nothing (not the old panel's node), "yes" diagnoses the row's node once
  base('tiers_diagnose_row_discard', 'tiers_diagnose', [...click(row('v-hk03')), panelBtn('tiers.panel.edit'), { page_ms: 500 },
    { fill: ['.tiers-panel [data-f="name"] input', 'hk03-renamed'] }, { page_ms: 200 },
    { eval: () => { document.querySelector('.tiers-row[data-key="v-tyo01"] [data-a="diag"]').click(); } }, { page_ms: 400 },
    { click: '.dlg [data-a="no"]' }, { page_ms: 600 },
    { eval: () => { window.__diagAfterNo = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.diagnose').length; document.querySelector('.tiers-row[data-key="v-tyo01"] [data-a="diag"]').click(); } }, { page_ms: 400 },
    ...yes, { advance_ms: 50 }, { page_ms: 300 }], () => {
    const d = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.diagnose');
    return [
      [window.__diagAfterNo === 0, `"no": nothing is diagnosed (${window.__diagAfterNo})`],
      [d.length === 1 && d[0].args.node_id === 'v-tyo01', `"yes": the row's node is diagnosed once (${d.map(x => x.args.node_id)})`],
    ];
  }, { extra: { langs: LANGS9 } }),
  // B-12 / G36: a pool node. Off (default build): no diagnosis is offered and none is sent. On
  // (--h-features all): the sidecar refuses it after resolution, reason tier3_auto_pool_self_check
  base('tiers_diagnose_pool_self_check', 'tiers_diagnose', [...go('bastion'), ...click('[data-pp="go"]'), ...click(row('b-p1')),
    { eval: () => { const b = document.querySelector('.tiers-panel [data-p="diag"]'); if (b) b.click(); } }, { advance_ms: 50 }, { page_ms: 400 }], () => {
    const b12 = T.rt.feature('B-12');
    const res = document.querySelector('.tiers-panel .tiers-res');
    const sent = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.diagnose');
    return b12 ? [
      [sent.length === 1 && sent[0].args.node_id === 'b-p1', 'B-12: the pool node diagnosis is sent'],
      [res && res.textContent.includes(T.t('tiers.reason.tier3_auto_pool_self_check')), 'G36: the refusal reason tier3_auto_pool_self_check is shown'],
    ] : [
      [!document.querySelector('.tiers-panel [data-p="diag"]'), 'B-12 off: no diagnosis button on a pool node'],
      [sent.length === 0, 'B-12 off: no nodes.diagnose is sent for a pool node'],
    ];
  }),
  // ---- writes (sections 8.4-8.7, G16, G18) -------------------------------------------------------------
  base('tiers_g16_edit_rolled_back', 'tiers_writes', [...click(row('v-hk03')), panelBtn('tiers.panel.edit'), { page_ms: 500 },
    { fill: ['.tiers-panel [data-f="name"] input', 'hk03-renamed'] }, panelBtn('tiers.form.save'), { advance_ms: 50 }, { page_ms: 500 }], () => {
    const f = document.querySelector('.tiers-panel .tiers-edit');
    const secrets = [...document.querySelectorAll('.tiers-panel .tiers-edit input[type="password"]')].length;
    return [
      [!!f, 'G16: the edit stays open after a rollback'],
      [secrets >= 1, 'G16: the share link can be replaced (SecretEdit), empty keeps it'],
      [document.querySelector('.tiers-panel').textContent.includes(T.t('tiers.write.rolled_back')) || document.querySelector('.tiers-panel .ovl-foot').textContent.includes(T.t('tiers.write.rolled_back')), 'the rollback is said'],
    ];
  }, { extra: { langs: LANGS9 } }),
  base('tiers_edit_saved_not_applied', 'tiers_writes', [...click(row('v-hk03')), panelBtn('tiers.panel.edit'), { page_ms: 500 },
    { fill: ['.tiers-panel [data-f="name"] input', 'hk03-renamed'] }, panelBtn('tiers.form.save'), { advance_ms: 50 }, { page_ms: 3000 },
    panelBtn('tiers.form.save'), { advance_ms: 50 }, { page_ms: 2000 }], () => {
    const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.update');
    return [
      [inv.length === 2 && inv[1].args.updates.name === 'hk03-renamed', 'the second save sends the name again'],
      [document.querySelector('.tiers-panel').textContent.includes(T.t('tiers.outcome', { kind: 'unapplied', what: 'saved' })), 'saved=true/applied=false: saved, not applied'],
    ];
  }, { extra: { langs: LANGS9 } }),
  // G18: the carrying node switched off asks first (a warning, then allowed: no hard stop); a node
  // that neither carries nor is the last one goes without asking (tyo01-vps, in tiers_writes' fixture)
  base('tiers_g18_draft_warns', 'tiers_writes', [...click(row('v-hk03')), { click: '.tiers-panel [data-p="tg"]' }, { page_ms: 400 }], () => [
    [!!document.querySelector('.dlg') && !!document.querySelector('.dlg [data-a="yes"]'), 'G18: switching the carrying node off warns first and then allows it'],
  ], { extra: { langs: LANGS9 } }),
  base('tiers_draft_without_warning', 'tiers_writes', [...click(row('v-tyo01')), { click: '.tiers-panel [data-p="tg"]' }, { advance_ms: 50 }, { page_ms: 600 }], () => {
    const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.update');
    return [[!document.querySelector('.dlg') && inv.length === 1 && inv[0].args.updates.enabled === false, 'not carrying, not the last: switched to draft at once (nodes.update enabled false)']];
  }, { extra: { langs: LANGS9 } }),
  base('tiers_g18_delete_unapplied', 'tiers_writes', [...click(row('v-sg02')), panelBtn('tiers.panel.delete'), { page_ms: 400 }, ...yes, { advance_ms: 50 }, { page_ms: 1200 }], () => {
    const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.delete');
    return [
      [inv.length === 1 && inv[0].args.item_id === 'v-sg02', 'G18: a warning first, then the delete goes'],
      [!document.querySelector('.tiers-panel'), 'deleted (saved): the panel closes'],
    ];
  }),
  base('tiers_k11_enable_draft', 'tiers_writes', [...click(row('v-old')), { click: '.tiers-panel [data-p="tg"]' }, { advance_ms: 50 }, { page_ms: 600 }], () => {
    const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.update');
    return [[inv.length === 1 && inv[0].args.item_id === 'cfg-old' && inv[0].args.updates.enabled === true, 'K-11: nodes.update{item_id: ConfigItem id, enabled: true}']];
  }),
  // section 8.5: nodes.reveal (private): text in the panel's memory only, never an attribute or the store
  base('tiers_reveal_secret', 'tiers_writes', [...click(row('v-hk03')), { click: '.tiers-panel .tiers-fold' }, { page_ms: 300 },
    { click: '.tiers-panel .tiers-params .icon-btn' }, { page_ms: 500 }], (secret) => {
    const p = document.querySelector('.tiers-panel');
    const attrs = [...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)).filter(v => v.includes(secret));
    /* every store key and the keyed lists of the four tiers (T.store has no all(): review A S4 / B S1) */
    const keys = Object.keys(T.store.snapshot());
    const inStore = Object.entries(T.store.snapshot()).filter(([, v]) => { try { return JSON.stringify(v === undefined ? null : v).includes(secret); } catch (e) { return true; } }).map(([k]) => k);
    const inTopics = ['vanguard', 'sentinel', 'bastion', 'gateway'].flatMap(t => ['nodes.list', 'subscriptions.list'].map(n => [`${n}/${t}`, JSON.stringify(T.rt.topic(n, t).get() || null)])).filter(([, j]) => j.includes(secret)).map(([k]) => k);
    return [
      [p && p.querySelector('.tiers-secv') && p.querySelector('.tiers-secv').textContent.includes(secret), 'the revealed link is shown as text'],
      [attrs.length === 0, 'no attribute holds the secret'],
      [keys.length > 0 && inStore.length === 0, `no store key holds the secret (${keys.length} keys; holding it: ${inStore})`],
      [inTopics.length === 0, `no nodes.list / subscriptions.list topic holds the secret: ${inTopics}`],
    ];
  }, { arg: SECRET, extra: { langs: LANGS9 } }),
  // ---- subscriptions.refresh (G35) --------------------------------------------------------------------
  base('tiers_g35_refresh_results', 'tiers_refresh', [...go('sentinel'),
    ...['sub-nebula', 'sub-aurora', 'sub-lyra', 'sub-vega'].flatMap(id => [{ click: `.tiers-grp[data-key="g:${id}"] [data-a="gupd"]` }, { advance_ms: 50 }, { page_ms: 300 }]), { page_ms: 3000 }], () => {
    const sub = id => document.querySelector(`.tiers-grp[data-key="g:${id}"] .tiers-gsub`).textContent;
    const r = k => T.t('tiers.refresh.result', { kind: k });
    return [
      [sub('sub-nebula').includes(r('applied')), `applied: ${sub('sub-nebula')}`],
      [sub('sub-aurora').includes(r('kept_previous')), `kept the previous nodes: ${sub('sub-aurora')}`],
      [sub('sub-lyra').includes(r('failed')), `failed (no previous): ${sub('sub-lyra')}`],
      // vega is refreshing already (subscriptions.list): its line keeps saying so after the refused refresh
      [sub('sub-vega') === T.t('tiers.sub.job.updating'), `vega still refreshing: ${sub('sub-vega')}`],
    ];
  }),
  // G35 in_progress (review A S4 / B S2): the refused refresh (conflict) is said on its button
  base('tiers_g35_refresh_in_progress', 'tiers_refresh', [...go('sentinel'), { click: '.tiers-grp[data-key="g:sub-vega"] [data-a="gupd"]' }, { advance_ms: 50 }, { page_ms: 400 }], () => {
    const b = document.querySelector('.tiers-grp[data-key="g:sub-vega"] [data-a="gupd"]');
    const sent = T.rt.__fake.invocations.filter(i => i.cmd === 'subscriptions.refresh' && i.args.item_id === 'sub-vega');
    return [
      [sent.length === 1, 'the refresh of vega is sent'],
      // an icon button: T.ui.busy announces the result as its name (aria-label) beside the fail glyph
      [b && b.classList.contains('is-fail') && b.getAttribute('aria-label') === T.t('tiers.refresh.short', { kind: 'in_progress' }), `conflict: the button says it is being refreshed (${b && b.getAttribute('aria-label')})`],
    ];
  }),
  base('tiers_g35_refresh_unapplied', 'tiers_refresh', [...go('gateway'), { click: '.tiers-grp[data-key="g:sub-exits"] [data-a="gupd"]' }, { advance_ms: 50 }, { page_ms: 3000 }], () => [
    [document.querySelector('.tiers-grp[data-key="g:sub-exits"] .tiers-gsub').textContent.includes(T.t('tiers.refresh.result', { kind: 'stored_unapplied' })), 'stored, not applied'],
  ]),
  base('tiers_refresh_lifecycle_busy', 'tiers_refresh', [...go('bastion'), { click: '.tiers-grp[data-key="g:sub-mysrc"] [data-a="gupd"]' }, { advance_ms: 50 }, { page_ms: 3000 }], () => [
    [document.querySelector('.tiers-grp[data-key="g:sub-mysrc"] .tiers-gsub').textContent.includes(T.t('tiers.refresh.result', { kind: 'busy_stopping' })), 'lifecycle_busy: the service is stopping'],
  ]),
  // ---- import (section 10, G19, G38) ---------------------------------------------------------------------
  base('tiers_g38_import_groups', 'tiers_import', [...click('.tiers-imp'), { fill: ['.tiers-imp-ta textarea', 'hysteria2://a@hk.example.net:443#a\nvless://b@hk.example.net:8443#b\ntrojan://c@jp.example.net:443#c'] },
    { page_ms: 400 }, { advance_ms: 20 }, { page_ms: 200 }], () => [
    [!!document.querySelector('.tiers-imp-groups') && document.querySelectorAll('.tiers-imp-groups .li').length === 2, 'G38: two physical-node groups'],
    [!document.querySelector('.tiers-imp .ovl-foot .btn-primary').disabled, 'importable'],
  ]),
  base('tiers_import_replan_and_glow', 'tiers_import', [...click('.tiers-imp'), { fill: ['.tiers-imp-ta textarea', 'hysteria2://a@hk.example.net:443#a\nvless://b@hk.example.net:8443#b\ntrojan://c@jp.example.net:443#c'] },
    { page_ms: 400 }, { advance_ms: 20 }, { page_ms: 200 }, { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 3000 },
    { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 900 }], () => {
    const inv = T.rt.__fake.invocations.filter(i => i.cmd === 'import.apply').map(i => i.args.plan_id);
    return [
      [JSON.stringify(inv) === JSON.stringify(['plan-1', 'plan-2']), `plan_expired: previewed again once, then imported with plan-2 (${inv})`],
      [!!document.querySelector('.tiers-row[data-key="v-imp1"].is-new') && !!document.querySelector('.tiers-row[data-key="v-imp2"].is-new'), 'the new rows (by difference) glow'],
    ];
  }, { extra: { langs: LANGS9 } }),
  base('tiers_import_rejected', 'tiers_import_errors', [...go('sentinel'), ...click('.tiers-imp'), { fill: ['.tiers-imp-ta textarea', 'https://sub.example.com/x\nhttps://sub.example.com/y\nxyz://abc'] },
    { page_ms: 400 }, { advance_ms: 20 }, { page_ms: 200 }], () => [
    [document.querySelectorAll('.tiers-imp-why .tiers-imp-wr').length === 2, 'the rejected reasons, counted (two lines)'],
    [!document.querySelector('.tiers-imp-groups'), 'no groups: no group list'],
    [!!document.querySelector('.tiers-imp-tosub'), 'a subscription address pasted: the subscription tab is offered'],
    [document.querySelector('.tiers-imp .ovl-foot .btn-primary').disabled, 'B-37: a rejected line blocks the batch'],
  ]),
  base('tiers_g19_manual_form', 'tiers_import_errors', [...go('sentinel'), ...click('.tiers-imp'), ...click('.tiers-imp-tabs [data-v="manual"]'), { advance_ms: 20 }, { page_ms: 300 }], () => [
    [!!document.querySelector('#tiers-imp-manual [data-f="protocol"] .select'), 'G19: the protocol choice from import.specs'],
    [!document.querySelector('#tiers-imp-manual .tiers-imp-kind'), 'Sentinel: single protocol only'],
  ]),
  base('tiers_import_sub_first_fetch_failed', 'tiers_import_errors', [...go('sentinel'), ...click('.tiers-imp'), ...click('.tiers-imp-tabs [data-v="sub"]'),
    { fill: ['#tiers-imp-sub input', 'https://sub.example.com/new'] }, { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 1500 }], () => [
    [!document.querySelector('.sheet.tiers-imp'), 'saved: the sheet closes'],
    // (T-P5-13 fix round 1) the whole sentence before {reason} (every language puts the reason
    // last), not its first five letters: de / fr Abonn…, en Subsc… would also match another notice
    [[...document.querySelectorAll('.toast, .notice, [role="status"]')].some(e => e.textContent.includes(T.t('tiers.import.sub.added_failed', { reason: '' }).trim())), 'the first fetch failed is said'],
  ], { extra: { langs: LANGS9 } }),
  // import.apply failed after it saved (PROTOCOL section 15, review A M1): the form closes, "saved, not
  // applied" is said with why, the lists are read again and the new rows glow by difference
  base('tiers_import_saved_unapplied_paste', 'tiers_import_unapplied', [...click('.tiers-imp'), { fill: ['.tiers-imp-ta textarea', 'hysteria2://a@hk.example.net:443#a\nvless://b@hk.example.net:8443#b\ntrojan://c@jp.example.net:443#c'] },
    { page_ms: 400 }, { advance_ms: 20 }, { page_ms: 200 }, { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 900 }], UNAPPLIED, { arg: { rows: ['v-imp1', 'v-imp2'], reason: 'core_reload_failed' }, extra: { langs: LANGS9 } }),
  base('tiers_import_saved_unapplied_manual', 'tiers_import_unapplied', [...click('.tiers-imp'), ...manualSocks5, { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 900 }],
    UNAPPLIED, { arg: { rows: ['v-man1'], reason: 'runtime_reload_failed' }, extra: { langs: LANGS9 } }),
  // section 10.3 / 12.3: import.apply's error.field lands on the field of the manual form (duplicate_node)
  base('tiers_import_manual_field_error', 'tiers_import_errors', [...go('sentinel'), ...click('.tiers-imp'), ...manualSocks5, { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 900 }], () => {
    const f = document.querySelector('#tiers-imp-manual [data-f="server"]');
    const sent = T.rt.__fake.invocations.filter(i => i.cmd === 'import.apply' && i.args.mode === 'manual');
    return [
      [sent.length === 1 && sent[0].args.manual.protocol === 'socks5' && sent[0].args.manual.fields.server === 'm1.example.net', 'the manual form is sent (socks5, its fields)'],
      [!!document.querySelector('.sheet.tiers-imp'), 'not saved: the form stays open'],
      [f && f.classList.contains('is-invalid') && f.textContent.includes(T.t('errors.import.duplicate_node')), 'duplicate_node is said on the server field'],
    ];
  }, { extra: { langs: LANGS9 } }),
  // F-01 L2 (user feedback 10-27: on Bastion, 导入 showed the paste and the subscription panes on one
  // page until a tab was clicked). The import sheet, opened from the overview's Bastion card (the
  // user's way), shows the selected tab's pane only; the screenshot is that sheet
  base('tiers_import_bastion_first_open', 'tiers_running', [...fromOverview('bastion'), ...click('.tiers-tb .tiers-imp'), impPanes('bastion:first')], impPanesCheck, { arg: { 'bastion:first': 'paste' } }),
  // ... then each tab shows its own pane, a second opening too, and Sentinel (paste · manual ·
  // subscription) and Vanguard (paste · manual) the same
  base('tiers_import_panes_tabs', 'tiers_running', [...fromOverview('bastion'),
    ...click('.tiers-tb .tiers-imp'), impPanes('bastion:first'),
    ...click('.tiers-imp-tabs [data-v="sub"]'), impPanes('bastion:sub'),
    ...click('.tiers-imp-tabs [data-v="manual"]'), { advance_ms: 20 }, { page_ms: 300 }, impPanes('bastion:manual'),
    ...click('.tiers-imp-tabs [data-v="paste"]'), impPanes('bastion:paste'),
    ...closeImport, ...click('.tiers-tb .tiers-imp'), impPanes('bastion:again'),
    ...closeImport, ...go('sentinel'), ...click('.tiers-tb .tiers-imp'), impPanes('sentinel:first'),
    ...closeImport, ...go('vanguard'), ...click('.tiers-tb .tiers-imp'), impPanes('vanguard:first')],
  impPanesCheck, { arg: { 'bastion:first': 'paste', 'bastion:sub': 'sub', 'bastion:manual': 'manual', 'bastion:paste': 'paste', 'bastion:again': 'paste', 'sentinel:first': 'paste', 'vanguard:first': 'paste' } }),
  // ... and after an import into Bastion the list is read again (tiers_import_bastion): a pasted link
  // lands among your own nodes, a subscription among your sources (its nodes among the nodes, as the
  // Bastion list keeps every node you added: mapping_tiers, Bastion's two sections; since F-01 round 1
  // each of them names its source on its second line, as on Sentinel), each row once, the new rows
  // glowing, the sheet closed
  base('tiers_import_bastion_paste_list', 'tiers_import_bastion', [...go('bastion'), ...click('.tiers-tb .tiers-imp'), { fill: ['.tiers-imp-ta textarea', 'trojan://x@bx.example.net:443#bx-imported'] },
    { page_ms: 400 }, { advance_ms: 20 }, { page_ms: 200 }, { click: '.sheet.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 900 }], bastionSections,
  { arg: { apply: { plan_id: 'plan-b1' }, mine: ['b-imp1', 'b-s1', 'b-u1'], msrc: ['g:sub-mysrc'], fresh: ['b-imp1'], src: { 'b-imp1': '', 'b-u1': '', 'b-s1': 'my-source' } } }),
  base('tiers_import_bastion_sub_list', 'tiers_import_bastion', [...go('bastion'), ...click('.tiers-tb .tiers-imp'), ...click('.tiers-imp-tabs [data-v="sub"]'),
    { fill: ['#tiers-imp-sub input', 'https://new-source.example.com/sub'] }, { click: '.sheet.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, { page_ms: 1500 }], bastionSections,
  { arg: { apply: { mode: 'subscriptions', urls: ['https://new-source.example.com/sub'] }, mine: ['b-s1', 'b-sn1', 'b-sn2', 'b-u1'], msrc: ['g:sub-mysrc', 'g:sub-newsrc'], fresh: ['b-sn1', 'b-sn2', 'g:sub-newsrc'], src: { 'b-sn1': 'new-source', 'b-sn2': 'new-source', 'b-s1': 'my-source', 'b-u1': '' } } }),
  // ---- the manage drawer's revealed address (PROTOCOL section 12, review A M2 / B M1) ----------------
  base('tiers_manage_reveal_close', 'tiers_reset', [...manageReveal, { eval: () => {
    const x = document.querySelector('.tiers-url'); let e = x; while (e && !e.querySelector(':scope > * .ovl-x, :scope > .ovl-x')) e = e.parentElement;
    const b = e && e.querySelector('.ovl-x'); if (!b) throw new Error('no drawer close button'); b.click();
  } }, { page_ms: 800 }], (secret) => [
    [!document.querySelector('.tiers-url'), 'the drawer is closed'],
    [!document.documentElement.outerHTML.includes(secret), 'closed: the revealed address is gone from the page (outerHTML)'],
  ], { arg: SECRET, extra: { langs: LANGS9 } }),
  // ---- runtime generations (criticism B9) ------------------------------------------------------------
  base('tiers_reset_manage_reveal', 'tiers_reset', [...manageReveal, { eval: () => {
    const fake = T.rt.__fake;
    const t0 = 1790000000000, e2 = 'rt-fixture-e2', b = { epoch: e2, produced_at_ms: t0, sent_at_ms: t0 + 1 };
    const hello = window.__TESSERACT_FIXTURE_HELLO__ || null;
    fake.deliver({ t: 'rt:reset', epoch: e2, reason: 'runtime_gone' });
    if (hello) fake.deliver({ ...hello, ...b });
  } }, { page_ms: 1200 }], (secret) => {
    // T-P4-H2 item 4 (05b review cont. 1 S2): a new generation closes the drawer, as the side panel
    const toasts = [...document.querySelectorAll('#toast-slot .toast')].map(x => x.textContent);
    return [
      [!document.querySelector('.tiers-url'), 'a new generation closes the 管理订阅 drawer'],
      [toasts.some(x => x.includes(T.t('tiers.panel.reset'))), `the page says why (${JSON.stringify(toasts)})`],
      [!document.documentElement.outerHTML.includes(secret), 'a new generation: the revealed address is gone from the page (outerHTML)'],
    ];
  }, { arg: SECRET, extra: { langs: LANGS9 } }),
  base('tiers_reset_generation', 'tiers_reset', [...click(row('v-hk03')), { click: '.tiers-panel [data-p="diag"]' }, { advance_ms: 50 }, { page_ms: 200 },
    { eval: () => {
      const fake = T.rt.__fake;
      const t0 = 1790000000000, e2 = 'rt-fixture-e2', b = { epoch: e2, produced_at_ms: t0, sent_at_ms: t0 + 1 };
      const hello = window.__TESSERACT_FIXTURE_HELLO__ || null;
      fake.deliver({ t: 'rt:reset', epoch: e2, reason: 'runtime_gone' });
      if (hello) fake.deliver({ ...hello, ...b });
    } }, { page_ms: 1200 }], () => {
    const c = T.rt.__fake.invocations.filter(i => i.cmd === 'nodes.cancelDiagnose');
    return [
      [!document.querySelector('.tiers-panel'), 'a new generation closes the side panel'],
      [c.length === 0, 'no cancel goes to the new sidecar for the old generation\'s diagnosis'],
    ];
  }, { extra: { langs: LANGS9 } }),
  base('tiers_reset_same_epoch', 'tiers_reset', [...click(row('v-hk03')), { eval: () => {
    const fake = T.rt.__fake;
    fake.deliver({ t: 'rt:reset', epoch: 'rt-fixture-e1', reason: 'reconnect' });
    const h = window.__TESSERACT_FIXTURE_HELLO__;
    if (h) fake.deliver(h);
  } }, { page_ms: 1200 }], () => [
    [!!document.querySelector('.tiers-panel'), 'a same-epoch reconnect keeps the side panel open'],
  ], { extra: { langs: LANGS9 } }),
];

// the hello of the scenario, for the reset cases (delivered again by their actions)
for (const c of CASES) {
  const sc = typeof c.scenario === 'string' ? SC(c.scenario) : c.scenario;
  const hello = sc.steps.find(s => s.deliver && s.deliver.t === 'hello').deliver;
  if (c.name.startsWith('tiers_reset')) c.actions.unshift({ eval: h => { window.__TESSERACT_FIXTURE_HELLO__ = h; }, arg: hello });
}

// (T-P5-10: the known failure of tiers_gateway_needs_route_pseudo is gone. T.ui.busy caps the
// reserved width at the button's container (the row's action cell) and shows the short fail word
// (or the glyph alone) with the whole words as tip and description when they do not fit; the case
// runs in the nine languages and pseudo. T-P5-15: its pseudo cell has no allowance of its own any
// more: the hover tip of the fail face wraps inside its 300 px since T-P5-14 gave .tip
// overflow-wrap: anywhere, so BUSY_TIP_PSEUDO is gone, here and in malicious.mjs.)
// The real languages of a case are one case (zh_CN unless it names more); pseudo is its own.
// (T-P5-13, requirement 3: the 16 cases that named zh_CN alone, extra: { langs }, run in the nine
// languages now; they still have no pseudo cell)
// T-P5-11 (ruling 3): a case with a pseudo cell (and so the pseudo allowances) runs its real-language
// cell in all nine languages, with no allowance; a case that names its languages keeps them
export default CASES.flatMap(c => {
  const langs = c.langs || [...LANGS9, 'pseudo'];
  const real = langs.filter(lang => lang !== 'pseudo');
  return [
    ...(real.length ? [{ ...c, langs: real }] : []),
    ...(langs.includes('pseudo') ? [{ ...c, name: `${c.name}_pseudo`, langs: ['pseudo'], overflow_allow: [...PSEUDO_LENGTH, ...PSEUDO_BOX, ...(c.pseudo_allow ? c.pseudo_allow() : [])] }] : []),
  ];
});
