// Render cases of T-P4-04d (message extraction · logs) and the helpers its unit test
// (ui/test/unit/logs_i18n.test.mjs) shares.
//
// Default export: fixture-mode cases for the render matrix (scenarios
// ui/test/fixtures/scenarios/logs_i18n_{running,stopped}.json, ASCII-only log rows, no
// simulator): the key-event list, the raw table, the reader (a key event, a folded repeat, a
// multi-line raw line), the filter popover, the ⋯ menu, and the stopped page with an empty
// search. Besides the matrix's own checks (leak in pseudo, overflow, injection, console) each
// cell asserts that the logs page and the overlays it owns show no CJK text outside
// translate="no" unless the language is zh_CN (critique E2), that every visible aria-label /
// title / placeholder / data-tip there came from the catalog in pseudo (wrapped in ⟦ ⟧), and,
// for the reader on tier.bastion_active, that the public-node warning is there (decided by the
// event key, not by the wording).
//
// The page still takes its records from the design target's transitional sim:log channel
// (T-P4-05c moves it to logs.batch). feedFixtureLogs hands the scenario's logs.batch rows (v1
// Log DTOs) to that channel; design/logs_demo.js fromSim keeps their key, level, source and
// category.
//
// Named exports:
//   RUNNING, STOPPED          the two scenarios (objects)
//   feedFixtureLogs()         in page: emit the scenario's logs.batch rows as sim:log
//   logsTextInPage()          in page: text nodes and text attributes of the logs page and its
//                             overlays, outside translate="no" ({texts, attrs, days})
//   dayLabelsInPage()         in page: the day separator labels of the key-event list
//   logsTreeInPage()          in page: the textree rows (T-P4-01 baseline format) of the whole body
import { readFileSync } from 'node:fs';

const scenario = name => JSON.parse(readFileSync(new URL(`../../fixtures/scenarios/${name}.json`, import.meta.url), 'utf8'));
export const RUNNING = scenario('logs_i18n_running');
export const STOPPED = scenario('logs_i18n_stopped');

/* Runs in the page. */
export function feedFixtureLogs() {
  let sc = window.__TESSERACT_FIXTURE__;
  if (typeof sc === 'string') sc = JSON.parse(sc);
  let n = 0;
  for (const st of (sc && sc.steps) || []) {
    const f = st.deliver;
    if (!f || f.topic !== 'logs.batch' || !f.data) continue;
    for (const r of f.data.records) {
      window.T.emit('sim:log', { id: 'F' + r.seq, ts: r.ts_ms, level: r.level, source: r.source, category: r.category,
        key: r.key === 'log.raw' ? null : r.key, title: r.title, detail: r.detail, raw: r.raw });
      n++;
    }
  }
  return n;
}

/* Runs in the page: what the logs page and the overlays it opens (reader, filter popover, ⋯
   menu, row menu) show outside translate="no". texts: visible text nodes; attrs: [attr, value]
   of visible elements (aria-label, title, placeholder, data-tip); days: the day labels. */
export function logsTextInPage() {
  const roots = [document.querySelector('section[data-page="logs"]'), ...document.querySelectorAll('.logs-reader, .pop, .menu')].filter(Boolean);
  const texts = [], attrs = [];
  const skip = el => !!(el && el.closest && el.closest('[translate="no"]'));
  for (const root of roots) {
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
  const days = [...document.querySelectorAll('#logs-events > .logs-day > span:first-child')].map(e => e.textContent.trim());
  return { texts, attrs, days };
}

export function dayLabelsInPage() {
  return {
    events: [...document.querySelectorAll('#logs-events > .logs-day > span:first-child')].map(e => e.textContent.trim()),
    origin: (document.querySelector('#logs-events .logs-origin-t') || { textContent: '' }).textContent.trim(),
  };
}

/* the T-P4-01 baseline text tree (test/baseline/capture.mjs textTreeInPage), whole body */
export function logsTreeInPage() {
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
  visit(document.body, 0);
  return out;
}

const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/u;
// tokens that may stay unbracketed in pseudo (same idea as checks.mjs LEAK_WHITELIST): digits,
// times, counters, product and tier names
const PLAIN = /^[\d\s:.,×·/–\-—%()（）]*$|^(?:Tesseract|Mihomo|Vanguard|Sentinel|Bastion|Gateway)$/u;

/** the expect() of every case: problems for this cell */
async function logsExpect({ page, lang }, extra) {
  const problems = [];
  const r = await page.evaluate(logsTextInPage);
  // the fake second language: every word comes from the catalog (the other 8 product languages
  // have no logs.* translation before P5 and fall back to zh_CN, so they are not judged here)
  if (lang === 'pseudo') {
    for (const s of r.texts) if (CJK.test(s)) problems.push(`CJK text outside translate="no": ${s.slice(0, 60)}`);
    for (const [a, v] of r.attrs) if (CJK.test(v)) problems.push(`CJK ${a} outside translate="no": ${v.slice(0, 60)}`);
    for (const [a, v] of r.attrs) if (!v.includes('⟦') && !PLAIN.test(v)) problems.push(`${a} not from the catalog: ${v.slice(0, 60)}`);
  }
  if (!r.texts.length) problems.push('the logs page shows no text');
  if (extra) problems.push(...(await extra(page, lang, r)));
  return { problems, metrics: { texts: r.texts.length, attrs: r.attrs.length, days: r.days } };
}

const feed = [{ eval: feedFixtureLogs }, { page_ms: 400 }];
const clickRow = text => ({ eval: t => { const row = [...document.querySelectorAll('#logs-events .logs-row, #logs-rows .logs-rrow')].find(r => r.textContent.includes(t)); if (!row) throw new Error('no row: ' + t); row.click(); }, arg: text });

const CASES = [
  {
    name: 'logs_i18n_events',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed],
    expect: ctx => logsExpect(ctx, async (page, lang) => {
      const p = [];
      const d = await page.evaluate(dayLabelsInPage);
      if (d.events.length < 2) p.push(`day separators: ${JSON.stringify(d.events)}`);
      if (lang === 'zh_CN' && !d.events.includes('今天 · 9月29日 · 周二')) p.push(`zh_CN day labels: ${JSON.stringify(d.events)}`);
      return p;
    }),
  },
  {
    name: 'logs_i18n_raw',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed, { click: '#logs-view [data-v="raw"]' }, { page_ms: 400 }],
    expect: ctx => logsExpect(ctx),
  },
  {
    name: 'logs_i18n_reader_bastion',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed, clickRow('Traffic on Bastion'), { page_ms: 400 }],
    // 11-05: the reader adds no public-node warning to a Bastion event any more (it is said once at
    // the fallback notice and in the letter for users, letter/letter.js)
    expect: ctx => logsExpect(ctx, async page => {
      const has = await page.evaluate(() => !!document.querySelector('.logs-reader .logs-rd .callout-warning'));
      return has ? ['tier.bastion_active: the reader still carries a standing public-node warning'] : [];
    }),
  },
  {
    name: 'logs_i18n_reader_fold',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed, clickRow('Source refresh failed'), { page_ms: 400 }, { click: '.logs-reader .logs-rd-more' }, { page_ms: 400 }],
    expect: ctx => logsExpect(ctx, async page => {
      const r = await page.evaluate(() => ({ warn: !!document.querySelector('.logs-reader .logs-rd .callout-warning'), title: document.querySelector('.logs-reader .ovl-title').getAttribute('translate'),
        shm: document.querySelectorAll('.logs-reader .logs-rd-sh .sh-m').length, keys: [...document.querySelectorAll('.logs-reader .logs-rd-kv dt')].map(e => e.textContent.trim()), repeat: T.t('logs.reader.kv.repeat') }));
      const p = [];
      if (r.warn) p.push('update.sources_failed: unexpected public-node warning');
      if (r.title !== 'no') p.push('the reader title (a log headline) is not translate="no"');
      // F-01: the count of the folded repeat is the 重复 row's; the 原文 heading has no side note
      if (r.shm) p.push('F-01: the 原文 heading carries a side note');
      if (!r.keys.includes(r.repeat)) p.push(`the folded repeat has no ${r.repeat} row (${JSON.stringify(r.keys)})`);
      return p;
    }),
  },
  {
    name: 'logs_i18n_reader_raw',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed, { click: '#logs-view [data-v="raw"]' }, { page_ms: 400 }, clickRow('Traceback'), { page_ms: 400 }],
    expect: ctx => logsExpect(ctx, async page => {
      const r = await page.evaluate(() => ({ t: document.querySelector('.logs-reader .ovl-title').getAttribute('translate'),
        d: document.querySelectorAll('.logs-reader .logs-rd-d').length, shm: document.querySelectorAll('.logs-reader .logs-rd-sh .sh-m').length }));
      const p = r.t === 'no' ? ['the reader title of a raw line (our wording) is marked translate="no"'] : [];
      // F-01: a raw line's reader starts with its facts (no 这是一条原始日志… sentence, no 逐字记录 note)
      if (r.d) p.push('F-01: a description paragraph in the reader of a raw line');
      if (r.shm) p.push('F-01: the 原文 heading carries a side note');
      return p;
    }),
  },
  {
    name: 'logs_i18n_filter',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed, { click: '#logs-filter' }, { page_ms: 400 }],
    expect: ctx => logsExpect(ctx, async page => {
      const r = await page.evaluate(() => ({ chips: document.querySelectorAll('.logs-flt .fchip').length, foot: [...document.querySelectorAll('.logs-flt-foot > *')].map(e => e.className) }));
      const p = r.chips >= 5 ? [] : ['filter popover: chips missing'];
      // F-01: the foot is the 清除筛选 link alone (no 都不选时显示全部 · 两个视图共用)
      if (JSON.stringify(r.foot) !== JSON.stringify(['link-btn'])) p.push(`F-01: filter popover foot ${JSON.stringify(r.foot)}`);
      return p;
    }),
  },
  {
    name: 'logs_i18n_more',
    scenario: RUNNING,
    page: 'logs',
    actions: [...feed, { click: '#logs-more' }, { page_ms: 400 }],
    // F-01: the only side note in ⋯ is the JSON export's count
    expect: ctx => logsExpect(ctx, async page => {
      const hints = await page.evaluate(() => [...document.querySelectorAll('.menu .menu-i')].filter(b => b.querySelector('.hint')).map(b => b.dataset.i));
      return JSON.stringify(hints) === JSON.stringify(['1']) ? [] : [`F-01: menu items with a side note: ${JSON.stringify(hints)} (the JSON export's count only)`];
    }),
  },
  {
    name: 'logs_i18n_stopped_search',
    scenario: STOPPED,
    page: 'logs',
    actions: [...feed, { fill: ['#logs-search input', 'zzzz'] }, { page_ms: 400 }],
    expect: ctx => logsExpect(ctx, async page => {
      const r = await page.evaluate(() => ({ shown: !document.querySelector('#logs-empty').hidden, sub: !!document.querySelector('#logs-empty .empty-s'),
        tip: document.querySelector('#logs-feed-m').getAttribute('data-tip') }));
      const p = r.shown ? [] : ['empty state not shown for a search without hits'];
      // F-01: the title and 清除搜索 (no 没有包含「…」的关键事件 under it); Mihomo 未启动 without a tip
      if (r.sub) p.push('F-01: a sentence under the empty state\'s title');
      if (r.tip != null) p.push(`F-01: Mihomo footer tip while stopped: ${r.tip}`);
      return p;
    }),
  },
];

// Truncation allowances (run.mjs overflow_allow; every entry says why):
//   DESIGN — by design in every language: a key-event row is one line with an ellipsis, so a
//   tier tag inside its headline or detail is cut where the line ends (the reader shows it all).
//   PSEUDO_LENGTH — pseudo only: labels that the ~3x pseudo text outgrows in fixed-width places.
//   Not this ticket's to fix (it moves words, not layout): listed in the T-P4-04d report for
//   the layout work (T-P4-09 / T-P4-H); the shell footer belongs to the shell (T-P4-04a).
const DESIGN = [
  { selector: '^#logs-events > div\\.li\\.logs-row[^ ]* > span\\.logs-main[^ ]* > span\\.logs-[dt][^ ]* > button\\.ttag', why: '关键事件行单行省略：标题、详情是记录原文（数据，translate="no"），其中内嵌的层级标签（代号是数据，角色词 span.r 是目录文字）随所在数据行的省略号被截。决定_P5裁定 P5-e 第 5 条要求界面文字不以省略号显示；这里登记为已知例外，待 Claude 裁定（T-P5-13 修复记录第 1 轮 S1）：标签嵌在句中，不能像原始日志那样把数据先让、标签移到行尾；完整文字在阅读器里，标签的无障碍名称（logs.tier_link.aria）含角色词' },
];
const PSEUDO_LENGTH = [
  { selector: '^#logs-follow > span\\.lab', why: '伪语言：「实时跟随 / 已暂停」按钮的标签宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-view > button\\.seg-btn', why: '伪语言：视图分段按钮宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-level > button\\.seg-btn', why: '伪语言：级别分段按钮宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-total$', why: '伪语言：页脚条数的宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-demo$', why: '伪语言：设计稿演示按钮（T-P4-07 删除）' },
  { selector: '^#logs-rows > div\\.logs-chunk[^ ]* > div\\.logs-rrow[^ ]* > span\\.logs-c[^ ]* > span\\.sev', why: '伪语言：原始日志表的级别列按当前语言最宽的级别词定宽，上限 108px（T-P5-04）；伪语言的级别词（约 85px）超过上限。9 种真实语言都在上限内，由全语言矩阵核对' },
  { selector: '^#logs-events > div\\.li\\.logs-row[^ ]* > span\\.sev', why: '伪语言：关键事件行的级别列按当前语言最宽的级别词定宽，上限 88px（T-P5-04）；伪语言的级别词（约 85px）超过上限。9 种真实语言都在上限内，由全语言矩阵核对' },
  { selector: '^#mi\\d+ > span\\.hint', why: '伪语言：「更多」菜单项右侧的提示句末尾的波浪号补长不能换行，宽过菜单（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: '^#logs-sc-raw > div\\.logs-grid > div\\.logs-gh[^ >]* > span:nth-of-type\\(1\\)$', why: '伪语言（T-P5-13 修复第 1 轮）：原始日志表「时间」列定宽 78px（放 hh:mm:ss 的等宽时刻；左 14px、右 10px 内边距，内容盒 54px），伪语言的列头「时间」补长后约 63px，以省略号结束。修复第 1 轮起检查器按内容盒、小数精度量省略号才量到它（旧检查器只比内边距盒、差 1px 以上才判）。9 种真实语言的列头都放得下，由全语言矩阵核对；同 logs.mjs PSEUDO_LENGTH' },
  { selector: '^#logs-rows > div\\.logs-chunk[^ ]* > div\\.logs-rrow[^ ]* > span\\.logs-c\\.logs-src', why: '伪语言（T-P5-13 省略号规则）：原始日志表的来源列按当前语言最宽的来源词或列头定宽，上限 150px；伪语言的「未知来源」连同波浪号补长宽过上限，以省略号结束（悬停行时阅读器给全文）。9 种真实语言都在上限内、来源词不省略，由全语言矩阵核对' },
];
// design.css `.demo-link::before { content: "演示" }` (design target only, file owned by T-P4-02b,
// removed with the demo controls in T-P4-07): the badge of #logs-demo, not this page's wording
const DEMO_BADGE = ['^演示$'];

// two variants per case: the product languages (zh_CN in a default run; all nine with
// --langs all) with the design allowances, and the pseudo language with the length allowances
// as well. The leak check (pseudo) is hard for the logs page: pages/logs.js has no
// @i18n-pending any more.
const LANGS = ['zh_CN', 'en_US', 'ja_JP', 'ko_KR', 'fr_FR', 'de_DE', 'es_ES', 'pt_BR', 'ru_RU'];
export default CASES.flatMap(c => [
  { ...c, langs: LANGS, overflow_allow: DESIGN },
  { ...c, name: c.name + '_pseudo', langs: ['pseudo'], overflow_allow: [...DESIGN, ...PSEUDO_LENGTH], leak_allow: DEMO_BADGE },
]);
