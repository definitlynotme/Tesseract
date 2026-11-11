// Malicious fixtures (T-P4-07; P4 architecture §7, 决定_第八波裁定 T-P4-07 补充): the four pages and
// the shell layer each replay untrusted text — node, subscription and Gateway names, the exit's
// org / city and country code, protocol keys, the published route's leaf, log title / detail /
// raw, host:notify, error params, release notes — made of
// <img onerror>, <script>, an attribute break-out with <svg onload>, javascript:, ICU-sensitive
// characters ({ } ' #) and bidirectional controls. Scenarios: ui/test/fixtures/malicious/*.json
// (generated from the named scenarios/ files; each says which in its desc).
//
// The matrix's inject check fails any cell with script:not([src]), [onerror], [onload],
// img[src="x"] or window.__pwned set; these cases add: no javascript: link, no element made from
// the payloads (b / svg outside the icons / foreignObject), and the payload text — the ICU
// characters included — shows literally where the page shows that datum.
// The two *_too_large cases are the fifth-wave ruling 31b-8: an oversize import.preview /
// import.apply rejected at once with frame_too_large says so where the user started it.
import { readFileSync } from 'node:fs';
import { PSEUDO_OVERFLOW as OVERVIEW_PSEUDO } from './overview_i18n.mjs';
import { PSEUDO_LENGTH as TIERS_PSEUDO, pillClearInPage } from './tiers_i18n.mjs';
import { PSEUDO_LENGTH as SETTINGS_PSEUDO, pseudoBreaks } from './settings_i18n.mjs';
import { LANGS9, busyFaceWatch, busyFailFitInPage, busyFailMetricsInPage } from './tiers.mjs';

const FX = name => JSON.parse(readFileSync(new URL(`../../fixtures/malicious/${name}.json`, import.meta.url), 'utf8'));
const P = {
  img: '<img src="x" onerror="window.__pwned=1">',
  script: '<script>window.__pwned=2</script>',
  svg: '"><svg onload="window.__pwned=3">',
  js: 'javascript:window.__pwned=4',
  icu: "{n, plural, one {#} other {x}} 'q' #",
  bidi: '‮evil‬⁦x⁩',
};
const ms = n => ({ page_ms: n });
// 总览 payloads (malicious_overview*.json): the Gateway name, and the leaf / selected pair
const GW = `${P.img} GW ${P.icu}`;
const LEAF = `<b>LEAF</b><img src="x" onerror="window.__pwned=5"> Leaf ${P.icu}`;
const GWSEL = `${P.svg} GWSEL ${P.icu}`;
const overviewFields = () => ({ route: (document.querySelector('#ov-route') || {}).textContent || '', gate: (document.querySelector('#ov-gate-stat') || {}).textContent || '', proto: (document.querySelector('#ov-pleg') || {}).textContent || '' });

/* Runs in the page: what came out of the payloads, and the visible text (innerText keeps the
   ICU characters and the bidi controls as they are). */
function pageInjectState() {
  const icons = el => !!el.closest('svg.ic');
  return {
    jsLinks: [...document.querySelectorAll('a[href], [src], [action], [formaction]')].filter(e => /^\s*javascript:/i.test(e.getAttribute('href') || e.getAttribute('src') || e.getAttribute('action') || e.getAttribute('formaction') || '')).length,
    svgs: [...document.querySelectorAll('svg')].filter(s => !icons(s) && !s.closest('.spk, .ov-chart, #ov-chart, .chart, [class*="chart"], [class*="spark"]') && !s.classList.contains('ic')).map(s => s.outerHTML.slice(0, 80)),
    foreign: document.querySelectorAll('foreignObject').length,
    // a <b> made from the data: its text is exactly a fragment the fixtures wrap in <b>…</b>
    // (bold? in a log detail, X as the exit's country code, LEAF in the route's leaf); data sits
    // under translate="no", so that subtree is read too (T-P4-07 fix round 1). The page's own <b>
    // are not data: .ttag / .callout-t, and the emphasis of a rich message around a whole value
    // (`<b>{name}</b>`: its text is the full payload, never one of these fragments alone)
    bold: [...document.querySelectorAll('b')].filter(b => !b.closest('.ttag, .callout-t')
      && /^(?:bold\?|X|LEAF)$/.test(b.textContent.trim())).map(b => b.textContent.trim().slice(0, 40)),
    pwned: window.__pwned === undefined ? null : window.__pwned,
    text: document.body.innerText,
  };
}
// the checks every case adds; `shows` are payload texts that must be on screen literally
const injectExpect = (shows, more) => async ({ page }) => {
  const s = await page.evaluate(pageInjectState);
  const problems = [];
  if (s.pwned !== null) problems.push(`window.__pwned = ${s.pwned}`);
  if (s.jsLinks) problems.push(`${s.jsLinks} javascript: link(s)`);
  if (s.foreign) problems.push('a foreignObject reached the DOM');
  if (s.bold.length) problems.push(`markup from data became <b>: ${JSON.stringify(s.bold)}`);
  for (const x of s.svgs) if (/onload|__pwned/.test(x)) problems.push(`an svg from data: ${x}`);
  for (const t of shows) if (!s.text.includes(t)) problems.push(`not shown literally: ${JSON.stringify(t)}`);
  if (more) problems.push(...(await more(page)));
  return { problems, metrics: { shown: shows.length } };
};

const CASES = [
  /* 总览: Gateway in force (selected = effective = leaf), org / city / country, protocol keys, node names */
  { name: 'malicious_overview', scenario: FX('malicious_overview'), page: 'overview',
    expect: injectExpect([`${P.img} GW ${P.icu}`, `${P.svg} City ${P.icu}`, `${P.script} Org ${P.bidi}`, P.img, '<b>X</b>'],
      async page => {
        const r = await page.evaluate(overviewFields);
        const p = [];
        // (runtime invariant 5: while the Gateway carries with effective set, leaf = effective, so
        // here the route and the Gateway row show the same payload; malicious_overview_leaf
        // gives the leaf a payload of its own)
        if (!r.route.includes(GW)) p.push(`the route line does not show the leaf literally: ${r.route}`);
        if (!r.gate.includes(GW)) p.push(`the Gateway row does not show the name literally: ${r.gate}`);
        if (!r.proto.includes('{n, plural, one {#} other {x}}')) p.push(`the protocol legend does not show the key literally: ${r.proto}`);
        return p;
      }) },
  /* 总览: the Gateway carries, gateway.current has a selected node not in force yet (effective
     null): the route line shows route.published.leaf and the Gateway row the selected name, two
     different payloads (field by field) */
  { name: 'malicious_overview_leaf', scenario: FX('malicious_overview_leaf'), page: 'overview',
    expect: injectExpect([LEAF, GWSEL], async page => {
      const r = await page.evaluate(overviewFields);
      const p = [];
      if (!r.route.includes(LEAF)) p.push(`the route line does not show the leaf literally: ${r.route}`);
      if (r.route.includes('GWSEL')) p.push(`the route line shows the selected Gateway, not the leaf: ${r.route}`);
      if (!r.gate.includes(GWSEL)) p.push(`the Gateway row does not show the selected name literally: ${r.gate}`);
      if (r.gate.includes('Leaf')) p.push(`the Gateway row shows the leaf: ${r.gate}`);
      return p;
    }) },
  /* 总览: a Gateway chosen but not in force (selected), a start failure whose params are hostile.
     errors.start.listener_conflict names no parameter (no errors.* message does), so the params
     must not reach the page at all: the error's own words are shown, the PARAM-* marks nowhere
     (text or attributes) */
  { name: 'malicious_overview_selected', scenario: FX('malicious_overview_selected'), page: 'overview',
    expect: injectExpect([`${P.svg} GW ${P.icu}`], async page => {
      const r = await page.evaluate(() => ({ want: T.t('errors.start.listener_conflict'), text: document.body.innerText, html: document.body.outerHTML }));
      const p = [];
      if (!r.text.includes(r.want)) p.push('the start failure is not shown with its own words (errors.start.listener_conflict)');
      for (const m of ['PARAM-PORT', 'PARAM-EXTRA']) if (r.html.includes(m)) p.push(`an error param reached the page: ${m}`);
      return p;
    }) },
  /* 层级: node / subscription / Gateway names; the Sentinel tier with its subscriptions. The long
     Gateway name carries the traffic: the carrying pill covers none of the cell's text and the
     name ends in an ellipsis (T-P5-03; 10th-wave ruling item 3; T-P4-H4) */
  { name: 'malicious_tiers', scenario: FX('malicious_tiers'), page: 'profiles',
    expect: injectExpect([P.icu], page => page.evaluate(pillClearInPage, 'gateway')) },
  { name: 'malicious_tiers_sentinel', scenario: FX('malicious_tiers'), page: 'profiles', actions: [{ go: 'profiles', params: { tier: 'sentinel' } }, ms(900)],
    expect: injectExpect([P.img]) },
  /* 运行日志: the hostile key event opened in the reader; the raw lines */
  { name: 'malicious_logs', scenario: FX('malicious_logs'), page: 'logs',
    actions: [{ eval: t => { const row = [...document.querySelectorAll('#logs-events .logs-row')].find(r => r.textContent.includes(t)); if (!row) throw new Error('no row: ' + t); row.click(); }, arg: 'ICU {n, plural' }, ms(400)],
    expect: injectExpect([`${P.svg} ICU ${P.icu}`, `${P.js} ${P.bidi}`, '<b>bold?</b>'], async page => {
      const title = await page.evaluate(() => (document.querySelector('.logs-reader .ovl-title') || {}).textContent || '');
      return title === `${'"><svg onload="window.__pwned=3">'} ICU {n, plural, one {#} other {x}} 'q' #` ? [] : [`the reader title is not the literal text: ${title}`];
    }) },
  { name: 'malicious_logs_raw', scenario: FX('malicious_logs'), page: 'logs', actions: [{ click: '#logs-view [data-v="raw"]' }, ms(400)],
    expect: injectExpect([`${P.script} ${P.icu} ${P.bidi}`, `${P.img} ${P.icu}`]) },
  /* 配置: release notes (plain text in their sheet), the alert history */
  { name: 'malicious_settings_notes', scenario: FX('malicious_settings'), page: 'settings', actions: [{ click: '#set-upd-notes' }, ms(700)],
    expect: injectExpect([P.img, P.script, P.icu], async page => {
      const t = await page.evaluate(() => (document.querySelector('.set-notes-text') || {}).textContent || null);
      return t && t.includes(`- ${P.svg} ${P.js}`) && t.includes(`- ${P.bidi}`) ? [] : [`the notes are not the literal text: ${t}`];
    }) },
  /* The 10-30 notice redesign removed the alert history list and the Telegram pairing drawer from
     配置, so their two cases (alert titles, the pairing QR) went with them. */
  /* 外壳层: host:notify title / detail */
  { name: 'malicious_shell', scenario: FX('malicious_shell'), page: 'overview',
    expect: injectExpect([`${P.svg} ${P.icu}`, `${P.js} ${P.bidi} ${P.icu}`, P.script, P.img]) },
  /* 31b-8: an oversize request rejected at once (frame_too_large) is said where it was started */
  { name: 'malicious_import_preview_too_large', scenario: FX('malicious_import_preview_too_large'), page: 'profiles',
    actions: [{ click: '.tiers-imp' }, ms(700), { fill: ['.tiers-imp-ta textarea', `vless://a@h.example.net:443#${'x'.repeat(64)}`] }, ms(600), { advance_ms: 20 }, ms(300)],
    expect: injectExpect([], async page => {
      const r = await page.evaluate(() => ({ want: T.t('errors.frame_too_large'), sum: (document.querySelector('.tiers-imp .tiers-imp-sum') || {}).textContent || '',
        btn: !!(document.querySelector('.tiers-imp .ovl-foot .btn-primary') || {}).disabled, inv: T.rt.__fake.invocations.filter(i => i.cmd === 'import.preview').length }));
      const p = [];
      if (r.inv !== 1) p.push(`import.preview sent ${r.inv} time(s)`);
      if (!r.sum.includes(r.want)) p.push(`the import sheet does not say frame_too_large where the paste was recognised: ${r.sum}`);
      if (!r.btn) p.push('import stays enabled after the preview was refused');
      return p;
    }) },
  { name: 'malicious_import_apply_too_large', scenario: FX('malicious_import_apply_too_large'), page: 'profiles',
    actions: [{ click: '.tiers-imp' }, ms(700), { fill: ['.tiers-imp-ta textarea', 'hysteria2://a@hk.example.net:443#a\nvless://b@hk.example.net:8443#b\ntrojan://c@jp.example.net:443#c'] },
      ms(400), { advance_ms: 20 }, ms(200), { eval: busyFaceWatch, arg: '.tiers-imp .ovl-foot .btn-primary' }, { click: '.tiers-imp .ovl-foot .btn-primary' }, { advance_ms: 50 }, ms(600)],
    // T-P5-10 (ruling 3): nine languages and pseudo; the import button's fail face (the whole
    // frame_too_large sentence, or the short fail word with it as tip and description) keeps both
    // footer buttons inside the sheet's footer
    langs9: true,
    expect: async ctx => {
      const arg = { btn: '.tiers-imp .ovl-foot .btn-primary', box: '.ovl-foot', keep: '.tiers-imp .ovl-foot > *', want: 'errors.frame_too_large' };
      const res = await injectExpect([], async page => {
        const r = await page.evaluate(() => {
          const b = document.querySelector('.tiers-imp .ovl-foot .btn-primary');
          // the button's shown label only (T-P5-10 fix round 1): b.textContent would also read the
          // whole words of a hidden .busy-why, so this check could never fail
          const lab = b ? [...b.querySelectorAll('.lab .sw')].filter(x => !x.closest('[aria-hidden="true"]')).pop() || b.querySelector('.lab') : null;
          return { want: T.t('errors.frame_too_large'), open: !!document.querySelector('.sheet.tiers-imp'),
            sheet: (document.querySelector('.sheet.tiers-imp .ovl-body') || {}).textContent || '', btn: lab ? lab.textContent : '', tip: b ? b.getAttribute('data-tip') || '' : '',
            inv: T.rt.__fake.invocations.filter(i => i.cmd === 'import.apply').length };
        });
        const p = [];
        if (r.inv !== 1) p.push(`import.apply sent ${r.inv} time(s)`);
        if (!r.open) p.push('the import sheet closed');
        if (!r.sheet.includes(r.want) && !r.btn.includes(r.want) && r.tip !== r.want) p.push(`frame_too_large is not said in the import sheet or on its button: sheet=${r.sheet.slice(0, 200)} button=${r.btn} tip=${r.tip}`);
        for (const [ok, msg] of await page.evaluate(busyFailFitInPage, arg)) if (!ok) p.push(msg);
        return p;
      })(ctx);
      return { ...res, metrics: { ...res.metrics, ...(await ctx.page.evaluate(busyFailMetricsInPage, arg)) } };
    } },
];

// Overflow (not what these cases are about; every entry says why). zh_CN: the logs page's own
// design allowance. (The two long data names that ran past their boxes, the overview's route line
// and the tier switch's Gateway cell, end in an ellipsis since T-P4-H4, P5 #29; their entries are
// gone.) pseudo (T-P4-07 fix round 1: no blanket allowance
// any more): the same, plus each page's own named pseudo-length entries, taken from that page's
// cases (overview_i18n PSEUDO_OVERFLOW, tiers_i18n / settings_i18n PSEUDO_LENGTH; the logs one
// repeats logs.mjs's, which is not exported), and the settings pages get settings_i18n's
// pseudoBreaks first, as its own pseudo cells do; any other overflow fails the cell.

const ZH_ALLOW = [
  { selector: '^#logs-events > div\\.li\\.logs-row[^ ]* > span\\.logs-main[^ ]* > span\\.logs-[dt][^ ]* > button\\.ttag', why: '关键事件行单行省略：标题、详情是记录原文（数据，translate="no"），其中内嵌的层级标签（代号是数据，角色词 span.r 是目录文字）随所在数据行的省略号被截。决定_P5裁定 P5-e 第 5 条要求界面文字不以省略号显示；这里登记为已知例外，待 Claude 裁定（T-P5-13 修复记录第 1 轮 S1）：标签嵌在句中，不能像原始日志那样把数据先让、标签移到行尾；完整文字在阅读器里，标签的无障碍名称（logs.tier_link.aria）含角色词' },
];
const PSEUDO_BY_PAGE = {
  overview: OVERVIEW_PSEUDO,
  profiles: [
    ...TIERS_PSEUDO,
    { selector: '^#layer > div\\.ovl > div\\.sheet\\.tiers-imp[^ ]* > div\\.ovl-foot[^ ]* > button\\.btn[^ ]* > span\\.lab$', why: '伪语言：导入底单脚栏的次按钮标签定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  ],
  logs: [
    { selector: '^#logs-rows > div\\.logs-chunk[^ ]* > div\\.logs-rrow[^ ]* > span\\.logs-c[^ ]* > span\\.sev', why: '伪语言：原始日志表的级别列定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）；同 logs.mjs PSEUDO_LENGTH' },
    { selector: '^#logs-sc-raw > div\\.logs-grid > div\\.logs-gh[^ >]* > span:nth-of-type\\(1\\)$', why: '伪语言（T-P5-13 修复第 1 轮）：原始日志表「时间」列定宽 78px，伪语言的列头补长后约 63px（内容盒 54px），以省略号结束；9 种真实语言都放得下；同 logs.mjs PSEUDO_LENGTH' },
    { selector: '^#logs-events > div\\.li\\.logs-row[^ ]* > span\\.sev', why: '伪语言：关键事件行的级别列按当前语言最宽的级别词定宽，上限 88px（T-P5-04）；伪语言的级别词超过上限（T-P5-11 第 1 轮修复：盒子越界规则的「文字越出自身格」量到它）；同 logs.mjs PSEUDO_LENGTH，9 种真实语言都在上限内' },
  ],
  settings: SETTINGS_PSEUDO,
};
// the design target's demo control carries a CSS badge (css/design.css .demo-link::before
// content "演示", design target only), as in overview.mjs / settings_i18n.mjs
const DEMO_BADGE = ['^演示$'];

// (T-P5-10: the known failure of malicious_import_apply_too_large_pseudo is gone. T.ui.busy caps the
// reserved width at the footer and shows the short fail word with the whole words as tip and
// description when they do not fit; that case (langs9) runs in the nine languages and pseudo.)
// (T-P5-14: the pseudo cell's hover-tip overflow allowance of T-P5-10 is gone. The pointer still rests on the
// import button, so its hover tip (the whole frame_too_large words) shows; .tip now breaks a word
// that cannot wrap (overflow-wrap: anywhere), so pseudo's tilde padding stays inside its 300 px.)
// T-P5-11 (ruling 3): every case's real-language cell runs in all nine languages (was zh_CN unless
// langs9), with the zh_CN allowance only
export default CASES.flatMap(({ langs9, ...c }) => [
  { ...c, langs: LANGS9, overflow_allow: ZH_ALLOW },
  { ...c, name: `${c.name}_pseudo`, langs: ['pseudo'], overflow_allow: [...ZH_ALLOW, ...(PSEUDO_BY_PAGE[c.page] || [])], leak_allow: DEMO_BADGE,
    ...(c.page === 'settings' ? { actions: [{ eval: pseudoBreaks }, { page_ms: 100 }, ...(c.actions || [])] } : {}) },
]);
