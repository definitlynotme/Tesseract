// Render cases of T-P4-04e (message extraction · settings and alerts) and the helpers its unit
// test (ui/test/unit/settings_i18n.test.mjs) shares.
//
// Default export: fixture-mode cases for the render matrix (scenarios
// ui/test/fixtures/scenarios/settings_i18n_{running,stopped}.json, ASCII-only frames): the whole
// page (all seven sections), the page with drafts (the save bar, effect tags, the index dots),
// the Telegram drawer (quick-setup tab, the manual tab with its fields and the advanced block),
// the Twilio drawer (a paid channel), the stopped page, and the demo's two failures (Telegram's
// test with the expired token, the first rule-data update). Besides the
// matrix's own checks (leak in pseudo, overflow, injection, console) each cell asserts that the
// settings page and the overlays it owns show no CJK text outside translate="no" unless the
// language is zh_CN (critique E2), and that every visible aria-label / title / placeholder /
// data-tip there came from the catalog in pseudo (wrapped in ⟦ ⟧) or is a plain data token.
//
// The page still takes its values from the design data and the design target's simulator
// (T-P4-05d / 05e wire config.snapshot and alerts.*); the scenarios give the shell and the
// runtime state an ASCII-only fixture. The demo data (alert history, delivery notes, release
// notes) stays inside if (__DEMO__) and renders under translate="no".
//
// Named exports:
//   RUNNING, STOPPED          the two scenarios (objects)
//   settingsTextInPage()      in page: text nodes and text attributes of the settings page and
//                             its overlays (drawer, sheet, popover, menu, save bar), outside
//                             translate="no" ({texts, attrs})
//   settingsTreeInPage()      in page: the textree rows (T-P4-01 baseline format) of the whole body
//   draftStateInPage()        in page: what a language switch must keep (switches, typed add
//                             fields, changed rows, index dots, save bar count, drawer, scroll)
import { readFileSync } from 'node:fs';

const scenario = name => JSON.parse(readFileSync(new URL(`../../fixtures/scenarios/${name}.json`, import.meta.url), 'utf8'));
export const RUNNING = scenario('settings_i18n_running');
export const STOPPED = scenario('settings_i18n_stopped');

/* Runs in the page. */
export function settingsTextInPage() {
  const roots = [document.querySelector('section[data-page="settings"]'), ...document.querySelectorAll('.ovl, .pop, .menu, .savebar')].filter(Boolean);
  const texts = [], attrs = [];
  const skip = el => !!(el && el.closest && el.closest('[translate="no"]'));
  const seen = new Set();
  for (const root of roots) {
    if ([...seen].some(r => r.contains(root))) continue;
    seen.add(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const p = n.parentElement;
      const s = n.nodeValue.replace(/\s+/g, ' ').trim();
      if (!s || !p || skip(p) || !p.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
      if (p.closest('.sw[aria-hidden="true"], .odo-v')) continue;   // a swap or odometer leaving / digit strips
      texts.push(s);
    }
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (skip(el) || !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
      for (const a of ['aria-label', 'title', 'placeholder', 'data-tip']) { const v = el.getAttribute(a); if (v && v.trim()) attrs.push([a, v.trim()]); }
    }
  }
  // data values that a message carries as a parameter inside an attribute (an attribute cannot
  // mark a part of itself translate="no"): the node names of the latency preview's tooltips
  const nodes = (window.T && window.T.data && window.T.data.nodes) || {};
  const data = Object.values(nodes).flat().map(n => n && n.name).filter(Boolean);
  return { texts, attrs, data };
}

/* the T-P4-01 baseline text tree (test/baseline/capture.mjs textTreeInPage) of the whole body, or
   of the elements that match `scope` (a selector list) */
export function settingsTreeInPage(scope) {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
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
  if (scope) for (const root of document.querySelectorAll(scope)) visit({ children: [root] }, 0);
  else visit(document.body, 0);
  return out;
}

/* Runs in the page: the state a language switch must keep. */
export function draftStateInPage() {
  const sec = document.querySelector('section[data-page="settings"]');
  const sw = {};
  for (const el of sec.querySelectorAll('.switch[data-k]')) sw[el.dataset.k] = el.getAttribute('aria-checked');
  const val = sel => { const el = sec.querySelector(sel); return el ? el.value : null; };
  const bar = document.querySelector('.savebar');
  const dr = document.querySelector('.ovl .drawer');
  return {
    switches: sw,
    typed: { rule: val('#set-rule-in'), src: val('#set-src-in'), res: val('#set-res-in') },
    changedRows: [...sec.querySelectorAll('.is-changed')].map(e => e.id || e.dataset.key || e.dataset.ch || e.getAttribute('href') || e.className).sort(),
    tocDots: [...sec.querySelectorAll('.toc-a.is-changed')].map(a => a.dataset.target).sort(),
    dirty: !!(window.T.ui.saveBarCurrent && window.T.ui.saveBarCurrent.dirty),
    count: window.T.ui.saveBarCurrent ? window.T.ui.saveBarCurrent.count : 0,
    bar: !!(bar && bar.checkVisibility()),
    drawer: dr ? { open: true, adv: !!dr.querySelector('.set-adv:not([hidden])'), chat: (dr.querySelector('[data-f="chat"] input') || {}).value || null } : null,
    scroll: document.querySelector('#host').scrollTop,
  };
}

const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/u;
// tokens that may stay unbracketed in pseudo (same idea as checks.mjs LEAK_WHITELIST): digits,
// times, counters, ports, product and tier names
const PLAIN = /^[\d\s:.,×·/–\-—%()（）]*$|^(?:Tesseract|Mihomo|Vanguard|Sentinel|Bastion|Gateway)$/u;

/** the expect() of every case: problems for this cell */
async function settingsExpect({ page, lang }, extra) {
  const problems = [];
  const r = await page.evaluate(settingsTextInPage);
  // the fake second language: every word comes from the catalog (the other 8 product languages
  // have no settings.* / alerts.* / catalog.* translation before P5 and fall back to zh_CN)
  if (lang === 'pseudo') {
    const strip = v => r.data.reduce((x, d) => x.split(d).join(''), v);
    for (const s of r.texts) if (CJK.test(s)) problems.push(`CJK text outside translate="no": ${s.slice(0, 60)}`);
    for (const [a, v] of r.attrs) if (CJK.test(strip(v))) problems.push(`CJK ${a} outside translate="no": ${v.slice(0, 60)}`);
    for (const [a, v] of r.attrs) if (!v.includes('⟦') && !PLAIN.test(v)) problems.push(`${a} not from the catalog: ${v.slice(0, 60)}`);
  }
  if (!r.texts.length) problems.push('the settings page shows no text');
  if (extra) problems.push(...(await extra(page, lang, r)));
  return { problems, metrics: { texts: r.texts.length, attrs: r.attrs.length } };
}

const openDrawer = id => [{ eval: ch => document.querySelector(`.set-ch[data-ch="${ch}"]`).click(), arg: id }, { page_ms: 600 }];
const SECTIONS = ['set-conn', 'set-route', 'set-dns', 'set-nodes', 'set-safety', 'set-alerts', 'set-app'];

const CASES = [
  {
    name: 'settings_i18n_page',
    scenario: RUNNING,
    page: 'settings',
    expect: ctx => settingsExpect(ctx, async page => {
      const ids = await page.evaluate(() => [...document.querySelectorAll('section[data-page="settings"] .fsec[id]')].map(s => s.id));
      return SECTIONS.every(id => ids.includes(id)) ? [] : [`sections: ${ids.join(',')}`];
    }),
  },
  {
    name: 'settings_i18n_drafts',
    scenario: RUNNING,
    page: 'settings',
    actions: [
      { click: '.switch[data-k="ipv6"]' }, { page_ms: 600 },
      { fill: ['#set-rule-in', 'example.org'] }, { page_ms: 600 },
    ],
    expect: ctx => settingsExpect(ctx, async page => {
      const s = await page.evaluate(draftStateInPage);
      const p = [];
      if (!s.dirty || !s.bar) p.push(`save bar: dirty=${s.dirty} shown=${s.bar}`);
      if (!s.tocDots.length) p.push('no section index dot for the drafts');
      return p;
    }),
  },
  {
    name: 'settings_i18n_drawer_telegram',
    scenario: RUNNING,
    page: 'settings',
    actions: [...openDrawer('telegram')],
    expect: ctx => settingsExpect(ctx, async page => ((await page.evaluate(() => !!document.querySelector('.ovl .drawer .set-ch-tabs'))) ? [] : ['the Telegram drawer (tabs) is not open'])),
  },
  {
    name: 'settings_i18n_drawer_telegram_manual',
    scenario: RUNNING,
    page: 'settings',
    actions: [...openDrawer('telegram'), { click: '.ovl .drawer .set-ch-tabs .tab[data-v="manual"]' }, { page_ms: 600 },
      { click: '.ovl .drawer .set-adv-t' }, { page_ms: 600 }],
    expect: ctx => settingsExpect(ctx, async page => ((await page.evaluate(() => !!document.querySelector('.ovl .drawer .set-adv:not([hidden])'))) ? [] : ['the advanced block is not open'])),
  },
  {
    name: 'settings_i18n_drawer_twilio',
    scenario: RUNNING,
    page: 'settings',
    // (T-P4-05e) the tiles carry the backend's type ids (alerts.catalog): Twilio is sms_twilio
    actions: [...openDrawer('sms_twilio'), { click: '.ovl .drawer .set-adv-t' }, { page_ms: 600 }],
    expect: ctx => settingsExpect(ctx),
  },
  {
    name: 'settings_i18n_stopped',
    scenario: STOPPED,
    page: 'settings',
    expect: ctx => settingsExpect(ctx),
  },
  // the demo's failures (design target): Telegram's expired token fails its test (the button's
  // failure word is a catalog message, the provider's reason a translate="no" data node), and the
  // first rule-data update fails one set (its reason in the status label's <x> slot)
  {
    name: 'settings_i18n_test_fail',
    scenario: RUNNING,
    page: 'settings',
    actions: [...openDrawer('telegram'),
      { eval: () => document.querySelector('.ovl .drawer .ovl-foot .btn:not(.btn-primary):not(.btn-danger-soft)').click() }, { page_ms: 1600 }],
    expect: ctx => settingsExpect(ctx, async page => ((await page.evaluate(() => !!document.querySelector('.ovl .drawer .ovl-foot .btn.is-fail'))) ? [] : ['the test button shows no failure'])),
  },
  {
    name: 'settings_i18n_geo_fail',
    scenario: RUNNING,
    page: 'settings',
    // T-P4-05d: geo.update is the scenario's (one kind failed: Geo.items, the reason by message_key
    // under the cards); the demo's first-update failure is gone with the design data
    actions: [{ eval: () => document.querySelector('#set-geo-btn').click() }, { page_ms: 1800 }],
    expect: ctx => settingsExpect(ctx, async page => ((await page.evaluate(() => !!document.querySelector('#set-geo .set-geo-i.is-fail') && !!document.querySelector('#set-geo-err:not([hidden]) .set-geo-e'))) ? [] : ['no failed rule-data set with its reason under the cards'])),
  },
];

// Truncation allowances (run.mjs overflow_allow; every entry says why). Filled from the first
// pseudo runs: labels that the ~3x pseudo text outgrows in fixed-width places are layout work
// (T-P4-09 / T-P4-H), not this ticket's (it moves words, not layout).
const DESIGN = [];
// (exported: core_i18n's shell_settings pseudo cell shows this page too; T-P4-H2 item 6)
export const PSEUDO_LENGTH = [
  { selector: '^#set-(?:rule-kind|rule-pick|set-behav)-v > span\\.sw$', why: '伪语言：分流规则的种类、内容和规则集内容下拉框当前值定宽（配置整理 P2；真实语言默认值最长的俄语「Автоопределение」在 152px 内放得下），选项全名在菜单里；同 settings.mjs PSEUDO_LENGTH' },
  { selector: '^#set-rl-custom > div\\.li\\.set-rule[^ ]* > span\\.set-rule-k[^ ]* > span\\.set-rule-res', why: '伪语言：规则行「解析域名」小标记在窄窗口的种类列里放不下（配置整理 P2；真实语言九种都放得下）；同 settings.mjs PSEUDO_LENGTH' },
  { selector: '^#set-(?:dns-mode|src-type|res-type) > span\\.select-v', why: '伪语言：下拉框当前值定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-ch-gal > div\\.set-chg[^ ]* > span\\.set-chg-l', why: '伪语言：提醒方式图库的分组名列定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-recent > div\\.li\\.two[^ ]* > span\\.sev', why: '伪语言：最近提醒的级别列定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-rl-cm > span\\.sw', why: '伪语言：自定义规则条数（窄窗口）定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-geo > div\\.set-geo-i[^ ]* > span\\.set-geo-n', why: '伪语言：规则数据四个卡片的标题（窄窗口）定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-[A-Za-z]+-row > div\\.frow-l[^ ]* > div\\.frow-t[^ ]* > span\\.set-t$', why: '伪语言：设置行标题末尾的波浪号补长不能换行（真实语言的长标题已可换行）（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: '^#set-upd-(?:row > div\\.frow-l[^ ]* > div\\.frow-d\\.set-ver|st > span\\.swap-box > span\\.sw$)', why: '伪语言（T-P5-13）：「版本与更新」行的三个按钮（更新说明、安装更新、检查更新）的伪语言标签都不能换行，占去整行，左侧的版本行与检查状态句只剩约 8px，伪语言补长的波浪号词越出；配置页目录列在伪语言里宽到上限（+28px）后更明显。9 种真实语言同一用例不越界，由全语言矩阵核对' },
];
// design.css `.demo-link::before { content: "演示" }` (design target only, file owned by T-P4-02b,
// removed with the demo controls in T-P4-07): the badge of the demo links, not this page's wording
const DEMO_BADGE = ['^演示$'];

// Two pseudo-only widenings of the page (host_scroll), layout work for T-P4-H / T-P4-09 and
// listed in the T-P4-04e report:
//   * pseudo text has no break opportunity inside a sentence (a Chinese sentence becomes one long
//     accented "word"), so the network-mode cards' notes (`.opt-d`, the administrator gate) push
//     the page wider than #host; real languages wrap at spaces;
//   * the copy button of the data directories (core `.copy`, T-P4-04a) keeps its ~3x longer
//     hidden "copied" label to the right of the button, past the window edge at 1060 px.
// The pseudo cells give the notes a break opportunity and clip the copy label with a test-only
// style, so host_scroll stays a hard check for everything else.
export function pseudoBreaks() {
  // (a constructed style sheet, not a <style> element: the --csp strict run (T-P4-08b) serves the
  // shell's style-src 'self', which blocks an inline <style> but not the CSSOM)
  const s = new CSSStyleSheet();
  s.replaceSync('');
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, s];
}

const LANGS = ['zh_CN', 'en_US', 'ja_JP', 'ko_KR', 'fr_FR', 'de_DE', 'es_ES', 'pt_BR', 'ru_RU'];
export default CASES.flatMap(c => [
  { ...c, langs: LANGS, overflow_allow: DESIGN },
  { ...c, name: c.name + '_pseudo', langs: ['pseudo'], actions: [{ eval: pseudoBreaks }, { page_ms: 100 }, ...(c.actions || [])], overflow_allow: [...DESIGN, ...PSEUDO_LENGTH], leak_allow: DEMO_BADGE },
]);
